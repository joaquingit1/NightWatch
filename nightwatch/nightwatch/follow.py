"""Skill containers: person follow + navigation, hardened for the Go2/Mac.

Changes vs stock (each verified against source July 23):

1. Localization: stock hardcodes QwenVlModel (Alibaba DashScope, needs
   ALIBABA_API_KEY) and seeds the tracker ONLY from that VL bbox, raising
   if the call fails; that is why "follow me" died whenever the VL backend
   was down. Now: generic person queries ("me", "person", "anyone") seed
   straight from the strongest YOLO person detection (local, ~21 ms);
   specific descriptions ("man in blue shirt") go to the shared local
   moondream (VisionService, verified tight boxes) with YOLO as fallback.
   OpenAI models are never used for localization: tested July 23, they
   hallucinate pixel coordinates (confident person bbox on an empty water
   dispenser).
2. Tracker: EdgeTAM hard-requires CUDA; we pre-seed one YoloFollowTracker
   (yolo11n-pose + BoT-SORT, CPU on macOS) so EdgeTAM is never constructed.
   Curious mode asks this module for person observations instead of loading a
   second detector in another worker.
3. Motion is re-armed (BalanceStand + joystick listening) before a follow
   starts; otherwise the firmware silently drops the servo velocities.
4. Follow output is remapped to ``nav_cmd_vel`` in the blueprint.  That puts
   it behind MovementManager, where teleop has unconditional priority.
"""

from collections import deque
from threading import Event, Thread, current_thread
import time
from typing import Any

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.agents.skills.navigation import NavigationSkillContainer
from dimos.agents.skills.person_follow import PersonFollowSkillContainer
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.msgs.sensor_msgs.Image import Image
from dimos.navigation.navigation_spec import NavigationInterfaceSpec
from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec
from dimos.utils.logging_config import setup_logger
from nightwatch.unitree import ensure_motion_ready
from nightwatch.vision import VisionSpec
from nightwatch.identity import AnonymousPersonMemory

logger = setup_logger()

_GENERIC_PERSON_WORDS = frozenset(
    {"person", "me", "them", "anyone", "someone", "somebody", "people", "human", "user", "you"}
)


def _is_generic_person_query(query: str) -> bool:
    words = {w.strip(".,!?'\"").lower() for w in query.split()}
    meaningful = words - {"the", "a", "an", "that", "this", "nearest", "closest", "follow"}
    return bool(meaningful) and meaningful <= _GENERIC_PERSON_WORDS


def _raised_hand(detection: Any, min_score: float = 0.35) -> tuple[bool, str | None]:
    """Recognize a deliberate raised hand from the pose detector's COCO joints.

    This is the latency-critical wave trigger, not a semantic classifier. A
    wrist must be confidently above its elbow and at least near its shoulder.
    Requiring consecutive frames in ``_acquisition_loop`` filters one-frame
    pose noise while still reacting in well under a second.
    """
    keypoints = getattr(detection, "keypoints", None)
    scores = getattr(detection, "keypoint_scores", None)
    if keypoints is None or scores is None or len(keypoints) < 11 or len(scores) < 11:
        return False, None

    # COCO: shoulders 5/6, elbows 7/8, wrists 9/10. Image y increases down.
    for side, shoulder_i, elbow_i, wrist_i in (
        ("left", 5, 7, 9),
        ("right", 6, 8, 10),
    ):
        if min(
            float(scores[shoulder_i]),
            float(scores[elbow_i]),
            float(scores[wrist_i]),
        ) < min_score:
            continue
        shoulder_y = float(keypoints[shoulder_i][1])
        elbow_y = float(keypoints[elbow_i][1])
        wrist_y = float(keypoints[wrist_i][1])
        arm_scale = max(8.0, abs(elbow_y - shoulder_y))
        if wrist_y < elbow_y - 0.10 * arm_scale and wrist_y < shoulder_y + 0.35 * arm_scale:
            return True, side
    return False, None


def _frontal_face_looking(
    detection: Any, min_score: float = 0.5
) -> bool:
    """Cheap, conservative proxy for a person looking toward the camera."""
    keypoints = getattr(detection, "keypoints", None)
    scores = getattr(detection, "keypoint_scores", None)
    if keypoints is None or scores is None or len(keypoints) < 3 or len(scores) < 3:
        return False
    # COCO pose: nose=0, left eye=1, right eye=2. A frontal face generally
    # exposes both eyes with the nose between them. This is not identity or
    # emotion analysis; it is only the low-latency attention gate.
    if min(float(scores[0]), float(scores[1]), float(scores[2])) < min_score:
        return False
    nose_x = float(keypoints[0][0])
    eye_a = float(keypoints[1][0])
    eye_b = float(keypoints[2][0])
    return min(eye_a, eye_b) < nose_x < max(eye_a, eye_b)


