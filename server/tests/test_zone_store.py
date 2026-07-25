from __future__ import annotations

import json

import pytest

from app.services.zone_store import ZoneStore


def test_zone_store_round_trip_and_delete(tmp_path) -> None:
    path = tmp_path / "zones.json"
    store = ZoneStore(str(path))

    zone = store.create(
        name="  Glass   wall  ",
        kind="keep_out",
        points=[[0, 0], [2, 0], [2, 2], [0, 2]],
        world_points=[[10, 10], [12, 10], [12, 12], [10, 12]],
    )

    assert zone["name"] == "Glass wall"
    assert zone["active"] is True
    assert zone["points_frame"] == "map"
    assert store.list() == [zone]
    assert json.loads(path.read_text())["version"] == 1
    assert store.delete(zone["id"]) is True
    assert store.list() == []
    assert store.delete(zone["id"]) is False


def test_demo_mode_only_disables_keep_in_zones(tmp_path) -> None:
    store = ZoneStore(str(tmp_path / "zones.json"))
    allowed = store.create(
        name="Booth",
        kind="keep_in",
        points=[[0, 0], [2, 0], [2, 2], [0, 2]],
    )
    hazard = store.create(
        name="Glass",
        kind="keep_out",
        points=[[3, 0], [4, 0], [4, 1], [3, 1]],
    )

    assert store.set_keep_in_active(False) == 1
    indexed = {zone["id"]: zone for zone in store.list()}
    assert indexed[allowed["id"]]["active"] is False
    assert indexed[hazard["id"]]["active"] is True
    assert store.set_keep_in_active(True) == 1
    updated = store.set_active(hazard["id"], False)
    assert updated is not None and updated["active"] is False
    assert store.set_active("missing", True) is None


def test_zone_store_rejects_degenerate_polygon(tmp_path) -> None:
    store = ZoneStore(str(tmp_path / "zones.json"))

    with pytest.raises(ValueError, match="too small"):
        store.create(
            name="line",
            kind="keep_in",
            points=[[0, 0], [1, 0], [2, 0]],
        )


def test_legacy_equal_points_are_identified_as_world_until_anchored(tmp_path) -> None:
    path = tmp_path / "zones.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "zones": [
                    {
                        "id": "focus",
                        "name": "Focus area",
                        "kind": "keep_in",
                        "points": [[0, 0], [2, 0], [2, 2], [0, 2]],
                        "world_points": [[0, 0], [2, 0], [2, 2], [0, 2]],
                    }
                ],
            }
        )
    )

    zone = ZoneStore(str(path)).list()[0]

    assert zone["points_frame"] == "world"


def test_sleeping_zone_kind_round_trips_and_survives_mode_switch(tmp_path) -> None:
    # "sleeping" polygons are escort destinations, not constraints: they must
    # persist through reloads and never be toggled by focus/full venue mode.
    store = ZoneStore(str(tmp_path / "zones.json"))
    sleeping = store.create(
        name="",
        kind="sleeping",
        points=[[5, 5], [7, 5], [7, 7], [5, 7]],
    )
    assert sleeping["name"] == "Sleeping area"
    assert sleeping["kind"] == "sleeping"

    reloaded = ZoneStore(str(tmp_path / "zones.json")).list()
    assert [zone["kind"] for zone in reloaded] == ["sleeping"]

    # Focus/full mode only touches keep_in polygons.
    assert store.set_keep_in_active(False) == 0
    assert store.list()[0]["active"] is True


def test_created_zone_world_snapshot_is_epoch_stamped(tmp_path, monkeypatch) -> None:
    """world_points are only meaningful in the odometry session they were
    captured in; the stamp lets robot-side consumers refuse them after a
    restart instead of enforcing polygons at the wrong physical spot."""
    epoch_path = tmp_path / "odom_epoch.json"
    epoch_path.write_text(
        json.dumps(
            {"epoch_id": "epoch-a", "updated_at": 0.0, "last_pose": [0.0, 0.0]}
        )
    )
    monkeypatch.setenv("NIGHTWATCH_ODOM_EPOCH_PATH", str(epoch_path))
    store = ZoneStore(str(tmp_path / "zones.json"))

    zone = store.create(
        name="Booth",
        kind="keep_in",
        points=[[0, 0], [2, 0], [2, 2], [0, 2]],
        world_points=[[10, 10], [12, 10], [12, 12], [10, 12]],
    )
    assert zone["world_epoch"] == "epoch-a"
    assert store.list()[0]["world_epoch"] == "epoch-a"

    # A MAP-frame create without a browser WORLD snapshot only has copied MAP
    # coordinates: mark them so frame-validating consumers refuse them until
    # the robot anchorer rewrites and restamps the snapshot.
    derived = store.create(
        name="Derived",
        kind="keep_out",
        points=[[5, 5], [7, 5], [7, 7], [5, 7]],
    )
    assert derived["world_epoch"] == ZoneStore.WORLD_EPOCH_UNANCHORED


def test_created_zone_without_epoch_file_stays_legacy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "NIGHTWATCH_ODOM_EPOCH_PATH", str(tmp_path / "missing_epoch.json")
    )
    store = ZoneStore(str(tmp_path / "zones.json"))
    zone = store.create(
        name="Booth",
        kind="keep_in",
        points=[[0, 0], [2, 0], [2, 2], [0, 2]],
        world_points=[[0, 0], [2, 0], [2, 2], [0, 2]],
    )
    assert zone["world_epoch"] is None


def test_booth_rewrites_preserve_robot_bookkeeping(tmp_path) -> None:
    """Toggling a zone in the booth must not strip the anchoring metadata the
    robot uses to keep the polygon recoverable after an alignment fix."""
    path = tmp_path / "zones.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "zones": [
                    {
                        "id": "anchored",
                        "name": "Focus",
                        "kind": "keep_in",
                        "points_frame": "map",
                        "points": [[0, 0], [2, 0], [2, 2], [0, 2]],
                        "world_points": [[1, 1], [3, 1], [3, 3], [1, 3]],
                        "world_epoch": "epoch-a",
                        "anchored_at": 123.0,
                        "drawn_world_points": [[1, 1], [3, 1], [3, 3], [1, 3]],
                        "anchor_transform": [1.0, -2.0, 0.5],
                        "created_at": 1.0,
                        "active": True,
                    }
                ],
            }
        )
    )
    store = ZoneStore(str(path))

    assert store.set_active("anchored", False) is not None

    persisted = json.loads(path.read_text())["zones"][0]
    assert persisted["active"] is False
    assert persisted["world_epoch"] == "epoch-a"
    assert persisted["anchored_at"] == 123.0
    assert persisted["drawn_world_points"] == [[1, 1], [3, 1], [3, 3], [1, 3]]
    assert persisted["anchor_transform"] == [1.0, -2.0, 0.5]


def test_epoch_path_derivation_follows_venue_bundle(tmp_path, monkeypatch) -> None:
    from app.services.zone_store import odom_epoch_path_for

    monkeypatch.delenv("NIGHTWATCH_ODOM_EPOCH_PATH", raising=False)
    legacy = tmp_path / "maps" / "nightwatch_zones.json"
    venue = tmp_path / "venues" / "expo" / "zones.json"
    assert odom_epoch_path_for(legacy).name == "nightwatch_odom_epoch.json"
    assert odom_epoch_path_for(legacy).parent == legacy.parent
    assert odom_epoch_path_for(venue).name == "odom_epoch.json"
    assert odom_epoch_path_for(venue).parent == venue.parent
