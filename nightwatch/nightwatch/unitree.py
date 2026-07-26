"""UnitreeSkillContainer that returns the Go2 to a locomotion-ready FSM.

Nightwatch uses the Go2 firmware's MCF sport ``Move`` API. Packets continue to
be accepted while the Go2 is left in a gesture/posture state, but firmware
silently ignores them. The MCF contract exits ``Pose`` with ``StopMove``;
``Pose(False)`` is not the documented exit operation:

    StopMove -> StandUp -> settle -> RecoveryStand -> BalanceStand

The old Nightwatch helper omitted RecoveryStand, waited too little after
StandUp, ignored every response, and then logged "Motion re-armed" even when
the robot remained locked.  Keep this sequence in one place and use it before
planner, follow, recovery, and manual-adjacent motion.

Named UnitreeSkillContainer so blueprint dedupe replaces the stock module.
"""

import time
from typing import Any

from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

from dimos.agents.annotation import skill
from dimos.robot.unitree.unitree_skill_container import (
    UnitreeSkillContainer as _StockUnitreeSkillContainer,
)
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

# A full FSM recovery takes several WebRTC round trips plus a required 3 s
# StandUp settle, and it briefly takes control away from velocity navigation.
# BalanceStand remains latched until a posture/gesture command changes it;
# gesture completion and operator Stand already use ``force=True``. Re-arming
# ordinary explorer/patrol restarts every 20 s created the observed
# stand/replan/stall feedback loop, so retain readiness for the run instead of
# treating it like a sensor heartbeat.
_READY_TTL_S = 5.0 * 60.0
_last_ready: list[float] = [0.0]

# Firmware pose mode is a dangerous latch on this robot: while it is on, the
# Go2 adjusts body attitude but ignores gait/velocity commands, exactly matching
# the live "joints and torso move, feet do not" failure. Nightwatch must never
# enter it. The flag remains only so an older in-flight scan can be recovered
# during a rolling process upgrade.
_pose_mode_on: list[bool] = [False]


def pose_mode_latched() -> bool:
    """True while the firmware is believed to be in body-pose mode."""
    return _pose_mode_on[0]


def clear_pose_mode(connection: Any, force: bool = False) -> bool:
    """Idempotently leave pose mode so velocity commands work again.

    Safe to call at any time; a no-op when pose mode was never entered. The
    tracked flag is cleared ONLY on a firmware acknowledgement, so a dropped
    or rejected command leaves it set and the next caller retries.
    """
    if not _pose_mode_on[0] and not force:
        return True
    try:
        topic = RTC_TOPIC["SPORT_MOD"]
        response = connection.publish_request(
            topic, {"api_id": SPORT_CMD["StopMove"]}
        )
        if not _request_succeeded(response):
            logger.warning(
                "MCF StopMove pose exit was not acknowledged; will retry",
                response=repr(response)[:200],
            )
            return False
        balance_response = connection.publish_request(
            topic, {"api_id": SPORT_CMD["BalanceStand"]}
        )
        if not _request_succeeded(balance_response):
            logger.warning(
                "BalanceStand after pose exit was not acknowledged; will retry",
                response=repr(balance_response)[:200],
            )
            return False
    except Exception:
        logger.exception("pose mode exit failed; will retry")
        return False
    _pose_mode_on[0] = False
    logger.info("pose mode cleared; velocity commands are effective again")
    return True


def _request_succeeded(response: Any) -> bool:
    """Interpret Unitree replies without treating an error dict as truthy."""
    if response is None or response is False:
        return False
    if not isinstance(response, dict):
        return bool(response)
    if response.get("error"):
        return False
    status = response.get("status")
    if isinstance(status, str) and status.lower() in {"error", "failed", "failure"}:
        return False
    code = response.get("code")
    if isinstance(code, int) and code not in {0, 100, 1000}:
        return False
    data = response.get("data")
    if isinstance(data, dict):
        nested_code = data.get("code")
        if isinstance(nested_code, int) and nested_code != 0:
            return False
        # unitree_webrtc_connect wraps firmware status here. The previous
        # parser missed this path and treated live status 7004 (unsupported
        # motion mode) as success because the outer response dict was truthy.
        header = data.get("header")
        if isinstance(header, dict):
            nested_status = header.get("status")
            if isinstance(nested_status, dict):
                firmware_code = nested_status.get("code")
                if isinstance(firmware_code, int) and firmware_code != 0:
                    return False
    return True