class NightwatchPersonFollow(PersonFollowSkillContainer):
    # CPU inference cannot sustain the stock 20 Hz consistently.  At 10 Hz,
    # forty missing detections provide a four-second occlusion grace without
    # ever switching to a different track ID.
    _frequency = 10.0
    _max_lost_frames = 40
    _max_lost_seconds = 4.0
    _max_frame_stale_seconds = 0.5
    _max_linear_speed = 0.25
    _max_angular_speed = 0.6
    # Identity creation/refresh happens during deliberate face observation.
    # A background OSNet sweep duplicated the active detector and degraded
    # navigation while adding no reliable identities in a moving crowd.
    _passive_catalog_enabled = False
    # The acquisition detector is the cheap always-on first stage. It owns one
    # latest-only cache at 4 Hz so supervisor RPCs never run YOLO synchronously
    # and never build a frame backlog while the robot is moving.
    _acquisition_period_s = 0.25
    _acquisition_stale_s = 0.8

    _connection: GO2ConnectionSpec
    _navigation: NavigationInterfaceSpec
    _vision: VisionSpec

    @rpc
    def start(self) -> None:
        super().start()
        if self._tracker is None:
            from nightwatch.tracker import YoloFollowTracker

            self._tracker = YoloFollowTracker()
        self._follow_started_at = 0.0
        self._follow_frames = 0
        self._duplicate_frames = 0
        self._last_inference_ms: float | None = None
        self._last_frame_age_ms: float | None = None
        self._last_follow_bbox: list[float] | None = None
        self._last_follow_twist: dict[str, float] | None = None
        self._last_follow_reason: str | None = None
        self._current_person_id: str | None = None
        self._last_identity_score: float | None = None
        self._identity_mismatches = 0
        self._person_memory = AnonymousPersonMemory()
        self._acquisition_stop = Event()
        self._acquisition_wake = Event()
        self._acquisition_people: list[dict[str, Any]] = []
        self._acquisition_at = 0.0
        self._acquisition_inference_ms: float | None = None
        self._raised_hand_tracks: dict[int, tuple[int, float]] = {}
        # A wave is an event, not a property of the latest video frame. Keep a
        # tiny one-shot queue so the supervisor cannot miss the single
        # ``consecutive == 2`` acquisition frame, and cannot consume that same
        # cached frame repeatedly while the detector is producing the next one.
        self._pending_wave_events: deque[dict[str, Any]] = deque(maxlen=8)
        self._gaze_tracks: dict[int, tuple[float, float]] = {}
        self._acquisition_thread = Thread(
            target=self._acquisition_loop,
            name="Nightwatch-person-acquisition",
            daemon=True,
        )
        self._acquisition_thread.start()
        # Passive cataloguing is deliberately stricter than an explicit
        # remember/follow request. A one-frame detector or re-ID miss must not
        # become a durable new person in a busy venue.
        self._catalog_tracks: dict[int, tuple[int, float]] = {}
        self._catalog_stop = Event()
        self._catalog_thread = None
        if self._passive_catalog_enabled:
            self._catalog_thread = Thread(
                target=self._catalog_loop,
                name="Nightwatch-person-catalog",
                daemon=True,
            )
            self._catalog_thread.start()

    @rpc
    def stop(self) -> None:
        acquisition_stop = getattr(self, "_acquisition_stop", None)
        if acquisition_stop is not None:
            acquisition_stop.set()
        acquisition_wake = getattr(self, "_acquisition_wake", None)
        if acquisition_wake is not None:
            acquisition_wake.set()
        acquisition_thread = getattr(self, "_acquisition_thread", None)
        if (
            acquisition_thread is not None
            and acquisition_thread is not current_thread()
        ):
            acquisition_thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        self._acquisition_thread = None
        catalog_stop = getattr(self, "_catalog_stop", None)
        if catalog_stop is not None:
            catalog_stop.set()
        catalog_thread = getattr(self, "_catalog_thread", None)
        if catalog_thread is not None and catalog_thread is not current_thread():
            catalog_thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        self._catalog_thread = None
        memory = getattr(self, "_person_memory", None)
        try:
            # Parent stops and joins the follow loop before the identity
            # database/model are closed underneath it.
            super().stop()
        finally:
            if memory is not None:
                memory.close()
                self._person_memory = None

    def _on_color_image(self, image: Image) -> None:
        super()._on_color_image(image)
        wake = getattr(self, "_acquisition_wake", None)
        if wake is not None:
            wake.set()

    def _acquisition_loop(self) -> None:
        last_frame_key: tuple[float, int] | None = None
        last_inference_at = 0.0
        while not self._acquisition_stop.is_set():
            self._acquisition_wake.wait(self._acquisition_period_s)
            self._acquisition_wake.clear()
            if self._acquisition_stop.is_set() or self.is_following():
                continue
            now = time.monotonic()
            if now - last_inference_at < self._acquisition_period_s:
                continue
            with self._lock:
                image = self._latest_image
                tracker = self._tracker
            if image is None or tracker is None:
                continue
            frame_key = (float(image.ts or 0.0), id(image))
            if frame_key == last_frame_key:
                continue
            last_frame_key = frame_key
            last_inference_at = now
            started = time.perf_counter()
            try:
                detections = tracker.detect_people(image).detections
                present_tracks: set[int] = set()
                people = []
                wave_events: list[dict[str, Any]] = []
                for detection in detections:
                    track_id = int(detection.track_id)
                    present_tracks.add(track_id)
                    raised, side = _raised_hand(detection)
                    prior_count, prior_at = self._raised_hand_tracks.get(
                        track_id, (0, 0.0)
                    )
                    consecutive = (
                        prior_count + 1
                        if raised and now - prior_at <= self._acquisition_stale_s
                        else (1 if raised else 0)
                    )
                    self._raised_hand_tracks[track_id] = (consecutive, now)
                    looking = _frontal_face_looking(detection)
                    gaze_started, gaze_at = self._gaze_tracks.get(
                        track_id, (now, 0.0)
                    )
                    if looking:
                        if now - gaze_at > self._acquisition_stale_s:
                            gaze_started = now
                        self._gaze_tracks[track_id] = (gaze_started, now)
                    else:
                        self._gaze_tracks.pop(track_id, None)
                    observation = self._observation_from_detection(image, detection)
                    observation["raised_hand"] = raised
                    observation["raised_hand_side"] = side
                    # This is an edge-triggered gesture event, not a pose
                    # state.  The old ``>= 2`` stayed true for as long as an
                    # arm was raised, so every supervisor poll could cancel
                    # navigation and start another Hello routine.  Emit once
                    # when the pose first becomes stable; the arm must lower
                    # (or the track disappear) before it can fire again.
                    observation["waving"] = consecutive == 2
                    observation["looking_at_camera"] = looking
                    observation["attention_s"] = (
                        max(0.0, now - gaze_started) if looking else 0.0
                    )
                    if observation["waving"]:
                        event = dict(observation)
                        event["_wave_event_at"] = now
                        wave_events.append(event)
                    people.append(observation)
                self._raised_hand_tracks = {
                    track_id: state
                    for track_id, state in self._raised_hand_tracks.items()
                    if track_id in present_tracks
                    and now - state[1] <= self._acquisition_stale_s
                }
                self._gaze_tracks = {
                    track_id: state
                    for track_id, state in self._gaze_tracks.items()
                    if track_id in present_tracks
                    and now - state[1] <= self._acquisition_stale_s
                }
            except Exception:
                logger.exception("person acquisition inference failed")
                continue
            with self._lock:
                self._acquisition_people = people
                self._acquisition_at = time.monotonic()
                self._pending_wave_events.extend(wave_events)
                self._acquisition_inference_ms = round(
                    (time.perf_counter() - started) * 1000.0, 1
                )

    def _observation_from_detection(
        self, image: Image, detection: Any
    ) -> dict[str, Any]:
        x1, y1, x2, y2 = (float(value) for value in detection.bbox)
        memory = getattr(self, "_person_memory", None)
        observation = {
            "bbox": [x1, y1, x2, y2],
            "frame_width": float(image.width),
            "frame_height": float(image.height),
            "offset": ((x1 + x2) / 2.0 - image.width / 2.0)
            / max(1.0, image.width / 2.0),
            "height_frac": (y2 - y1) / max(1.0, float(image.height)),
            "track_id": int(detection.track_id),
            "frame_ts": float(image.ts or 0.0),
            "person_id": (
                memory.session_person(int(detection.track_id))
                if memory is not None
                else None
            ),
        }
        keypoints = getattr(detection, "keypoints", None)
        keypoint_scores = getattr(detection, "keypoint_scores", None)
        if keypoints is not None and keypoint_scores is not None:
            observation["keypoints"] = [
                [float(point[0]), float(point[1])] for point in keypoints
            ]
            observation["keypoint_scores"] = [
                float(score) for score in keypoint_scores
            ]
        return observation

    @rpc
    def follow_status(self) -> dict[str, Any]:
        """Runtime evidence for camera, detector, identity, and control health."""
        tracker = self._tracker
        tracker_status = tracker.status() if tracker is not None else {}
        return {
            "following": self.is_following(),
            "started_at": self._follow_started_at or None,
            "processed_frames": self._follow_frames,
            "duplicate_frames_skipped": self._duplicate_frames,
            "last_inference_ms": self._last_inference_ms,
            "last_frame_age_ms": self._last_frame_age_ms,
            "last_bbox": self._last_follow_bbox,
            "last_twist": self._last_follow_twist,
            "last_stop_reason": self._last_follow_reason,
            "person_id": self._current_person_id,
            "identity_score": self._last_identity_score,
            "acquisition_age_ms": (
                max(0.0, time.monotonic() - self._acquisition_at) * 1000.0
                if self._acquisition_at
                else None
            ),
            "acquisition_people": len(self._acquisition_people),
            "acquisition_inference_ms": self._acquisition_inference_ms,
            **tracker_status,
        }

    @rpc
    def is_following(self) -> bool:
        """True while the background follow loop is running (IdleBehavior
        polls this so curious-mode motion never fights the follow servo)."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _yolo_person_detection(self, image: Image) -> Any | None:
        """Strongest person in view by area x centrality, from the tracker's
        own YOLO detector (local; no VL round trip)."""
        tracker = self._tracker
        if tracker is None:
            return None
        detections = tracker.detect_people(image).detections
        if not detections:
            return None
        width = float(image.data.shape[1])

        def score(det: Any) -> float:
            x1, y1, x2, y2 = det.bbox
            area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
            centrality = 1.0 - abs((x1 + x2) / 2.0 - width / 2.0) / (width / 2.0)
            return area * (0.5 + 0.5 * centrality)

        return max(detections, key=score)

    @rpc
    def observe_person(self, min_height_frac: float = 0.0) -> dict[str, Any] | None:
        """Return the most prominent visible person from the shared detector.

        The result is intentionally small and serializable so IdleBehavior can
        use it over RPC without subscribing to or copying camera frames.
        """
        with self._lock:
            cached_people = list(getattr(self, "_acquisition_people", []))
            acquisition_at = float(getattr(self, "_acquisition_at", 0.0))
            image = self._latest_image
        if acquisition_at:
            if time.monotonic() - acquisition_at > self._acquisition_stale_s:
                return None
            candidates = [
                person
                for person in cached_people
                if float(person["height_frac"]) >= min_height_frac
            ]
            if not candidates:
                return None
            return max(
                candidates,
                key=lambda person: (
                    (person["bbox"][2] - person["bbox"][0])
                    * (person["bbox"][3] - person["bbox"][1])
                    * (1.0 - 0.35 * abs(float(person["offset"])))
                ),
            )

        # Compatibility fallback for isolated unit tests and the first few
        # milliseconds of startup before the acquisition thread has a result.
        tracker = self._tracker
        if image is None or tracker is None or image.height <= 0 or image.width <= 0:
            return None
        detections = tracker.detect_people(image).detections
        candidates = [
            detection
            for detection in detections
            if (detection.bbox[3] - detection.bbox[1]) / float(image.height)
            >= min_height_frac
        ]
        if not candidates:
            return None

        def score(det: Any) -> float:
            x1, y1, x2, y2 = det.bbox
            area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
            centrality = 1.0 - abs((x1 + x2) / 2.0 - image.width / 2.0) / (
                image.width / 2.0
            )
            return area * (0.5 + 0.5 * centrality)

        best = max(candidates, key=score)
        return self._observation_from_detection(image, best)

    @rpc
    def observe_waving_person(
        self, min_height_frac: float = 0.10
    ) -> dict[str, Any] | None:
        """Consume one raised-hand event without another model.

        The acquisition cache is intentionally retained for ordinary person
        observations, but a gesture must be consumed exactly once. Otherwise
        several supervisor polls can see the same detector frame and queue
        several physical Hello routines.
        """
        now = time.monotonic()
        with self._lock:
            pending = getattr(self, "_pending_wave_events", None)
            if pending is None:
                # Compatibility for old serialized workers and isolated tests.
                pending = deque(maxlen=8)
                self._pending_wave_events = pending
            candidates = [
                event
                for event in pending
                if now - float(event.get("_wave_event_at", 0.0))
                <= self._acquisition_stale_s
                and float(event.get("height_frac", 0.0)) >= min_height_frac
            ]
            pending.clear()
        if not candidates:
            return None
        result = max(
            candidates,
            key=lambda person: (
                float(person.get("height_frac", 0.0)),
                -abs(float(person.get("offset", 0.0))),
            ),
        )
        result = dict(result)
        result.pop("_wave_event_at", None)
        return result

    @rpc
    def observe_people_pose(
        self, min_height_frac: float = 0.08
    ) -> list[dict[str, Any]]:
        """Expose cached boxes and pose joints to other robot behaviors.

        Intervention used to instantiate a second yolo11n-pose model in a
        different process. Besides doubling CPU use, the second BoT-SORT
        timeline gave the same visitor unrelated track IDs. This RPC keeps one
        detector and one identity timeline for acquisition, wave-back, follow,
        and intervention.
        """
        with self._lock:
            people = list(getattr(self, "_acquisition_people", []))
            acquisition_at = float(getattr(self, "_acquisition_at", 0.0))
        if (
            not acquisition_at
            or time.monotonic() - acquisition_at > self._acquisition_stale_s
        ):
            return []
        return [
            person
            for person in people
            if float(person.get("height_frac", 0.0)) >= min_height_frac
        ]

    @rpc
    def observe_attentive_person(
        self,
        min_height_frac: float = 0.12,
        min_attention_s: float = 5.0,
    ) -> dict[str, Any] | None:
        """Return someone continuously facing the camera for the given dwell."""
        people = self.observe_people_pose(min_height_frac)
        candidates = [
            person
            for person in people
            if person.get("looking_at_camera")
            and float(person.get("attention_s", 0.0)) >= min_attention_s
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda person: (
                float(person.get("attention_s", 0.0)),
                float(person.get("height_frac", 0.0)),
                -abs(float(person.get("offset", 0.0))),
            ),
        )

    def _catalog_loop(self) -> None:
        """Remember passers-by sparsely without adding camera backpressure."""
        while not self._catalog_stop.wait(8.0):
            if self.is_following():
                continue
            with self._lock:
                image = self._latest_image
                tracker = self._tracker
            memory = getattr(self, "_person_memory", None)
            if image is None or tracker is None or memory is None:
                continue
            try:
                now = time.monotonic()
                detections = tracker.detect_people(image).detections
                candidates = sorted(
                    detections,
                    key=lambda d: (d.bbox[2] - d.bbox[0])
                    * (d.bbox[3] - d.bbox[1]),
                    reverse=True,
                )[:3]
                for detection in candidates:
                    if (detection.bbox[3] - detection.bbox[1]) < image.height * 0.30:
                        continue
                    track_id = int(detection.track_id)
                    # Passive scans may refresh an existing identity, but must
                    # never mint one. In a crowded moving-camera run, recycled
                    # BoT-SORT IDs created 215 one-view "people" and made every
                    # later match ambiguous. New identities are created only
                    # after the supervisor has held one stable person in view.
                    person_id, _ = memory.identify(
                        image,
                        tuple(float(v) for v in detection.bbox),
                        track_id,
                        allow_new=False,
                    )
                    if person_id is None:
                        self._catalog_tracks[track_id] = (1, now)
                    else:
                        self._catalog_tracks.pop(track_id, None)
                self._catalog_tracks = {
                    track_id: state
                    for track_id, state in self._catalog_tracks.items()
                    if now - state[1] <= 20.0
                }
            except Exception:
                logger.exception("background person catalog failed")

    @skill
    def list_remembered_people(
        self, active_within_minutes: float = 0.0
    ) -> list[dict[str, Any]]:
        """List anonymous local person identities and their sighting counts."""
        memory = getattr(self, "_person_memory", None)
        if memory is None:
            return []
        return memory.list_people(max(0.0, active_within_minutes) * 60.0)

    @skill
    def person_memory_status(self) -> dict[str, Any]:
        """Return bounded-gallery health without exposing image crops."""
        memory = getattr(self, "_person_memory", None)
        people = memory.list_people() if memory is not None else []
        return {
            "remembered_people": len(people),
            "current_person_id": self._current_person_id,
            "identity_score": self._last_identity_score,
            "following": self.is_following(),
        }

    @rpc
    def identify_visible_track(self, track_id: int) -> dict[str, Any] | None:
        """Bind one supervisor-confirmed body track to an anonymous identity.

        This is intentionally separate from passive cataloguing: the caller
        must first observe the same live track across multiple frames while
        navigation is stopped.
        """
        with self._lock:
            image = self._latest_image
            tracker = self._tracker
        memory = getattr(self, "_person_memory", None)
        if image is None or tracker is None or memory is None:
            return None
        detections = tracker.detect_people(image).detections
        detection = next(
            (item for item in detections if int(item.track_id) == int(track_id)),
            None,
        )
        if detection is None:
            return None
        person_id, confidence = memory.identify(
            image,
            tuple(float(v) for v in detection.bbox),
            int(track_id),
            allow_new=True,
        )
        if person_id is None:
            return None
        return {
            "person_id": person_id,
            "confidence": float(confidence),
            "track_id": int(track_id),
            "bbox": [float(value) for value in detection.bbox],
        }

    @skill
    def remember_visible_person(self) -> str:
        """Assign/recover an anonymous ID for the most prominent visible person."""
        with self._lock:
            image = self._latest_image
        detection = self._yolo_person_detection(image) if image is not None else None
        memory = getattr(self, "_person_memory", None)
        if image is None or detection is None or memory is None:
            return "No person is clearly visible."
        person_id, confidence = memory.identify(
            image,
            tuple(float(v) for v in detection.bbox),
            int(detection.track_id),
        )
        return (
            f"Remembered {person_id} (match confidence {confidence:.2f})."
            if person_id
            else "The visible person could not be identified reliably."
        )

    @skill
    def name_remembered_person(
        self, person_id: str, name: str, consent_confirmed: bool = False
    ) -> str:
        """Add a human-readable alias only after that person explicitly consents."""
        memory = getattr(self, "_person_memory", None)
        if memory is None:
            return "Person memory is unavailable."
        if not consent_confirmed:
            return "Alias not stored: explicit consent must be confirmed."
        return (
            f"Stored the consented alias '{name.strip()}' for {person_id}."
            if memory.set_alias(person_id, name, True)
            else f"No remembered identity named {person_id}."
        )

    @skill
    def forget_remembered_person(self, person_id: str) -> str:
        """Permanently delete one anonymous identity and every gallery view."""
        memory = getattr(self, "_person_memory", None)
        if memory is None:
            return "Person memory is unavailable."
        if self._current_person_id == person_id and self.is_following():
            return "Stop following this person before deleting their identity."
        if memory.forget(person_id):
            if self._current_person_id == person_id:
                self._current_person_id = None
            return f"Deleted {person_id} and all of its local identity data."
        return f"No remembered identity named {person_id}."

    @skill(uses=[CAP_MOVEMENT], lifecycle="background")
    def follow_person(
        self,
        query: str,
        initial_bbox: list[float] | None = None,
        initial_image: str | None = None,
        initial_track_id: int | None = None,
        person_id: str | None = None,
        identify_during_follow: bool = True,
    ) -> str:
        """Follow a person matching the given description using visual servoing.

        The robot will continuously track and follow the person, while keeping
        them centered in the camera view.

        Args:
            query: Description of the person to follow (e.g., "man with blue shirt")
            initial_bbox: Optional pre-computed bounding box [x1, y1, x2, y2].
            initial_image: Optional base64-encoded JPEG of the frame on which
                initial_bbox was detected.
            initial_track_id: Optional YOLO/BoT-SORT identity from
                observe_person. This is the preferred curious-mode seed.
            person_id: Optional persistent anonymous identity. If supplied,
                refuse to follow unless that same person is visible.
            identify_during_follow: Whether to run periodic persistent
                re-identification. False for the short acquisition approach,
                where body-track continuity is sufficient and latency matters.

        Returns:
            Status message indicating the result of the following action.

        Example:
            follow_person("man with blue shirt")
            follow_person("person in the doorway")
        """
        logger.info(
            "follow_person entered",
            query=query,
            has_bbox=initial_bbox is not None,
            track_id=initial_track_id,
            person_id=person_id,
        )
        with self._lock:
            image = self._latest_image
            tracker = self._tracker
        if image is None or tracker is None:
            return "No image available to detect person."

        if person_id is None:
            self._current_person_id = None
            self._last_identity_score = None

        if person_id:
            memory = getattr(self, "_person_memory", None)
            if memory is None:
                return "Persistent person memory is unavailable."
            detections = tracker.detect_people(image).detections
            detected, identity_score = memory.find_visible(person_id, image, detections)
            self._last_identity_score = identity_score
            if detected is None:
                return (
                    f"Could not uniquely verify {person_id} in view "
                    f"(best score {identity_score:.2f}); refusing to switch people."
                )
            initial_bbox = [float(v) for v in detected.bbox]
            initial_track_id = int(detected.track_id)
            self._current_person_id = person_id
        elif initial_bbox is None:
            logger.info("follow_person has image; running local detection")

            bbox: tuple[float, float, float, float] | None = None
            if not _is_generic_person_query(query):
                try:
                    boxes = self._vision.moondream_detect(query)
                    if boxes:
                        # Largest match: the most prominent person fitting
                        # the description.
                        bbox = tuple(
                            max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
                        )
                except Exception:
                    logger.exception("moondream person detection failed; falling back to YOLO")
            if bbox is None:
                detected = self._yolo_person_detection(image)
                if detected is not None:
                    bbox = tuple(float(v) for v in detected.bbox)
                    initial_track_id = int(detected.track_id)
            if bbox is None:
                return f"Could not find '{query}' — no person visible right now."
            initial_bbox = [float(v) for v in bbox]

        # Follow and the navigation planner both publish on nav_cmd_vel. Stop
        # any goal first; MovementManager then arbitrates this stream against
        # higher-priority manual teleop.
        try:
            self._navigation.cancel_goal()
        except Exception:
            logger.exception("Could not cancel navigation before person follow")

        connection = getattr(self, "_connection", None)
        if connection is not None:
            ensure_motion_ready(connection)
        logger.info(
            "follow_person seeding tracker",
            bbox=[round(v) for v in initial_bbox],
            track_id=initial_track_id,
        )

        # Reimplement the small stock startup wrapper so a BoT-SORT identity
        # observed by curious mode can be locked directly. Re-running YOLO on a
        # later frame and matching by IoU made close, fast-moving people fail
        # before the follow loop even began.
        self._stop_following()
        if self._thread is not None:
            self._thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
            self._thread = None
        self._should_stop.clear()
        self.start_tool("follow_person")
        launched = False
        try:
            if initial_track_id is not None and initial_track_id >= 0:
                tracker.lock_track(initial_track_id)
                initial_detections = tracker.process_image(image)
            else:
                initial_detections = tracker.init_track(
                    image=image,
                    box=initial_bbox,
                    obj_id=1,
                )
            if len(initial_detections) == 0:
                self.cmd_vel.publish(self._zero_twist())
                return f"YOLO could not lock onto '{query}'."

            self._thread = Thread(
                target=self._follow_loop,
                args=(tracker, query, identify_during_follow),
                name="Nightwatch-person-follow",
                daemon=True,
            )
            self._follow_started_at = time.time()
            self._follow_frames = 0
            self._duplicate_frames = 0
            self._last_follow_reason = None
            self._identity_mismatches = 0
            self._thread.start()
            launched = True
            return (
                "Found the person. Starting to follow. Call stop_following "
                "to stop."
            )
        finally:
            if not launched:
                self.stop_tool("follow_person")

    @staticmethod
    def _zero_twist():
        from dimos.msgs.geometry_msgs.Twist import Twist

        return Twist.zero()

    @skill
    def stop_following(self) -> str:
        """Stop following and unconditionally close its capability stream."""
        self._stop_following()
        self.cmd_vel.publish(self._zero_twist())
        if self._thread is not None:
            self._thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
            self._thread = None
        self.stop_tool("follow_person")
        return "Stopped following."

    def _follow_loop(
        self,
        tracker: Any,
        query: str,
        identify_during_follow: bool = True,
    ) -> None:
        """Track on fresh camera frames, not on a synthetic timer.

        The stock loop calls the stateful tracker at 20 Hz even when the camera
        has not produced a new frame. Repeated identical frames advance
        BoT-SORT and its loss counters, which was the main cause of 4-7 second
        follow failures in the live logs.
        """
        period = 1.0 / self._frequency
        last_frame_key: tuple[float, int] | None = None
        last_fresh_at = time.monotonic()
        lost_since: float | None = None
        last_telemetry_at = 0.0
        last_identity_at = 0.0

        while not self._should_stop.wait(period):
            with self._lock:
                latest_image = self._latest_image
            if latest_image is None:
                continue

            frame_key = (float(latest_image.ts or 0.0), id(latest_image))
            now = time.monotonic()
            if frame_key == last_frame_key:
                self._duplicate_frames += 1
                if now - last_fresh_at >= self._max_frame_stale_seconds:
                    self.cmd_vel.publish(self._zero_twist())
                continue
            last_frame_key = frame_key
            last_fresh_at = now

            wall_age = (
                max(0.0, time.time() - float(latest_image.ts)) * 1000.0
                if latest_image.ts
                else None
            )
            inference_started = time.perf_counter()
            detections = tracker.process_image(latest_image)
            self._last_inference_ms = round(
                (time.perf_counter() - inference_started) * 1000.0, 1
            )
            self._last_frame_age_ms = round(wall_age, 1) if wall_age is not None else None
            self._follow_frames += 1

            if len(detections) == 0:
                self.cmd_vel.publish(self._zero_twist())
                lost_since = lost_since or now
                if now - lost_since >= self._max_lost_seconds:
                    self._last_follow_reason = "lost track of the person"
                    self._send_stop_reason(query, self._last_follow_reason)
                    return
                continue

            lost_since = None
            best = max(detections.detections, key=lambda d: d.bbox_2d_volume())
            self._last_follow_bbox = [round(float(v), 1) for v in best.bbox]
            memory = self._person_memory
            if (
                identify_during_follow
                and memory is not None
                and now - last_identity_at >= 4.0
            ):
                last_identity_at = now
                bbox = tuple(float(v) for v in best.bbox)
                if self._current_person_id is None:
                    person_id, identity_score = memory.identify(
                        latest_image, bbox, int(best.track_id)
                    )
                    self._current_person_id = person_id
                    self._last_identity_score = identity_score
                else:
                    identity_score = memory.verify(
                        self._current_person_id, latest_image, bbox
                    )
                    self._last_identity_score = identity_score
                    if identity_score < max(
                        0.55, memory.match_threshold - 0.10
                    ):
                        self._identity_mismatches += 1
                    else:
                        self._identity_mismatches = 0
                    if self._identity_mismatches >= 2:
                        self.cmd_vel.publish(self._zero_twist())
                        self._last_follow_reason = (
                            "persistent identity verification rejected a target switch"
                        )
                        self._send_stop_reason(query, self._last_follow_reason)
                        return
            if self.config.use_3d_navigation:
                with self._lock:
                    pointcloud = self._latest_pointcloud
                if pointcloud is None:
                    self._last_follow_reason = "no pointcloud available for 3D navigation"
                    self._send_stop_reason(query, self._last_follow_reason)
                    return
                twist = self._detection_navigation.compute_twist_for_detection_3d(
                    pointcloud, best, latest_image
                )
                if twist is None:
                    self._last_follow_reason = "3D navigation failed"
                    self._send_stop_reason(query, self._last_follow_reason)
                    return
            else:
                twist = self._visual_servo.compute_twist(best.bbox, latest_image.width)

            # The stock 0.5 m/s command is too fast for visual-only following
            # in a crowded venue. Bound both axes while retaining controller
            # direction and distance behavior.
            linear_x = max(
                -self._max_linear_speed,
                min(self._max_linear_speed, float(twist.linear.x)),
            )
            angular_z = max(
                -self._max_angular_speed,
                min(self._max_angular_speed, float(twist.angular.z)),
            )
            bounded = type(twist)(
                linear=type(twist.linear)(linear_x, 0.0, 0.0),
                angular=type(twist.angular)(0.0, 0.0, angular_z),
            )
            self._last_follow_twist = {
                "linear_x": round(linear_x, 3),
                "angular_z": round(angular_z, 3),
            }
            self.cmd_vel.publish(bounded)

            if now - last_telemetry_at >= 1.0:
                last_telemetry_at = now
                logger.info(
                    "person follow telemetry",
                    frame_age_ms=self._last_frame_age_ms,
                    inference_ms=self._last_inference_ms,
                    bbox=self._last_follow_bbox,
                    twist=self._last_follow_twist,
                    tracker=tracker.status(),
                )

        self._last_follow_reason = "it was requested to stop following"
        self._send_stop_reason(query, self._last_follow_reason)


class NightwatchNavigation(NavigationSkillContainer):
    """NavigationSkillContainer also hardcodes DashScope Qwen for its
    navigate-to-object branch. OpenAI here is a stopgap: that branch is
    rarely reached (tagged locations and go_to_visible_object come first)
    and OpenAI boxes are unreliable, but Qwen would raise outright without
    ALIBABA_API_KEY."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        from dimos.models.vl.openai import OpenAIVlModel

        self._vl_model = OpenAIVlModel()
