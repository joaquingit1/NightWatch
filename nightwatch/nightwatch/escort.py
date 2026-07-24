"""escort_to_sleeping_area: the guarded "take me to Bedroom" escort.

Autonomous behaviors cannot distract an active escort, but the operator's
manual takeover and safety holds always remain above it. It walks the person
to the one operator-selected Bedroom and stops.

Uninterruptibility is bought entirely with an existing mechanism, not new
arbitration code: the escort holds a priority-60 ``escort`` behavior lease on
the CuriositySupervisor for its whole duration. 60 outranks the suspicious
protocol's intervene lease (40), the curious person-follow, and autonomous
curiosity, so the supervisor's ``_tick`` arbitration yields the base to the
escort and suppresses everything below it (including an escort that preempts an
in-flight wave protocol, which is correct by design: an explicit escort beats a
wave). The lease also surfaces as ``RobotActivity(behavior=escort,
owner="escort")`` on the single-writer activity stream the operator console
already reads, so the escort is visible on the dashboard without a second writer
of the activity contract.

Triggered manually today (operator console / chat via
``escort_to_sleeping_area``) and later automatically when a facial pipeline
reports sustained sleepiness; the skill body is identical either way.

The navigation loop is factored so it is testable without threads or hardware:
``run_escort`` takes injected now/get_pose/find_area/acquire/renew/release/
send_goal/arrived/sleep callables, so the full lifecycle (including the
always-release-the-lease finally path) can be exercised with plain fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
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
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.navigation.navigation_spec import NavigationInterfaceSpec
from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec
from dimos.spec.utils import Spec
from dimos.utils.logging_config import setup_logger
from nightwatch.unitree import ensure_motion_ready

logger = setup_logger()


class EscortLeaseSpec(Spec, Protocol):
    """The slice of CuriositySupervisor the escort needs for motion authority."""

    def acquire_behavior(
        self, owner: str, behavior: str, priority: int, ttl_s: float, reason: str
    ) -> dict[str, Any]: ...
    def renew_behavior(self, lease_id: str, ttl_s: float) -> bool: ...
    def release_behavior(self, lease_id: str) -> bool: ...


class SleepingAreaSpec(Spec, Protocol):
    """The one world-model query the escort needs: the unique Bedroom."""

    def bedroom_destination(self, x: float, y: float) -> dict | None: ...


@dataclass(frozen=True, slots=True)
class EscortResult:
    found: bool
    acquired: bool
    arrived: bool
    area_id: str | None
    source: str | None
    goal_attempts: int
    distance_m: float
    duration_s: float
    message: str


def run_escort(
    *,
    now: Callable[[], float],
    get_pose: Callable[[], tuple[float, float] | None],
    find_area: Callable[[float, float], dict[str, Any] | None],
    acquire: Callable[[], dict[str, Any]],
    renew: Callable[[], Any],
    release: Callable[[], Any],
    send_goal: Callable[[float, float], Any],
    arrived: Callable[[], bool],
    sleep: Callable[[float], None],
    speak: Callable[[str], Any] | None = None,
    area_type: str = "sleeping_area",
    arrival_radius_m: float = 0.8,
    tick_s: float = 0.25,
    per_goal_timeout_s: float = 40.0,
    overall_timeout_s: float = 120.0,
    max_goal_attempts: int = 3,
    start_line: str = "",
    arrival_line: str = "Here is the sleeping area. Rest well.",
) -> EscortResult:
    """Escort lifecycle with hardware injected, so tests drive it with fakes.

    Order of operations matters for the guarantees:

    - The nearest area is resolved BEFORE any lease is acquired, so "no sleeping
      area known" never touches motion authority (``acquire`` is not called).
    - The lease is acquired exactly once; if it is refused (only possible versus
      another escort at the same priority) nothing is sent and there is nothing
      to release.
    - Once the lease is held it is renewed every loop tick and ALWAYS released in
      the finally block, whether the escort arrives, times out, or raises.
    - A goal is (re)published up to ``max_goal_attempts`` times: the planner
      replans internally, so a fresh ``send_goal`` is the retry when a goal does
      not arrive within its per-goal budget. Arrival is nav's goal-reached flag
      OR the pose falling within ``arrival_radius_m`` of the area center.
    - Speech is best-effort at both ends: ``start_line`` once, right after the
      first goal is sent, and ``arrival_line`` on arrival. Both are wrapped so a
      failing speaker never changes the escort's outcome.
    """
    start = now()
    pose0 = get_pose()
    x0, y0 = pose0 if pose0 is not None else (0.0, 0.0)
    area = find_area(x0, y0)
    if area is None:
        return EscortResult(
            found=False,
            acquired=False,
            arrived=False,
            area_id=None,
            source=None,
            goal_attempts=0,
            distance_m=0.0,
            duration_s=now() - start,
            message=(
                "No sleeping area is known yet; explore or tag one first, then "
                "I can escort you there."
            ),
        )

    area_id = str(area.get("area_id"))
    source = str(area.get("source")) if area.get("source") is not None else None

    acquired = acquire()
    if not acquired.get("accepted"):
        current = acquired.get("current") or {}
        owner = current.get("owner", "a higher-priority behavior")
        return EscortResult(
            found=True,
            acquired=False,
            arrived=False,
            area_id=area_id,
            source=source,
            goal_attempts=0,
            distance_m=0.0,
            duration_s=now() - start,
            message=(
                f"Cannot start the escort: {owner} currently owns motion at a "
                "higher priority."
            ),
        )

    gx, gy = float(area["x"]), float(area["y"])
    traveled = 0.0
    last = pose0
    arrived_flag = False
    authority_lost = False
    goal_attempts = 0

    def say(text: str, failure_msg: str) -> None:
        # Best-effort speech: never let a speaker error reach the escort result.
        if speak is None or not text:
            return
        try:
            speak(text)
        except Exception:
            logger.exception(failure_msg)

    try:
        overall_deadline = start + overall_timeout_s
        while (
            goal_attempts < max_goal_attempts
            and not arrived_flag
            and now() < overall_deadline
        ):
            goal_attempts += 1
            send_goal(gx, gy)
            if goal_attempts == 1:
                # Announce the escort once, right after the first goal is sent.
                say(start_line, "escort start speech failed")
            goal_deadline = min(now() + per_goal_timeout_s, overall_deadline)
            while now() < goal_deadline:
                if renew() is False:
                    authority_lost = True
                    break
                pose = get_pose()
                if pose is not None:
                    if last is not None:
                        traveled += math.hypot(pose[0] - last[0], pose[1] - last[1])
                    last = pose
                    dist = math.hypot(pose[0] - gx, pose[1] - gy)
                else:
                    dist = float("inf")
                if arrived() or dist <= arrival_radius_m:
                    arrived_flag = True
                    break
                sleep(tick_s)
            if authority_lost:
                break

        duration = now() - start
        if arrived_flag:
            logger.info(
                "escort completed",
                area_id=area_id,
                source=source,
                distance_m=round(traveled, 2),
                duration_s=round(duration, 1),
            )
            say(arrival_line, "escort arrival speech failed")
            message = (
                f"Escort complete: arrived at sleeping area '{area_id}' "
                f"({source}) after {traveled:.1f} m in {duration:.1f}s."
            )
        else:
            logger.info(
                "escort did not arrive",
                area_id=area_id,
                source=source,
                goal_attempts=goal_attempts,
                distance_m=round(traveled, 2),
                duration_s=round(duration, 1),
            )
            message = (
                f"Escort ended without reaching sleeping area '{area_id}' "
                f"({source}) because operator/safety authority took over."
                if authority_lost
                else (
                    f"Escort ended without reaching sleeping area '{area_id}' "
                    f"({source}) after {goal_attempts} goal attempt(s), "
                    f"{traveled:.1f} m, {duration:.1f}s."
                )
            )
        return EscortResult(
            found=True,
            acquired=True,
            arrived=arrived_flag,
            area_id=area_id,
            source=source,
            goal_attempts=goal_attempts,
            distance_m=traveled,
            duration_s=duration,
            message=message,
        )
    finally:
        try:
            release()
        except Exception:
            logger.exception("escort lease release failed")


class EscortConfig(ModuleConfig):
    # Behavior lease: priority 60 outranks the suspicious protocol (40), the
    # curious person-follow, and autonomous curiosity, so the escort cannot be
    # interrupted. BRIDGE.md documents this priority ladder.
    lease_priority: int = 60
    lease_ttl_s: float = 15.0
    # Arrival: nav's goal-reached flag OR within this radius of the area center.
    arrival_radius_m: float = 0.8
    tick_s: float = 0.25
    # Per-goal budget before a fresh goal is published (the planner replans
    # internally; a re-published goal is the retry), and the overall cap.
    per_goal_timeout_s: float = 40.0
    overall_timeout_s: float = 120.0
    max_goal_attempts: int = 3
    # Spoken once at the start over the dog's Bluetooth speaker (natural Chinese
    # TTS via SpeakSkill): "you look exhausted; head to the sleeping area, scan
    # the QR code on me if you agree, and follow me."
    start_text: str = (
        "你看起来太累啦。建议你去休息区睡一觉。愿意的话，扫一下我身上的二维码，然后跟我来吧。"
    )
    arrival_line: str = "Here is the sleeping area. Rest well."


class EscortSkill(Module):
    config: EscortConfig

    _connection: GO2ConnectionSpec
    _curiosity: EscortLeaseSpec
    _navigation: NavigationInterfaceSpec
    _world: SleepingAreaSpec
    # Optional: speech is best-effort, so a stack without a speaker still escorts.
    _speak: SpeakSkillSpec | None

    odom: In[PoseStamped]
    goal_request: Out[PoseStamped]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._latest_odom: PoseStamped | None = None

    @rpc
    def start(self) -> None:
        super().start()
        self.register_disposable(
            Disposable(self.odom.subscribe(lambda m: setattr(self, "_latest_odom", m)))
        )

    # ---- injected-callable implementations ----------------------------------

    def _get_pose(self) -> tuple[float, float] | None:
        odom = self._latest_odom
        if odom is None:
            return None
        return (float(odom.position.x), float(odom.position.y))

    def _send_goal(self, x: float, y: float) -> None:
        """Publish an area-center goal on the planner's goal_request channel.

        Mirrors goto.py/intervene.py: goals ride ``goal_request`` as a
        PoseStamped in the world frame the odometry/area coords share, and the
        firmware is re-armed first (it silently drops planner velocities unless
        the dog is standing in BalanceStand with the joystick listening).
        """
        odom = self._latest_odom
        if odom is not None:
            heading = math.atan2(y - odom.position.y, x - odom.position.x)
        else:
            heading = 0.0
        ensure_motion_ready(self._connection)
        self.goal_request.publish(
            PoseStamped(
                ts=time.time(),
                frame_id="map",
                position=Vector3(x, y, 0.0),
                orientation=Quaternion.from_euler(Vector3(0.0, 0.0, heading)),
            )
        )
        logger.info("escort goal", goal=(round(x, 2), round(y, 2)))

    def _arrived(self) -> bool:
        try:
            return bool(self._navigation.is_goal_reached())
        except Exception:
            logger.exception("escort arrival poll failed")
            return False

    def _speak_line(self, text: str) -> None:
        speaker = getattr(self, "_speak", None)
        if speaker is None:
            return
        try:
            speaker.speak(text)
        except Exception:
            logger.exception("escort speech failed")

    # ---- skill --------------------------------------------------------------

    @skill(uses=[CAP_MOVEMENT])
    def escort_to_sleeping_area(self) -> str:
        """Escort the person to the NEAREST known sleeping area, uninterruptibly.

        Like going home, but it CANNOT be interrupted: while it runs, curious
        wandering, dog gestures, person-following, and the suspicious-subject
        wave protocol are all suspended (the escort holds a high-priority motion
        lease). It walks straight to the nearest sleeping area and stops. Use
        this when someone needs to be taken to rest. Returns a one-line summary.
        """
        pose0 = self._get_pose()
        x0, y0 = pose0 if pose0 is not None else (0.0, 0.0)
        area = self._world.bedroom_destination(x0, y0)
        if area is None:
            logger.info("escort: Bedroom unavailable")
            return (
                "No Bedroom is available in the current map frame; mark it on "
                "the 3D map before requesting an escort."
            )

        acquired = self._curiosity.acquire_behavior(
            "escort",
            "escort",
            self.config.lease_priority,
            self.config.lease_ttl_s,
            "uninterruptible escort to sleeping area",
        )
        if not acquired.get("accepted"):
            current = acquired.get("current") or {}
            owner = current.get("owner", "a higher-priority behavior")
            logger.info("escort refused", holder=owner)
            return (
                f"Cannot start the escort: {owner} currently owns motion at a "
                "higher priority."
            )
        lease_id = str(acquired["lease"]["lease_id"])
        logger.info(
            "escort started",
            area_id=area.get("area_id"),
            source=area.get("source"),
            lease_id=lease_id,
        )

        result = run_escort(
            now=time.monotonic,
            get_pose=self._get_pose,
            # The area is already resolved above; re-resolve inside the core so
            # the acquire/release lifecycle stays entirely within run_escort.
            find_area=lambda x, y: area,
            acquire=lambda: acquired,
            renew=lambda: self._curiosity.renew_behavior(
                lease_id, self.config.lease_ttl_s
            ),
            release=lambda: self._curiosity.release_behavior(lease_id),
            send_goal=self._send_goal,
            arrived=self._arrived,
            sleep=time.sleep,
            speak=self._speak_line,
            arrival_radius_m=self.config.arrival_radius_m,
            tick_s=self.config.tick_s,
            per_goal_timeout_s=self.config.per_goal_timeout_s,
            overall_timeout_s=self.config.overall_timeout_s,
            max_goal_attempts=self.config.max_goal_attempts,
            start_line=self.config.start_text,
            arrival_line=self.config.arrival_line,
        )
        if not result.arrived:
            try:
                self._navigation.cancel_goal()
            except Exception:
                logger.exception("escort cancellation failed")
        logger.info(
            "escort finished",
            arrived=result.arrived,
            area_id=result.area_id,
            source=result.source,
            goal_attempts=result.goal_attempts,
            distance_m=round(result.distance_m, 2),
            duration_s=round(result.duration_s, 1),
        )
        return result.message