def ensure_motion_ready(connection: Any, force: bool = False) -> bool:
    """Put the robot in a state where velocity commands actually drive it.

    connection: anything with publish_request(topic, data) (GO2ConnectionSpec).
    Returns True if the re-arm sequence was sent (or fresh), False on error.
    """
    now = time.monotonic()
    if not force and now - _last_ready[0] < _READY_TTL_S:
        # The TTL fast path must still break a pose-mode latch, otherwise a
        # re-arm within 5 minutes of the last one silently leaves the dog
        # unable to walk (the live freeze: the full sequence below, which
        # clears pose mode, was skipped by this very branch).
        return clear_pose_mode(connection)
    try:
        topic = RTC_TOPIC["SPORT_MOD"]

        def step(name: str, *, parameter: dict[str, Any] | None = None) -> bool:
            request: dict[str, Any] = {"api_id": SPORT_CMD[name]}
            if parameter is not None:
                request["parameter"] = parameter
            response = connection.publish_request(topic, request)
            ok = _request_succeeded(response)
            if not ok:
                logger.error(
                    "Go2 StandReady step rejected",
                    step=name,
                    response=repr(response)[:300],
                )
            return ok

        # MCF's documented exit from Pose is StopMove, not Pose(False). Always
        # attempt it because firmware state can outlive this process. A Go2
        # that is already lying down returns status -1 ("nothing to stop"),
        # though, and DimOS' stock StandReady contract starts directly with
        # StandUp. Do not let that benign precondition response prevent the
        # actual, fully acknowledged stand sequence from running.
        stopmove_ok = step("StopMove")
        if not stopmove_ok:
            logger.warning(
                "Go2 StopMove precondition was rejected; continuing with "
                "the standard StandUp recovery sequence"
            )
        if not step("StandUp"):
            return False
        time.sleep(3.0)
        if not step("RecoveryStand"):
            return False
        time.sleep(0.3)
        if not step("BalanceStand"):
            return False
        # A completed StandUp/RecoveryStand/BalanceStand sequence necessarily
        # supersedes any stale body-pose FSM state.
        _pose_mode_on[0] = False
        _last_ready[0] = time.monotonic()
        logger.info(
            "MCF motion re-armed "
            "(StopMove + StandUp + RecoveryStand + BalanceStand)"
        )
        return True
    except Exception:
        logger.exception("ensure_motion_ready failed")
        return False


def wave_hello(connection: Any) -> bool:
    """Perform ONE real Hello wave and verify the firmware accepted it.

    connection: anything with publish_request(topic, data) (GO2ConnectionSpec).

    A wave only counts when the firmware acknowledges it. The Go2 silently
    ignores sport gestures unless it is standing in a locomotion-ready FSM, so
    we re-arm first (StopMove -> StandUp -> RecoveryStand -> BalanceStand) and
    then publish the Hello routine (SPORT_CMD["Hello"] == 1016, parameterless).
    A blocked or rejected wave returns False so the caller can re-arm and retry
    without counting it as a real wave.
    """
    if not ensure_motion_ready(connection):
        logger.warning("wave_hello: motion not ready; wave not attempted")
        return False
    try:
        response = connection.publish_request(
            RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["Hello"]}
        )
    except Exception:
        logger.exception("wave_hello publish failed")
        return False
    ok = _request_succeeded(response)
    if not ok:
        logger.warning(
            "wave_hello: firmware rejected the Hello wave",
            response=repr(response)[:200],
        )
    return ok


def set_body_pitch(connection: Any, pitch_rad: float) -> bool:
    """Refuse body-pose changes; Nightwatch must never lower/tilt the hips.

    A non-zero request is reported as unavailable without sending anything to
    the firmware. A zero request is cleanup from older scan code and uses only
    the ordinary MCF StopMove/BalanceStand recovery path.
    """
    if abs(float(pitch_rad)) > 1e-6:
        logger.warning(
            "body-pitch request refused; posture changes are disabled",
            requested_pitch_rad=float(pitch_rad),
        )
        return False
    return clear_pose_mode(connection, force=True)


class UnitreeSkillContainer(_StockUnitreeSkillContainer):
    @skill
    def relative_move(self, forward: float = 0.0, left: float = 0.0, degrees: float = 0.0) -> str:
        """Move the robot relative to its current position.

        The `degrees` arguments refers to the rotation the robot should be at the end, relative to its current rotation.

        Example calls:

            # Move to a point that's 2 meters forward and 1 to the right.
            relative_move(forward=2, left=-1, degrees=0)

            # Move back 1 meter, while still facing the same direction.
            relative_move(forward=-1, left=0, degrees=0)

            # Rotate 90 degrees to the right (in place)
            relative_move(forward=0, left=0, degrees=-90)

            # Move 3 meters left, and face that direction
            relative_move(forward=0, left=3, degrees=90)
        """
        ensure_motion_ready(self._connection)
        return super().relative_move(forward=forward, left=left, degrees=degrees)
