from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, TypedDict


# "sleeping" polygons are escort destinations, not movement constraints: the
# robot-side world model turns their centroid into a navigable sleeping area,
# and the navigation loaders ignore the kind entirely.
ZoneKind = Literal["keep_in", "keep_out", "sleeping"]
Point = tuple[float, float]


class MapZone(TypedDict):
    id: str
    name: str
    kind: ZoneKind
    points_frame: Literal["map", "world"]
    points: list[list[float]]
    world_points: list[list[float]]
    # Odometry epoch the world_points snapshot was taken in. Robot-side
    # consumers only trust the snapshot while this matches the running
    # session's epoch; None marks a legacy/unknown snapshot.
    world_epoch: str | None
    created_at: float
    active: bool


def odom_epoch_path_for(zone_path: Path) -> Path:
    """Locate the odometry-epoch file that scopes world_points snapshots.

    NIGHTWATCH_ODOM_EPOCH_PATH wins when set (run_scout.sh exports it); the
    fallback derives the sibling name used by both the legacy and per-venue
    layouts (nightwatch_zones.json -> nightwatch_odom_epoch.json, zones.json
    -> odom_epoch.json), keeping the venue bundle together (AGENTS
    invariant 9).
    """
    env = os.getenv("NIGHTWATCH_ODOM_EPOCH_PATH")
    if env:
        return Path(env)
    name = zone_path.name
    if "zones" in name:
        return zone_path.with_name(name.replace("zones", "odom_epoch", 1))
    return zone_path.with_name("nightwatch_odom_epoch.json")


