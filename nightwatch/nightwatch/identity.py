"""Persistent anonymous full-body person re-identification.

This deliberately uses DimensionalOS' TorchReID/OSNet model on person crops,
not facial recognition. Galleries are bounded, local, confidence-gated, and
ambiguous matches are refused rather than silently switching targets.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sqlite3
from threading import RLock
import time
from typing import Any
import uuid

import numpy as np

from dimos.constants import DIMOS_PROJECT_ROOT
from dimos.msgs.sensor_msgs.Image import Image
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

BBox = tuple[float, float, float, float]


def _crop_person(image: Image, bbox: BBox) -> Image | None:
    x1, y1, x2, y2 = bbox
    width = max(1.0, x2 - x1)
    height = max(1.0, y2 - y1)
    # A little context helps shoes/clothing, while clamping prevents wraparound.
    left = int(x1 - 0.06 * width)
    top = int(y1 - 0.03 * height)
    right = int(x2 + 0.06 * width)
    bottom = int(y2 + 0.03 * height)
    crop = image.crop(left, top, right - left, bottom - top)
    if crop.width < 24 or crop.height < 48:
        return None
    return crop


class AnonymousPersonMemory:
    """Cross-session OSNet galleries plus fast session track association."""

    def __init__(
        self,
        path: str = "assets/output/memory/person_identity_v2.sqlite3",
        *,
        match_cos: float = 0.68,
        match_margin: float = 0.06,
        gallery_novelty: float = 0.94,
        gallery_max: int = 8,
        min_embed_interval_s: float = 4.0,
    ) -> None:
        db_path = Path(path)
        if not db_path.is_absolute():
            db_path = DIMOS_PROJECT_ROOT / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            db_path, timeout=10.0, check_same_thread=False
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS people (
                person_id TEXT PRIMARY KEY,
                alias TEXT,
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL,
                sightings INTEGER NOT NULL,
                consented INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS person_views (
                view_id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id TEXT NOT NULL,
                ts REAL NOT NULL,
                vector BLOB NOT NULL,
                dim INTEGER NOT NULL,
                FOREIGN KEY(person_id) REFERENCES people(person_id)
            );
            CREATE INDEX IF NOT EXISTS idx_person_views_person
                ON person_views(person_id, ts);
            """
        )
        self._db.commit()
        self._lock = RLock()
        self._model: Any = None
        self._match_cos = float(match_cos)
        self._match_margin = float(match_margin)
        self._gallery_novelty = float(gallery_novelty)
        self._gallery_max = int(gallery_max)
        self._min_embed_interval_s = float(min_embed_interval_s)
        self._galleries: dict[str, list[np.ndarray]] = defaultdict(list)
        self._session_tracks: dict[int, tuple[str, float]] = {}
        self._last_embedded: dict[str, float] = {}
        self._load_galleries()

    def _load_galleries(self) -> None:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT person_id,vector,dim FROM person_views
                ORDER BY ts ASC
                """
            ).fetchall()
            for person_id, blob, dim in rows:
                vec = np.frombuffer(blob, dtype=np.float32, count=int(dim)).copy()
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    self._galleries[str(person_id)].append(vec / norm)
            for person_id, views in list(self._galleries.items()):
                self._galleries[person_id] = views[-self._gallery_max :]

    def _get_model(self) -> Any:
        if self._model is None:
            from dimos.models.embedding.treid import TorchReIDModel

            model = TorchReIDModel(model_name="osnet_x1_0", device="cpu")
            model.start()
            self._model = model
            logger.info(
                "anonymous person re-identification ready",
                identities=len(self._galleries),
                model="osnet_x1_0",
            )
        return self._model

    def _embed(self, image: Image, bbox: BBox) -> np.ndarray | None:
        crop = _crop_person(image, bbox)
        if crop is None:
            return None
        try:
            embedding = self._get_model().embed(crop).to_numpy().astype(np.float32)
        except Exception:
            logger.exception("person embedding failed")
            return None
        embedding = embedding.reshape(-1)
        norm = float(np.linalg.norm(embedding))
        return embedding / norm if norm > 0 else None

    def _rank(self, vector: np.ndarray) -> list[tuple[str, float]]:
        ranked: list[tuple[str, float]] = []
        for person_id, gallery in self._galleries.items():
            if not gallery or gallery[0].size != vector.size:
                continue
            ranked.append(
                (person_id, max(float(np.dot(vector, view)) for view in gallery))
            )
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked

    def identify(
        self,
        image: Image,
        bbox: BBox,
        track_id: int,
        *,
        allow_new: bool = True,
    ) -> tuple[str | None, float]:
        """Associate a visible person, returning ``(person_id, confidence)``."""
        now = time.time()
        with self._lock:
            tracked = self._session_tracks.get(int(track_id))
            # BoT-SORT IDs can be recycled after a person leaves. Trust the
            # cheap session binding only for a very short continuity window;
            # after that, run OSNet again before carrying identity forward.
            if tracked is not None and now - tracked[1] <= 3.0:
                person_id = tracked[0]
                self._session_tracks[int(track_id)] = (person_id, now)
                self._touch(person_id, now, increment=False)
                if now - self._last_embedded.get(person_id, 0.0) < self._min_embed_interval_s:
                    return person_id, 1.0

        vector = self._embed(image, bbox)
        if vector is None:
            return (tracked[0], 0.5) if tracked is not None else (None, 0.0)

        with self._lock:
            ranked = self._rank(vector)
            best_id, best = ranked[0] if ranked else (None, 0.0)
            second = ranked[1][1] if len(ranked) > 1 else -1.0
            accepted = bool(
                best_id is not None
                and best >= self._match_cos
                and best - second >= self._match_margin
            )
            if not accepted:
                if not allow_new:
                    return None, float(best)
                best_id = f"person_{uuid.uuid4().hex[:10]}"
                self._db.execute(
                    """
                    INSERT INTO people(person_id,alias,created_at,last_seen,sightings,consented)
                    VALUES(?,?,?,?,?,0)
                    """,
                    (best_id, None, now, now, 1),
                )
                best = 1.0
            else:
                self._touch(str(best_id), now, increment=True)

            person_id = str(best_id)
            self._session_tracks[int(track_id)] = (person_id, now)
            self._last_embedded[person_id] = now
            self._add_view(person_id, vector, now)
            self._db.commit()
            return person_id, float(best)

    def verify(
        self, person_id: str, image: Image, bbox: BBox
    ) -> float:
        """Return best full-body cosine against one locked identity."""
        vector = self._embed(image, bbox)
        if vector is None:
            return 0.0
        with self._lock:
            gallery = self._galleries.get(person_id, [])
            if not gallery or gallery[0].size != vector.size:
                return 0.0
            return max(float(np.dot(vector, view)) for view in gallery)

    def find_visible(
        self,
        person_id: str,
        image: Image,
        detections: list[Any],
    ) -> tuple[Any | None, float]:
        """Find one known person and refuse near-ties between candidates."""
        scored: list[tuple[Any, float]] = []
        for detection in detections:
            score = self.verify(
                person_id,
                image,
                tuple(float(v) for v in detection.bbox),
            )
            scored.append((detection, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        if not scored:
            return None, 0.0
        best_det, best = scored[0]
        second = scored[1][1] if len(scored) > 1 else -1.0
        if best < self._match_cos or best - second < self._match_margin:
            return None, float(best)
        return best_det, float(best)

    def session_person(self, track_id: int, max_age_s: float = 12.0) -> str | None:
        """Return the persistent identity already bound to a live tracker ID."""
        with self._lock:
            tracked = self._session_tracks.get(int(track_id))
            if tracked is None or time.time() - tracked[1] > float(max_age_s):
                return None
            return tracked[0]

    @property
    def match_threshold(self) -> float:
        return self._match_cos

    def _touch(self, person_id: str, now: float, *, increment: bool) -> None:
        self._db.execute(
            """
            UPDATE people SET last_seen=?,
              sightings=sightings+?
            WHERE person_id=?
            """,
            (now, int(increment), person_id),
        )

    def _add_view(self, person_id: str, vector: np.ndarray, now: float) -> None:
        gallery = self._galleries[person_id]
        similarity = (
            max(float(np.dot(vector, view)) for view in gallery)
            if gallery
            else None
        )
        if similarity is not None and similarity >= self._gallery_novelty:
            return
        if len(gallery) >= self._gallery_max:
            # Remove the oldest persisted view and its in-memory counterpart.
            row = self._db.execute(
                """
                SELECT view_id FROM person_views WHERE person_id=?
                ORDER BY ts ASC LIMIT 1
                """,
                (person_id,),
            ).fetchone()
            if row is not None:
                self._db.execute(
                    "DELETE FROM person_views WHERE view_id=?", (row[0],)
                )
            gallery.pop(0)
        vec = np.asarray(vector, dtype=np.float32).reshape(-1)
        self._db.execute(
            """
            INSERT INTO person_views(person_id,ts,vector,dim)
            VALUES(?,?,?,?)
            """,
            (person_id, now, vec.tobytes(), vec.size),
        )
        gallery.append(vec)

    def list_people(self, active_within_s: float = 0.0) -> list[dict[str, Any]]:
        with self._lock:
            args: list[Any] = []
            where = ""
            if active_within_s > 0:
                where = " WHERE last_seen>=?"
                args.append(time.time() - float(active_within_s))
            rows = self._db.execute(
                """
                SELECT person_id,alias,created_at,last_seen,sightings,consented
                FROM people
                """
                + where
                + " ORDER BY last_seen DESC",
                args,
            ).fetchall()
            return [
                {
                    "person_id": row[0],
                    "alias": row[1],
                    "created_at": row[2],
                    "last_seen": row[3],
                    "sightings": row[4],
                    "consented": bool(row[5]),
                    "gallery_views": len(self._galleries.get(str(row[0]), [])),
                }
                for row in rows
            ]

    def set_alias(self, person_id: str, alias: str, consented: bool) -> bool:
        if not consented:
            return False
        with self._lock:
            cursor = self._db.execute(
                "UPDATE people SET alias=?,consented=1 WHERE person_id=?",
                (alias.strip(), person_id),
            )
            self._db.commit()
            return cursor.rowcount > 0

    def forget(self, person_id: str) -> bool:
        """Delete one identity, all embeddings, aliases, and live bindings."""
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM person_views WHERE person_id=?", (person_id,)
            )
            person_cursor = self._db.execute(
                "DELETE FROM people WHERE person_id=?", (person_id,)
            )
            self._db.commit()
            self._galleries.pop(person_id, None)
            self._last_embedded.pop(person_id, None)
            self._session_tracks = {
                track_id: binding
                for track_id, binding in self._session_tracks.items()
                if binding[0] != person_id
            }
            return bool(cursor.rowcount or person_cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            if self._model is not None:
                try:
                    self._model.stop()
                except Exception:
                    logger.exception("person re-id stop failed")
                self._model = None
            self._db.commit()
            self._db.close()
