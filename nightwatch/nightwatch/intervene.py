"""potential_detected: the Nightwatch "suspicious subject" protocol.

When a potential subject is found the robot stands next to them and waves,
raising its camera onto their face, until they look back OR it has waved four
times, whichever comes first. The waves must be REAL: a gesture the firmware
silently drops (because the dog is not in a locomotion-ready FSM) does not
count, so every wave is re-armed and verified against the firmware ack the way
``nightwatch.unitree.wave_hello`` does.

While the protocol runs it owns motion through a priority-40 ``intervene``
behavior lease on the CuriositySupervisor. That lease sits above autonomous
curiosity and the curious person-follow, so curious wandering, dog gestures,
and close-person follow are all suppressed for the duration (the supervisor
arbitration yields to the top lease, and the lease is surfaced as
``RobotActivity(behavior=intervene, owner="intervention")`` on the same
activity stream the operator console already reads, so the event is visible on
the dashboard without a second writer of the single-writer activity contract).

The protocol loop is factored so it is testable without threads or hardware:
``run_wave_protocol`` takes injected waver/gaze/renew callables, and
``_run_protocol`` takes injected find/approach/wave/gaze/pitch/lease callables
so the full lifecycle (including the always-restore-and-release finally path)
can be exercised with plain fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Any, Callable, Protocol

from reactivex.disposable import Disposable

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.agents.skills.speak_skill_spec import SpeakSkillSpec
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.CameraInfo import CameraInfo
from dimos.msgs.sensor_msgs.Image import Image
from dimos.navigation.base import NavigationState
from dimos.navigation.navigation_spec import NavigationInterfaceSpec
from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec
from dimos.spec.utils import Spec
from dimos.utils.logging_config import setup_logger
from nightwatch.unitree import ensure_motion_ready, set_body_pitch, wave_hello

logger = setup_logger()

# COCO/yolo11n-pose keypoint indices used by the frontal-face heuristic.
_NOSE = 0
_LEFT_EYE = 1
_RIGHT_EYE = 2


class InterventionLeaseSpec(Spec, Protocol):
    """The slice of CuriositySupervisor the protocol needs for motion authority."""

    def acquire_behavior(
        self, owner: str, behavior: str, priority: int, ttl_s: float, reason: str
    ) -> dict[str, Any]: ...
    def renew_behavior(self, lease_id: str, ttl_s: float) -> bool: ...
    def release_behavior(self, lease_id: str) -> bool: ...


class FollowControlSpec(Spec, Protocol):
    """The slice of the person-follow container needed to stand it down.

    A running ``follow_person`` holds the MCP movement capability, and the
    dimos MCP server only auto-preempts ``begin_exploration``/``start_patrol``.
    The protocol must therefore stop an active follow itself before it can own
    motion.
    """

    def is_following(self) -> bool: ...
    def stop_following(self) -> str: ...


@dataclass(frozen=True, slots=True)
class InterventionResult:
    found: bool
    waves_performed: int
    blocked_attempts: int
    subject_looked: bool
    duration_s: float
    message: str
    # Whether a roughly-frontal face was seen before waving (after the
    # face-seeking arc). False means the dog waved from wherever it ended up.
    face_found: bool = False


def frontal_face_looking(
    keypoints: Any, keypoint_scores: Any, conf_threshold: float = 0.5
) -> bool:
    """Roughly-frontal-face gaze heuristic from pose keypoints.

    The subject is treated as looking at the robot when the nose and BOTH eyes
    are confidently detected and the two eye x-positions straddle the nose
    x-position (a face turned away hides one eye or pushes the nose to one
    side). This is deliberately a geometric heuristic, not gaze estimation.
    """
    try:
        nose_s = float(keypoint_scores[_NOSE])
        left_s = float(keypoint_scores[_LEFT_EYE])
        right_s = float(keypoint_scores[_RIGHT_EYE])
    except (IndexError, TypeError, ValueError):
        return False
    if nose_s < conf_threshold or left_s < conf_threshold or right_s < conf_threshold:
        return False
    try:
        nose_x = float(keypoints[_NOSE][0])
        left_x = float(keypoints[_LEFT_EYE][0])
        right_x = float(keypoints[_RIGHT_EYE][0])
    except (IndexError, TypeError, ValueError):
        return False
    lo, hi = (left_x, right_x) if left_x <= right_x else (right_x, left_x)
    return lo < nose_x < hi


def check_gaze(
    sample: Callable[[], bool],
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    window_s: float,
    frames_required: int,
    poll_s: float,
) -> bool:
    """True once ``sample`` reports a frontal face on N consecutive frames.

    ``sample`` returns whether the subject looks frontal on the current frame.
    A single non-frontal frame resets the streak, so a glance does not qualify;
    the subject must hold a frontal face for ``frames_required`` frames within
    the ``window_s`` window.
    """
    deadline = clock() + window_s
    consecutive = 0
    while clock() < deadline:
        if sample():
            consecutive += 1
            if consecutive >= frames_required:
                return True
        else:
            consecutive = 0
        sleep(poll_s)
    return False


def run_wave_protocol(
    *,
    waver: Callable[[], bool],
    rearm: Callable[[], Any],
    gaze_check: Callable[[], bool],
    renew_lease: Callable[[], Any],
    raise_pitch: Callable[[], Any] = lambda: None,
    max_waves: int = 4,
    max_attempts: int = 8,
) -> tuple[int, int, bool]:
    """Wave up to ``max_waves`` REAL times or until the subject looks back.

    Returns ``(waves_performed, total_attempts, subject_looked)``.

    - ``waver()`` performs one wave and returns True only for a REAL
      (firmware-acknowledged) wave; a blocked wave returns False, is followed
      by ``rearm()``, and never counts toward ``waves_performed``.
    - ``raise_pitch()`` re-applies the camera up-tilt after every real wave.
      The Hello gesture resets the body pose, so a single up-front tilt would
      not survive the first wave; re-applying keeps the subject's face in view
      for the facial-analysis pipeline. It must be a no-fail callable (log and
      continue on failure) so a rejected re-apply never aborts the protocol.
    - ``total_attempts`` (real + blocked) is capped at ``max_attempts`` so a
      permanently-blocked firmware can never spin forever.
    - After each real wave (below the cap) ``gaze_check()`` runs; the first
      time it is True the protocol performs exactly ONE more real wave (which
      counts toward the cap) and stops.
    - The lease is renewed once per attempt so a slow wave sequence never lets
      the behavior lease expire underneath the protocol.
    """
    waves = 0
    attempts = 0
    looked = False
    while waves < max_waves and attempts < max_attempts:
        renew_lease()
        attempts += 1
        if not waver():
            # Blocked by firmware: re-arm and retry; this attempt is not a wave.
            rearm()
            continue
        waves += 1
        # Re-tilt the camera after the wave reset the body pose.
        raise_pitch()
        if looked:
            # This was the promised single final wave after the subject looked.
            break
        if waves >= max_waves:
            break
        if gaze_check():
            looked = True
            # The next successful wave becomes the final wave, then we stop.
    return waves, attempts, looked


def run_face_seek(
    *,
    face_check: Callable[[], bool],
    arc_step: Callable[[int], Any],
    max_hops: int = 3,
    timeout_s: float = 30.0,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, int]:
    """Find a frontal face, arcing around the subject until one is seen.

    Returns ``(face_found, hops_taken)``.

    - ``face_check()`` reports whether a roughly-frontal face was seen (a short
      streak over ~2 s in the real wiring). It runs once at the standoff point
      first, so a subject already facing the dog costs zero hops.
    - When only a back/side view is visible, ``arc_step(i)`` repositions the dog
      to the next waypoint on a circle around the subject; we re-check after
      each hop and stop the instant a face appears.
    - The whole phase is bounded by ``timeout_s``. An ``arc_step`` failure (for
      example a rejected goal) is swallowed so it never aborts the protocol;
      the dog just waves from wherever it is.
    """

    def _face() -> bool:
        try:
            return bool(face_check())
        except Exception:
            logger.warning(
                "intervention face check failed; treating as no face",
                exc_info=True,
            )
            return False

    deadline = clock() + timeout_s
    if _face():
        return True, 0
    hops = 0
    while hops < max_hops and clock() < deadline:
        try:
            arc_step(hops)
        except Exception:
            logger.warning("intervention arc step failed; continuing", exc_info=True)
        hops += 1
        if clock() >= deadline:
            break
        if _face():
            return True, hops
    return False, hops


class InterventionConfig(ModuleConfig):
    camera_info: CameraInfo
    # Front camera height above the floor while standing (matches goto.py).
    camera_height_m: float = 0.33
    # Stand roughly this far from the subject before waving.
    standoff_m: float = 1.2
    # Sanity cap on an unprojected subject distance.
    max_goal_m: float = 8.0
    approach_timeout_s: float = 20.0
    # Behavior lease: priority 40 (above curiosity/follow, below operator and
    # safety), renewed each wave attempt; BRIDGE.md documents this priority.
    lease_priority: int = 40
    lease_ttl_s: float = 15.0
    body_pitch_rad: float = 0.35
    max_waves: int = 4
    max_wave_attempts: int = 8
    wave_settle_s: float = 0.4
    # Gaze window after each wave: N consecutive frontal frames within window.
    gaze_window_s: float = 3.0
    gaze_frames_required: int = 3
    gaze_poll_s: float = 0.15
    # In-place scan for a subject: 60 deg steps, up to a full turn.
    scan_step_deg: float = 60.0
    scan_max_steps: int = 6
    scan_turn_rad_s: float = 0.5
    scan_settle_s: float = 0.6
    person_min_height_frac: float = 0.20
    keypoint_conf: float = 0.5
    # After stopping an active follow, poll for the movement capability to
    # release before taking the lease.
    follow_release_timeout_s: float = 2.0
    follow_release_poll_s: float = 0.1
    # Speech: greet the subject at the start and ask them to look at the dog
    # (which also cues the gaze check). Best-effort; a stack with no speaker
    # still runs the protocol unchanged.
    greeting_text: str = "你好你好！我是守夜犬。请看看我这边好吗？"
    # A one-off friendly, non-judgmental Chinese remark about something visible
    # about the person, produced by the OpenAI vision model in parallel with
    # the approach and spoken on arrival before the first wave.
    personalized_comment: bool = True
    comment_timeout_s: float = 6.0
    comment_model: str = "gpt-4o-mini"
    comment_max_width: int = 768
    comment_prompt: str = (
        "用一句简短、友好、不带评判的中文，评论这个人身上一个显眼的细节"
        "（例如衣服的颜色、配饰或随身物品）。绝对不要提到身材、体型、年龄、"
        "性别、外貌或任何敏感话题。只输出这一句话，不要解释。"
    )
    # Face-seeking arc: never wave at someone's back. After arriving, look for
    # a frontal face; if only a back/side view is visible, arc around the
    # person on a circle to find their face before waving.
    face_seek_window_s: float = 2.0
    arc_radius_m: float = 1.5
    arc_step_deg: float = 72.0
    arc_max_hops: int = 3
    arc_timeout_s: float = 30.0
    arc_hop_timeout_s: float = 10.0
    # Constant arc direction (+1 counter-clockwise). This module has no costmap
    # signal, so the direction is fixed rather than free-space aware.
    arc_direction: float = 1.0


class InterventionSkill(Module):
    config: InterventionConfig

    _connection: GO2ConnectionSpec
    _curiosity: InterventionLeaseSpec
    _follow: FollowControlSpec
    _navigation: NavigationInterfaceSpec
    # Optional: speech is best-effort, so a stack without a speaker still runs.
    _speak: SpeakSkillSpec | None

    color_image: In[Image]
    odom: In[PoseStamped]
    goal_request: Out[PoseStamped]
    tele_cmd_vel: Out[Twist]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._latest_image: Image | None = None
        self._latest_odom: PoseStamped | None = None
        self._detector: Any = None

    @rpc
    def start(self) -> None:
        super().start()
        self.register_disposable(
            Disposable(
                self.color_image.subscribe(lambda m: setattr(self, "_latest_image", m))
            )
        )
        self.register_disposable(
            Disposable(self.odom.subscribe(lambda m: setattr(self, "_latest_odom", m)))
        )

    # ---- perception helpers -------------------------------------------------

    def _ensure_detector(self) -> Any:
        """Lazily build the shared YOLO pose detector (keypoints for gaze).

        Constructed on first use so the process boot cost is only paid when an
        intervention actually runs. On macOS this detector runs on CPU (see
        YoloFollowTracker._best_device), so it does not contend with the
        follow module's detector on the MPS/Metal compiler.
        """
        if self._detector is None:
            from nightwatch.tracker import YoloFollowTracker

            self._detector = YoloFollowTracker()
        return self._detector

    def _detect_people(self, image: Image) -> list[Any]:
        detector = self._ensure_detector()
        try:
            return list(detector.detect_people(image).detections)
        except Exception:
            logger.exception("intervention person detection failed")
            return []

    @staticmethod
    def _prominence(det: Any, image_width: float) -> float:
        x1, y1, x2, y2 = det.bbox
        area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        half = max(1.0, image_width / 2.0)
        centrality = 1.0 - abs((x1 + x2) / 2.0 - image_width / 2.0) / half
        return area * (0.5 + 0.5 * centrality)

    def _pick_subject(self, image: Image, detections: list[Any]) -> Any | None:
        candidates = [
            d
            for d in detections
            if (d.bbox[3] - d.bbox[1]) / float(max(1, image.height))
            >= self.config.person_min_height_frac
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda d: self._prominence(d, float(image.width)))

    def _visible_subject(self) -> Any | None:
        image = self._latest_image
        if image is None or image.height <= 0 or image.width <= 0:
            return None
        return self._pick_subject(image, self._detect_people(image))

    def _gaze_sample(self, track_id: int | None) -> bool:
        """One frame: does the tracked (or most prominent) subject look frontal."""
        image = self._latest_image
        if image is None or image.height <= 0 or image.width <= 0:
            return False
        detections = self._detect_people(image)
        subject = None
        if track_id is not None:
            subject = next(
                (d for d in detections if int(getattr(d, "track_id", -1)) == track_id),
                None,
            )
        if subject is None:
            subject = self._pick_subject(image, detections)
        if subject is None:
            return False
        return frontal_face_looking(
            getattr(subject, "keypoints", None),
            getattr(subject, "keypoint_scores", None),
            self.config.keypoint_conf,
        )

    # ---- motion helpers -----------------------------------------------------

    def _find_subject(self) -> Any | None:
        """Return a visible subject, scanning in place up to a full turn."""
        subject = self._visible_subject()
        if subject is not None:
            return subject
        step_rad = math.radians(self.config.scan_step_deg)
        duration = abs(step_rad) / max(0.05, self.config.scan_turn_rad_s)
        for _ in range(self.config.scan_max_steps):
            ensure_motion_ready(self._connection)
            self._turn_in_place(self.config.scan_turn_rad_s, duration)
            time.sleep(self.config.scan_settle_s)
            subject = self._visible_subject()
            if subject is not None:
                return subject
        return None

    def _turn_in_place(self, rate_rad_s: float, duration_s: float) -> None:
        """Rotate in place by publishing a bounded yaw twist, then stopping.

        Mirrors CuriositySupervisor._recover_from_stall: the twist rides
        tele_cmd_vel through MovementManager (teleop priority), and the
        intervene lease guarantees curiosity is not fighting for the base.
        """
        twist = Twist(
            linear=Vector3(0.0, 0.0, 0.0),
            angular=Vector3(0.0, 0.0, rate_rad_s),
        )
        deadline = time.monotonic() + duration_s
        try:
            while time.monotonic() < deadline:
                self.tele_cmd_vel.publish(twist)
                time.sleep(0.1)
        finally:
            self.tele_cmd_vel.publish(Twist.zero())

    def _approach(self, subject: Any) -> bool:
        """Send a standoff goal ~standoff_m short of the subject and wait.

        Uses goto.py's flat-floor unprojection: the bbox bottom edge is the
        floor-contact line, so pinhole geometry gives distance and bearing.
        Returns True on arrival, False on timeout or if the geometry is
        unusable (the protocol still proceeds to wave from where it stands).
        """
        odom = self._latest_odom
        if odom is None:
            logger.warning("intervention approach skipped: no odometry")
            return False
        x1, _y1, x2, y2 = (float(v) for v in subject.bbox)
        k = self.config.camera_info.K
        fx, fy, cx0, cy0 = k[0], k[4], k[2], k[5]
        u = (x1 + x2) / 2.0
        dv = y2 - cy0
        if dv <= 5:
            logger.info("intervention approach: subject not touching floor in view")
            return False
        dist = self.config.camera_height_m * fy / dv
        if dist > self.config.max_goal_m:
            logger.info("intervention approach: subject beyond safe range", dist=dist)
            return False
        lateral = (u - cx0) / fx * dist
        d_goal = max(0.3, dist - self.config.standoff_m)
        scale = d_goal / dist
        fwd, right = d_goal, lateral * scale
        yaw = odom.orientation.euler.z
        gx = odom.position.x + fwd * math.cos(yaw) - (-right) * math.sin(yaw)
        gy = odom.position.y + fwd * math.sin(yaw) + (-right) * math.cos(yaw)
        heading = math.atan2(gy - odom.position.y, gx - odom.position.x)
        ensure_motion_ready(self._connection)
        self.goal_request.publish(
            PoseStamped(
                ts=time.time(),
                frame_id="map",
                position=Vector3(gx, gy, 0.0),
                orientation=Quaternion.from_euler(Vector3(0.0, 0.0, heading)),
            )
        )
        logger.info(
            "intervention approach goal",
            dist=round(dist, 2),
            goal=(round(gx, 2), round(gy, 2)),
        )
        return self._wait_for_arrival(self.config.approach_timeout_s)

    def _wait_for_arrival(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        # Let the planner accept the goal before polling for IDLE.
        time.sleep(0.5)
        while time.monotonic() < deadline:
            try:
                if self._navigation.is_goal_reached():
                    return True
                if self._navigation.get_state() is NavigationState.IDLE:
                    # Planner went idle without a reached flag: goal resolved or
                    # unreachable. Either way stop waiting and wave from here.
                    return bool(self._navigation.is_goal_reached())
            except Exception:
                logger.exception("intervention arrival poll failed")
                return False
            time.sleep(0.25)
        try:
            self._navigation.cancel_goal()
        except Exception:
            logger.exception("intervention approach cancel failed")
        return False

    # ---- face-seeking arc geometry ------------------------------------------

    def _estimate_person_center(
        self, track_id: int | None
    ) -> tuple[tuple[float, float], float] | None:
        """Estimate the subject's world position for the arc circle center.

        The subject's bounding-box bottom-center is their floor-contact point;
        the same flat-floor pinhole unprojection the approach uses (goto.py
        geometry) turns it into a distance and bearing, which combine with the
        current odometry pose into a world (x, y). The arc is a circle centered
        on that point. Also returns ``theta0``, the bearing FROM the subject TO
        the robot's current position, so the first hop steps away from where
        the dog already stands rather than an arbitrary zero.
        """
        image = self._latest_image
        odom = self._latest_odom
        if image is None or odom is None or image.height <= 0 or image.width <= 0:
            return None
        detections = self._detect_people(image)
        subject = None
        if track_id is not None:
            subject = next(
                (d for d in detections if int(getattr(d, "track_id", -1)) == track_id),
                None,
            )
        if subject is None:
            subject = self._pick_subject(image, detections)
        if subject is None:
            return None
        x1, _y1, x2, y2 = (float(v) for v in subject.bbox)
        k = self.config.camera_info.K
        fx, fy, cx0, cy0 = k[0], k[4], k[2], k[5]
        u = (x1 + x2) / 2.0
        dv = y2 - cy0
        if dv <= 5:
            return None
        dist = self.config.camera_height_m * fy / dv
        lateral = (u - cx0) / fx * dist
        yaw = odom.orientation.euler.z
        px = odom.position.x + dist * math.cos(yaw) - (-lateral) * math.sin(yaw)
        py = odom.position.y + dist * math.sin(yaw) + (-lateral) * math.cos(yaw)
        theta0 = math.atan2(odom.position.y - py, odom.position.x - px)
        return (px, py), theta0

    def _arc_to(
        self, center: tuple[float, float], theta0: float, hop_index: int
    ) -> None:
        """Send a goal to the next waypoint on the circle around the subject."""
        px, py = center
        step = math.radians(self.config.arc_step_deg)
        angle = theta0 + self.config.arc_direction * (hop_index + 1) * step
        radius = self.config.arc_radius_m
        wx = px + radius * math.cos(angle)
        wy = py + radius * math.sin(angle)
        heading = math.atan2(py - wy, px - wx)  # keep facing the subject
        ensure_motion_ready(self._connection)
        self.goal_request.publish(
            PoseStamped(
                ts=time.time(),
                frame_id="map",
                position=Vector3(wx, wy, 0.0),
                orientation=Quaternion.from_euler(Vector3(0.0, 0.0, heading)),
            )
        )
        logger.info(
            "intervention arc hop",
            hop=hop_index,
            waypoint=(round(wx, 2), round(wy, 2)),
        )
        self._wait_for_arrival(self.config.arc_hop_timeout_s)

    def _arc_step(
        self, arc_state: dict[str, Any], track_id: int | None, hop_index: int
    ) -> None:
        """One face-seeking hop: lazily fix the circle center, then move to it.

        The circle center is estimated once, from the first detection available
        when arcing begins, and cached so every hop shares the same geometry.
        With no position estimate there is nothing to arc around, so the dog
        stays put and waves from where it is.
        """
        if arc_state.get("center") is None:
            estimate = self._estimate_person_center(track_id)
            if estimate is None:
                logger.info("intervention arc skipped: no person-center estimate")
                return
            arc_state["center"], arc_state["theta0"] = estimate
        self._arc_to(arc_state["center"], arc_state["theta0"], hop_index)

    # ---- follow standdown ---------------------------------------------------

    def _release_active_follow(self) -> tuple[bool, str | None]:
        """Stop an active person-follow so its movement capability releases.

        A running ``follow_person`` holds the MCP movement capability, and the
        dimos MCP server only auto-preempts exploration/patrol. The common
        trigger for this protocol (a tired person nearby) has usually started a
        curious follow first, so the follow must be stood down before the
        intervene lease is worth taking.

        Returns ``(ok, holder)``: ``ok`` is False (with ``holder`` naming the
        blocker) only when a follow is active and could not be released.
        """
        try:
            following = bool(self._follow.is_following())
        except Exception:
            # An unknown follow state must not block the protocol on a query
            # error; proceed and let the lease arbitration settle ownership.
            logger.exception("intervention could not query follow state")
            return True, None
        if not following:
            return True, None

        logger.info("suspicious protocol stopping active person-follow first")
        try:
            self._follow.stop_following()
        except Exception:
            logger.exception("intervention stop_following raised")
            return False, "person_follow"

        deadline = time.monotonic() + self.config.follow_release_timeout_s
        while True:
            try:
                if not self._follow.is_following():
                    return True, None
            except Exception:
                logger.exception("intervention follow-release poll failed")
                return False, "person_follow"
            if time.monotonic() >= deadline:
                return False, "person_follow"
            time.sleep(self.config.follow_release_poll_s)

    # ---- speech + vision ----------------------------------------------------

    def _speak_line(self, text: str) -> None:
        """Speak a line best-effort; a missing or failing speaker is ignored."""
        speaker = getattr(self, "_speak", None)
        if speaker is None or not text:
            return
        try:
            speaker.speak(text)
        except Exception:
            logger.warning("intervention speech failed; continuing", exc_info=True)

    def _describe_person(self, frame: Image | None) -> str | None:
        """One short, friendly, non-judgmental Chinese remark about the person.

        Uses the stock OpenAI SDK (OPENAI_BASE_URL routes to the relay) with a
        vision-capable model and a strict request timeout. Any failure returns
        None so nothing is spoken; this never raises into the protocol.
        """
        if frame is None:
            return None
        try:
            from openai import OpenAI

            data_url = f"data:image/jpeg;base64,{frame.to_base64(max_width=self.config.comment_max_width)}"
            client = OpenAI(timeout=self.config.comment_timeout_s)
            response = client.chat.completions.create(
                model=self.config.comment_model,
                timeout=self.config.comment_timeout_s,
                max_tokens=60,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self.config.comment_prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
            )
            text = (response.choices[0].message.content or "").strip()
            return text or None
        except Exception:
            logger.warning(
                "intervention personalized comment failed; skipping", exc_info=True
            )
            return None

    def _start_comment_job(
        self, describe: Callable[[], str | None]
    ) -> Callable[[], str | None]:
        """Start the personalized-comment vision call in the background.

        Returns a zero-arg ``take`` callable that joins the worker (bounded by
        ``comment_timeout_s``) and returns the sentence, or None on timeout,
        failure, or when the feature is disabled. Running in a thread lets the
        vision round trip overlap the approach navigation, so it adds no
        user-visible latency.
        """
        if not self.config.personalized_comment:
            return lambda: None
        holder: dict[str, str | None] = {"result": None}

        def worker() -> None:
            try:
                holder["result"] = describe()
            except Exception:
                logger.warning(
                    "intervention comment worker failed; skipping", exc_info=True
                )
                holder["result"] = None

        thread = threading.Thread(
            target=worker, name="intervention-comment", daemon=True
        )
        thread.start()

        def take() -> str | None:
            thread.join(timeout=self.config.comment_timeout_s)
            if thread.is_alive():
                return None
            return holder["result"]

        return take

    # ---- protocol core ------------------------------------------------------

    def _run_protocol(
        self,
        *,
        find_subject: Callable[[], Any | None],
        approach: Callable[[Any], bool],
        set_pitch: Callable[[float], Any],
        waver: Callable[[], bool],
        gaze_check: Callable[[], bool],
        rearm: Callable[[], Any],
        renew: Callable[[], Any],
        release: Callable[[], Any],
        greet: Callable[[], Any] = lambda: None,
        start_comment: Callable[[], Callable[[], str | None]] = lambda: (lambda: None),
        speak_comment: Callable[[str], Any] = lambda _text: None,
        face_check: Callable[[], bool] = lambda: True,
        arc_step: Callable[[int], Any] = lambda _hop: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> InterventionResult:
        """Lifecycle with hardware injected, so tests drive it with fakes.

        The pitch is ALWAYS restored to neutral and the lease ALWAYS released
        in the finally block, even if the wave loop raises. Speech and vision
        are best-effort: ``greet``, ``start_comment``, its returned ``take``,
        and ``speak_comment`` are each wrapped so a failure never changes the
        protocol result. Between approach and waving, a face-seeking arc
        (``face_check``/``arc_step``) repositions the dog so it never waves at
        someone's back.
        """

        def _safe(label: str, fn: Callable[..., Any], *args: Any) -> Any:
            try:
                return fn(*args)
            except Exception:
                logger.warning(
                    "intervention %s failed; continuing", label, exc_info=True
                )
                return None

        start = clock()
        waves = 0
        attempts = 0
        looked = False
        try:
            subject = find_subject()
            if subject is None:
                return InterventionResult(
                    found=False,
                    waves_performed=0,
                    blocked_attempts=0,
                    subject_looked=False,
                    duration_s=clock() - start,
                    message="No potential subject found after scanning.",
                )
            # Kick off the personalized-comment vision call so it overlaps the
            # approach (added latency is hidden).
            take_comment = _safe("comment start", start_comment)
            approach(subject)
            # Announce the dog AFTER arrival and BEFORE the arc, so the subject
            # hears it (and the "please look at me" cue) while it repositions.
            _safe("greeting", greet)
            # Never wave at a back: look for a frontal face, arcing around the
            # subject to discover it. Proceed to wave regardless when the arc
            # is exhausted (the wave and raised camera may still draw a look).
            face_found, _hops = run_face_seek(
                face_check=face_check,
                arc_step=arc_step,
                max_hops=self.config.arc_max_hops,
                timeout_s=self.config.arc_timeout_s,
                clock=clock,
            )
            # Speak the comment before the first wave; fail silent.
            comment = _safe("comment fetch", take_comment) if take_comment else None
            if comment:
                _safe("comment speech", speak_comment, comment)
            set_pitch(self.config.body_pitch_rad)

            def reapply_pitch() -> None:
                # The Hello gesture resets the body pose, so the up-tilt is
                # re-applied after every real wave. A rejected or failing
                # re-apply must never abort the protocol.
                try:
                    ok = set_pitch(self.config.body_pitch_rad)
                except Exception:
                    logger.warning(
                        "intervention pitch re-apply raised; continuing",
                        exc_info=True,
                    )
                    return
                if ok is False:
                    logger.warning(
                        "intervention pitch re-apply rejected by firmware; continuing"
                    )

            waves, attempts, looked = run_wave_protocol(
                waver=waver,
                rearm=rearm,
                gaze_check=gaze_check,
                renew_lease=renew,
                raise_pitch=reapply_pitch,
                max_waves=self.config.max_waves,
                max_attempts=self.config.max_wave_attempts,
            )
            return InterventionResult(
                found=True,
                waves_performed=waves,
                blocked_attempts=attempts - waves,
                subject_looked=looked,
                duration_s=clock() - start,
                message=(
                    f"Suspicious protocol complete: {waves} real wave(s), "
                    f"{attempts - waves} blocked attempt(s), "
                    f"face {'found' if face_found else 'not found'}, subject "
                    f"{'looked back' if looked else 'did not look back'}."
                ),
                face_found=face_found,
            )
        finally:
            try:
                set_pitch(0.0)
            except Exception:
                logger.exception("intervention pitch restore failed")
            try:
                release()
            except Exception:
                logger.exception("intervention lease release failed")

    @skill(uses=[CAP_MOVEMENT])
    def potential_detected(self, query: str = "person") -> str:
        """Run the suspicious-subject protocol on a visible potential subject.

        Stands next to the subject and waves (raising the camera onto their
        face) until they look back or the robot has waved four times. Curious
        wandering and person-following are suspended for the duration via a
        priority behavior lease. Returns a one-line summary.

        Args:
            query: description of the subject to approach (default the most
                prominent visible person).
        """
        # Stand down an active person-follow BEFORE taking the lease: it holds
        # the MCP movement capability the dimos server will not auto-preempt.
        released, holder = self._release_active_follow()
        if not released:
            logger.info("suspicious protocol refused: follow would not release")
            return (
                f"Cannot start the suspicious protocol: {holder} is still "
                "following and would not release motion."
            )

        acquired = self._curiosity.acquire_behavior(
            "intervention",
            "intervene",
            self.config.lease_priority,
            self.config.lease_ttl_s,
            f"suspicious protocol: {query}",
        )
        if not acquired.get("accepted"):
            current = acquired.get("current") or {}
            owner = current.get("owner", "a higher-priority behavior")
            logger.info("suspicious protocol refused", holder=owner)
            return (
                f"Cannot start the suspicious protocol: {owner} currently owns "
                "motion at a higher priority."
            )
        lease_id = str(acquired["lease"]["lease_id"])
        logger.info("suspicious protocol started", query=query, lease_id=lease_id)

        subject_track: dict[str, int | None] = {"id": None}
        arc_state: dict[str, Any] = {"center": None, "theta0": None}

        def find_subject() -> Any | None:
            subject = self._find_subject()
            if subject is not None:
                subject_track["id"] = int(getattr(subject, "track_id", -1))
            return subject

        result = self._run_protocol(
            find_subject=find_subject,
            approach=self._approach,
            set_pitch=lambda pitch: set_body_pitch(self._connection, pitch),
            waver=lambda: wave_hello(self._connection),
            gaze_check=lambda: check_gaze(
                lambda: self._gaze_sample(subject_track["id"]),
                clock=time.monotonic,
                sleep=time.sleep,
                window_s=self.config.gaze_window_s,
                frames_required=self.config.gaze_frames_required,
                poll_s=self.config.gaze_poll_s,
            ),
            rearm=lambda: ensure_motion_ready(self._connection, force=True),
            renew=lambda: self._curiosity.renew_behavior(
                lease_id, self.config.lease_ttl_s
            ),
            release=lambda: self._curiosity.release_behavior(lease_id),
            greet=lambda: self._speak_line(self.config.greeting_text),
            start_comment=lambda: self._start_comment_job(
                lambda: self._describe_person(self._latest_image)
            ),
            speak_comment=self._speak_line,
            face_check=lambda: check_gaze(
                lambda: self._gaze_sample(subject_track["id"]),
                clock=time.monotonic,
                sleep=time.sleep,
                window_s=self.config.face_seek_window_s,
                frames_required=self.config.gaze_frames_required,
                poll_s=self.config.gaze_poll_s,
            ),
            arc_step=lambda hop: self._arc_step(arc_state, subject_track["id"], hop),
        )
        logger.info(
            "suspicious protocol finished",
            found=result.found,
            waves=result.waves_performed,
            blocked_attempts=result.blocked_attempts,
            subject_looked=result.subject_looked,
            face_found=result.face_found,
            duration_s=round(result.duration_s, 1),
        )
        if not result.found:
            return result.message
        return (
            f"{result.message} ({result.duration_s:.1f}s, "
            f"{result.waves_performed + result.blocked_attempts} total attempt(s))"
        )