def current_odom_epoch_id(zone_path: Path) -> str | None:
    """Return the running session's odometry epoch id, or None if unknown."""
    try:
        raw = odom_epoch_path_for(zone_path).read_text(encoding="utf-8")
        return str(json.loads(raw)["epoch_id"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _finite_point(point: list[float] | tuple[float, float]) -> Point:
    if len(point) != 2:
        raise ValueError("each point must contain x and y")
    x, y = float(point[0]), float(point[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("zone coordinates must be finite")
    if abs(x) > 1_000 or abs(y) > 1_000:
        raise ValueError("zone coordinates are outside the map")
    return x, y


def _validate_polygon(points: list[list[float]] | list[Point]) -> list[list[float]]:
    if not 3 <= len(points) <= 128:
        raise ValueError("a zone needs between 3 and 128 vertices")
    clean = [_finite_point(point) for point in points]
    area_twice = sum(
        clean[i][0] * clean[(i + 1) % len(clean)][1]
        - clean[(i + 1) % len(clean)][0] * clean[i][1]
        for i in range(len(clean))
    )
    if abs(area_twice) < 0.02:
        raise ValueError("zone polygon is too small")
    return [[x, y] for x, y in clean]


class ZoneStore:
    """Small atomic JSON store shared by the booth and robot navigation."""

    # Sentinel stamp for a world_points list that was merely copied from MAP
    # coordinates (no browser-provided WORLD snapshot). It never equals a
    # session epoch, so frame-validating consumers refuse it until the robot
    # anchorer rewrites the snapshot under a live transform and restamps it.
    WORLD_EPOCH_UNANCHORED = "unanchored"

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Cross-process lock shared with the robot-side zone anchorer.

        Both this store and nightwatch/navigation._anchor_world_operator_zones
        rewrite the same JSON with read-modify-write cycles; without a shared
        lock a zone saved during a patrol anchoring tick is silently lost.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        with open(lock_path, "w", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def list(self) -> list[MapZone]:
        with self._lock:
            return self._read()

    def create(
        self,
        *,
        name: str,
        kind: ZoneKind,
        points: list[list[float]],
        world_points: list[list[float]] | None = None,
        points_frame: Literal["map", "world"] = "map",
    ) -> MapZone:
        clean_points = _validate_polygon(points)
        clean_world = (
            _validate_polygon(world_points)
            if world_points is not None
            else clean_points
        )
        # Scope the WORLD snapshot to the session it was captured in, so
        # robot-side consumers can refuse it after an odometry reset instead
        # of enforcing/rendering the polygon at the wrong physical spot.
        if world_points is not None or points_frame == "world":
            world_epoch = current_odom_epoch_id(self.path)
        else:
            world_epoch = self.WORLD_EPOCH_UNANCHORED
        clean_name = " ".join(name.strip().split())[:48]
        if not clean_name:
            clean_name = {
                "keep_in": "Allowed area",
                "keep_out": "Restricted area",
                "sleeping": "Sleeping area",
            }[kind]
        zone: MapZone = {
            "id": uuid.uuid4().hex,
            "name": clean_name,
            "kind": kind,
            "points_frame": points_frame,
            "points": clean_points,
            "world_points": clean_world,
            "world_epoch": world_epoch,
            "created_at": time.time(),
            "active": True,
        }
        with self._lock, self._file_lock():
            zones = self._read()
            zones.append(zone)
            self._write(zones)
        return zone

    def delete(self, zone_id: str) -> bool:
        with self._lock, self._file_lock():
            zones = self._read()
            remaining = [zone for zone in zones if zone["id"] != zone_id]
            if len(remaining) == len(zones):
                return False
            self._write(remaining)
            return True

    def set_keep_in_active(self, active: bool) -> int:
        """Atomically enter focus-area or full-venue mode.

        Keep-out hazards always remain active. Only allowed-area polygons are
        toggled, so opening the booth never permits glass/window hazards.
        """
        with self._lock, self._file_lock():
            zones = self._read()
            changed = 0
            for zone in zones:
                if zone["kind"] == "keep_in" and zone["active"] != active:
                    zone["active"] = active
                    changed += 1
            if changed:
                self._write(zones)
            return changed

    def set_active(self, zone_id: str, active: bool) -> MapZone | None:
        with self._lock, self._file_lock():
            zones = self._read()
            selected: MapZone | None = None
            for zone in zones:
                if zone["id"] == zone_id:
                    zone["active"] = bool(active)
                    selected = zone
                    break
            if selected is None:
                return None
            self._write(zones)
            return selected

    def _read(self) -> list[MapZone]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            return []
        items = raw.get("zones", []) if isinstance(raw, dict) else []
        zones: list[MapZone] = []
        normalized_keys = {
            "id",
            "name",
            "kind",
            "points_frame",
            "points",
            "world_points",
            "world_epoch",
            "created_at",
            "active",
        }
        for item in items:
            try:
                kind = str(item["kind"])
                if kind not in {"keep_in", "keep_out", "sleeping"}:
                    continue
                points = _validate_polygon(item["points"])
                world_points = _validate_polygon(item.get("world_points", points))
                raw_epoch = item.get("world_epoch")
                # Robot-side bookkeeping (anchored_at, drawn_world_points,
                # anchor_transform, ...) must survive booth rewrites, or a
                # zone toggle would strip the very metadata that keeps the
                # polygon recoverable after an alignment correction.
                extras = {
                    key: value
                    for key, value in item.items()
                    if key not in normalized_keys
                }
                zones.append(
                    {
                        **extras,  # type: ignore[typeddict-item]
                        "id": str(item["id"]),
                        "name": str(item.get("name") or "Map zone")[:48],
                        "kind": kind,  # type: ignore[typeddict-item]
                        "points_frame": (
                            "world"
                            if str(
                                item.get(
                                    "points_frame",
                                    "world" if points == world_points else "map",
                                )
                            ).lower()
                            == "world"
                            else "map"
                        ),
                        "points": points,
                        "world_points": world_points,
                        "world_epoch": str(raw_epoch) if raw_epoch else None,
                        "created_at": float(item.get("created_at", 0.0)),
                        "active": bool(item.get("active", True)),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return zones

    def _write(self, zones: list[MapZone]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "zones": zones}, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
