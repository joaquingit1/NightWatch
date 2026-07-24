"""UnitreeSkillContainer that returns the Go2 to a locomotion-ready FSM.

The WebRTC velocity path emulates the wireless controller.  Packets continue
to be accepted while the Go2 is left in a gesture/posture state, but firmware
silently ignores them.  DimensionalOS' hosted teleop solves this with its
StandReady sequence:

    StandUp -> settle -> RecoveryStand -> BalanceStand -> SwitchJoystick(True)

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

# A full FSM recovery takes several WebRTC round trips and a required StandUp
# settle.  Avoid repeating it while locomotion remains fresh.
_READY_TTL_S = 20.0
_last_ready: list[float] = [0.0]


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
    return True


def ensure_motion_ready(connection: Any, force: bool = False) -> bool:
    """Put the robot in a state where velocity commands actually drive it.

    connection: anything with publish_request(topic, data) (GO2ConnectionSpec).
    Returns True if the re-arm sequence was sent (or fresh), False on error.
    """
    now = time.monotonic()
    if not force and now - _last_ready[0] < _READY_TTL_S:
        return True
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

        # This intentionally mirrors dimos.teleop.hosted.go2_command StandReady.
        if not step("StandUp"):
            return False
        time.sleep(3.0)
        if not step("RecoveryStand"):
            return False
        time.sleep(0.3)
        if not step("BalanceStand"):
            return False
        time.sleep(0.3)
        if not step("SwitchJoystick", parameter={"data": True}):
            return False
        _last_ready[0] = time.monotonic()
        logger.info(
            "Motion re-armed "
            "(StandUp + RecoveryStand + BalanceStand + SwitchJoystick on)"
        )
        return True
    except Exception:
        logger.exception("ensure_motion_ready failed")
        return False


# Body tilt is bounded so a facial-analysis camera raise can never command a
# posture that unbalances the Go2. 0.4 rad (~23 deg) up is plenty to lift the
# front camera onto a standing person's face at ~1.2 m.
_MAX_BODY_PITCH_RAD = 0.4


def wave_hello(connection: Any) -> bool:
    """Perform ONE real Hello wave and verify the firmware accepted it.

    connection: anything with publish_request(topic, data) (GO2ConnectionSpec).

    A wave only counts when the firmware acknowledges it. The Go2 silently
    ignores sport gestures unless it is standing in a locomotion-ready FSM, so
    we re-arm first (StandUp -> RecoveryStand -> BalanceStand -> joystick) and
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
    """Tilt the body (and thus the front camera) up via the Euler sport command.

    connection: anything with publish_request(topic, data) (GO2ConnectionSpec).

    The Go2 exposes a body-orientation command, SPORT_CMD["Euler"] == 1007,
    whose parameter is a body-frame roll/pitch/yaw triple ``{"x", "y", "z"}``
    (the same {x, y, z} envelope the Move command uses in
    ``dimos.robot.unitree.connection._publish_movement``). We only touch pitch:
    ``(0, pitch, 0)``. A positive pitch raises the nose, lifting the front
    camera onto a standing person's face for the facial-analysis pipeline.

    Pitch is clamped to +/- 0.4 rad for balance safety. Restore the neutral
    pose with ``set_body_pitch(connection, 0.0)``. Caller is responsible for
    having the robot standing (BalanceStand); this helper only publishes the
    orientation command and verifies the ack.
    """
    pitch = max(-_MAX_BODY_PITCH_RAD, min(_MAX_BODY_PITCH_RAD, float(pitch_rad)))
    try:
        response = connection.publish_request(
            RTC_TOPIC["SPORT_MOD"],
            {"api_id": SPORT_CMD["Euler"], "parameter": {"x": 0.0, "y": pitch, "z": 0.0}},
        )
    except Exception:
        logger.exception("set_body_pitch publish failed")
        return False
    ok = _request_succeeded(response)
    if not ok:
        logger.warning(
            "set_body_pitch: firmware rejected the Euler pose",
            pitch=pitch,
            response=repr(response)[:200],
        )
    return ok


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
