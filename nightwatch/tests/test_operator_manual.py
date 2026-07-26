from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from dimos.msgs.geometry_msgs.Twist import Twist

from nightwatch.webchat import NightwatchWebInput, _OPERATOR_HTML


class Published:
    def __init__(self) -> None:
        self.values: list[Twist] = []

    def publish(self, value: Twist) -> None:
        self.values.append(value)


class Navigation:
    def __init__(self) -> None:
        self.cancelled = 0

    def cancel_goal(self) -> bool:
        self.cancelled += 1
        return True


@dataclass
class Curiosity:
    mode: str = "autonomous"
    epoch: int = 0
    sequence: int = 0

    def curiosity_status(self) -> dict[str, object]:
        return {
            "operating_mode": self.mode,
            "mode_epoch": self.epoch,
            "manual_input_seq": self.sequence,
        }

    def set_operating_mode(self, mode: str) -> str:
        if mode != self.mode:
            self.mode = mode
            self.epoch += 1
        return f"Operating mode changed to {mode}; command epoch is {self.epoch}."

    def set_mission_mode(self, mode: str) -> str:
        return self.set_operating_mode(
            "sleep_analysis" if mode == "cruise" else "autonomous"
        )

    def set_control_mode(self, mode: str) -> str:
        return self.set_operating_mode(
            "manual" if mode == "manual" else "autonomous"
        )

    def note_manual_input(self, epoch: int, sequence: int) -> str:
        if epoch != self.epoch or sequence <= self.sequence:
            return "Manual input refused: stale command."
        self.sequence = sequence
        return f"Manual input {sequence} accepted for epoch {epoch}."

    def request_fatigue_scan(self) -> str:
        return "Immediate sleep-analysis scan requested."

    def set_sleep_scan_settings(
        self, interval_s: float, duration_s: float
    ) -> str:
        return f"{interval_s}/{duration_s}"

    # The direct-action lambda table in _dispatch_operator_action binds these
    # attributes for every action, so the stub must provide them even for
    # continuation-dispatched actions such as the voice presets.
    def hold_position(self, reason: str) -> str:
        return f"Holding position: {reason}."

    def resume_curiosity(self) -> str:
        return "Resumed."

    def lie_down_until_resumed(self) -> str:
        return "Lying down."

    def stand_up_and_resume(self) -> str:
        return "Standing up."

    def perform_dog_expression(self, expression: str) -> str:
        return f"Expression {expression} accepted."


def _web_input() -> tuple[NightwatchWebInput, Curiosity, Navigation, Published]:
    web = object.__new__(NightwatchWebInput)
    curiosity = Curiosity()
    navigation = Navigation()
    published = Published()
    web._curiosity = curiosity
    web._navigation = navigation
    web.tele_cmd_vel = published
    web._teleop_lock = Lock()
    web._last_teleop_seq = 0
    web._teleop_last_at = 0.0
    web._teleop_active = False
    return web, curiosity, navigation, published


def test_manual_mode_transition_cancels_autonomy_and_publishes_zero() -> None:
    web, curiosity, navigation, published = _web_input()
    ok, _message = web._dispatch_operator_action(
        "operating_mode", {"mode": "manual"}
    )
    assert ok is True
    assert curiosity.mode == "manual"
    assert navigation.cancelled == 1
    assert len(published.values) == 1
    assert published.values[-1].is_zero()


def test_manual_velocity_uses_epoch_sequence_and_hard_speed_limits() -> None:
    web, curiosity, _navigation, published = _web_input()
    curiosity.mode = "manual"
    curiosity.epoch = 4

    ok, _message = web._dispatch_operator_action(
        "teleop",
        {
            "epoch": 4,
            "sequence": 1,
            "x": 99.0,
            "y": -99.0,
            "wz": 99.0,
        },
    )
    assert ok is True
    twist = published.values[-1]
    assert twist.linear.x == 0.8
    assert twist.linear.y == -0.5
    assert twist.angular.z == 1.0

    stale, _message = web._dispatch_operator_action(
        "teleop",
        {"epoch": 4, "sequence": 1, "x": 0.1, "y": 0.0, "wz": 0.0},
    )
    assert stale is False
    assert len(published.values) == 1


def test_emergency_stop_latches_manual_and_sends_zero() -> None:
    web, curiosity, navigation, published = _web_input()
    ok, message = web._dispatch_operator_action("emergency_stop")
    assert ok is True
    assert "Emergency stop" in message
    assert curiosity.mode == "manual"
    assert navigation.cancelled == 1
    assert published.values[-1].is_zero()


class AgentSpec:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.dispatched: list[tuple[dict, dict]] = []

    def dispatch_continuation(self, continuation: dict, context: dict) -> bool:
        self.dispatched.append((continuation, context))
        return self.ok


def test_speak_text_passes_sanitized_text_to_the_speak_skill() -> None:
    web, _curiosity, _navigation, _published = _web_input()
    agent = AgentSpec()
    web._agent_spec = agent

    ok, message = web._dispatch_operator_action(
        "speak_text", {"text": "  你好，我是守夜犬。 Hello!  "}
    )

    assert ok is True
    assert "sent" in message.lower()
    continuation, context = agent.dispatched[-1]
    assert continuation["tool"] == "speak"
    assert continuation["args"] == {"text": "你好，我是守夜犬。 Hello!"}
    assert context["_silent"] is True


def test_speak_text_rejects_empty_and_caps_length() -> None:
    web, _curiosity, _navigation, _published = _web_input()
    agent = AgentSpec()
    web._agent_spec = agent

    ok, _message = web._dispatch_operator_action("speak_text", {"text": "   "})
    assert ok is False
    assert agent.dispatched == []

    missing, _message = web._dispatch_operator_action("speak_text")
    assert missing is False
    assert agent.dispatched == []

    long_ok, _message = web._dispatch_operator_action(
        "speak_text", {"text": "x" * 500}
    )
    assert long_ok is True
    continuation, _context = agent.dispatched[-1]
    assert continuation["args"]["text"] == "x" * 200


def test_voice_presets_dispatch_canned_lines_through_speak() -> None:
    web, _curiosity, _navigation, _published = _web_input()
    agent = AgentSpec()
    web._agent_spec = agent

    ok, _message = web._dispatch_operator_action("speak_invite")

    assert ok is True
    continuation, _context = agent.dispatched[-1]
    assert continuation["tool"] == "speak"
    assert continuation["args"]["text"].startswith("你看起来有点累")
    assert "rest area" in continuation["args"]["text"]

    for preset in (
        "speak_prescribe",
        "speak_escort_start",
        "speak_arrival",
        "speak_farewell",
        "speak_greeting",
    ):
        preset_ok, _message = web._dispatch_operator_action(preset)
        assert preset_ok is True
        continuation, _context = agent.dispatched[-1]
        assert continuation["tool"] == "speak"
        assert continuation["args"]["text"]


def test_every_operator_voice_preset_is_prewarmed() -> None:
    # Every canned line a console button can speak must be in PRESET_LINES,
    # or SpeakSkill's prewarm misses it and that button pays full synthesis.
    from nightwatch import voice_presets
    from nightwatch.webchat import _OPERATOR_ACTIONS

    canned = {
        spec["args"]["text"]
        for spec in _OPERATOR_ACTIONS.values()
        if spec.get("tool") == "speak"
    }
    assert canned
    assert canned <= set(voice_presets.PRESET_LINES)


def test_operator_html_has_voice_panel_with_presets_and_free_text() -> None:
    assert "VOICE 语音" in _OPERATOR_HTML
    for preset in (
        "speak_greeting",
        "speak_invite",
        "speak_prescribe",
        "speak_escort_start",
        "speak_arrival",
        "speak_farewell",
    ):
        assert f"act('{preset}')" in _OPERATOR_HTML
    assert 'id="speakForm"' in _OPERATOR_HTML
    assert 'id="speakText"' in _OPERATOR_HTML
    assert 'maxlength="200"' in _OPERATOR_HTML
    assert "act('speak_text', {text})" in _OPERATOR_HTML


def test_operator_html_has_intake_confirmation_dialog_hooks() -> None:
    # The workbench popup for incoming questionnaire submissions: polls the
    # operator inbox and resolves via PATCH acknowledged/declined.
    assert 'id="intakeDialog"' in _OPERATOR_HTML
    assert 'id="intakeSummary"' in _OPERATOR_HTML
    assert "confirmIntakePrompt()" in _OPERATOR_HTML
    assert "dismissIntakePrompt()" in _OPERATOR_HTML
    assert "/api/form/operator/inbox" in _OPERATOR_HTML
    assert "resolveIntakePrompt('acknowledged')" in _OPERATOR_HTML
    assert "resolveIntakePrompt('declined')" in _OPERATOR_HTML
    assert "window.setInterval(refreshIntakeInbox" in _OPERATOR_HTML


def test_operator_html_has_always_visible_modes_and_manual_fail_safe_hooks() -> None:
    assert "Autonomous" in _OPERATOR_HTML
    assert "Sleep Analysis" in _OPERATOR_HTML
    assert "Manual Override" in _OPERATOR_HTML
    assert "EMERGENCY STOP" in _OPERATOR_HTML
    assert "visibilitychange" in _OPERATOR_HTML
    assert "window.addEventListener('blur', stopManual)" in _OPERATOR_HTML
    assert "setInterval(() =>" in _OPERATOR_HTML


def test_operator_html_offers_manual_qr_intake_trigger() -> None:
    # Bilingual button that fires the server-side manual intake (speak the
    # invite, open the QR session) through the policy server on :8000.
    assert "START INTAKE / 显示二维码" in _OPERATOR_HTML
    assert 'onclick="startIntake()"' in _OPERATOR_HTML
    assert "'/api/robot/action'" in _OPERATOR_HTML
    assert "start_intake" in _OPERATOR_HTML
    # The returned bridge message must surface in the command log.
    assert "setStatusKey(data.message || 'status.completed')" in _OPERATOR_HTML


def test_operator_html_offers_the_auto_escort_consent_switch() -> None:
    # Operator consent switch that makes an unbound public QR/NFC response
    # actionable (AGENTS.md invariant 14). Bilingual, next to START INTAKE,
    # reading live state from the policy server and writing it back on :8000.
    assert "AUTO ESCORT 自动护送" in _OPERATOR_HTML
    assert 'id="autoEscortButton"' in _OPERATOR_HTML
    assert 'onclick="toggleAutoEscort()"' in _OPERATOR_HTML
    assert "'/api/robot/auto_escort'" in _OPERATOR_HTML
    assert "JSON.stringify({enabled: next})" in _OPERATOR_HTML
    # Live ON/OFF state comes from /api/robot/status, never invented locally.
    assert "autoEscortEnabled = robot.auto_escort_enabled === true;" in _OPERATOR_HTML
    assert "renderAutoEscort();" in _OPERATOR_HTML
    assert "button.classList.toggle('active', on);" in _OPERATOR_HTML
    assert "'autoEscort.on': 'ON 已开启'" in _OPERATOR_HTML
    assert "'autoEscort.off': 'OFF 关闭'" in _OPERATOR_HTML
    # The bridge's bilingual confirmation lands in the command log.
    assert "setStatusKey(data.message || 'status.completed')" in _OPERATOR_HTML


def test_auto_escort_switch_is_usable_in_every_operating_mode() -> None:
    # "在自动模式下按需开启或关闭": the toggle must not carry the
    # sleep-analysis-only or manual-only gating other buttons use.
    button = next(
        line
        for line in _OPERATOR_HTML.splitlines()
        if 'id="autoEscortButton"' in line
    )
    assert "cruise-only" not in button
    assert 'class="move"' not in button
    assert "disabled" not in button


def test_operator_views_are_peer_screens_with_main_camera_panel() -> None:
    assert 'data-i18n="header.lidar"' in _OPERATOR_HTML
    assert 'data-i18n="header.firstPerson"' in _OPERATOR_HTML
    assert 'href="/operator/help"' in _OPERATOR_HTML
    # The lidar map stays a peer screen (no embedded iframe or view swapper)…
    assert 'id="lidarPreview"' not in _OPERATOR_HTML
    assert "setVisionPrimary" not in _OPERATOR_HTML
    # …but the main camera with the fatigue-model overlay is part of the
    # operator console (user requirement 2026-07-25: camera + live scores
    # visible, as the original workbench intended).
    assert 'id="robotVideo"' in _OPERATOR_HTML
    assert "ANALYSIS_VIDEO" in _OPERATOR_HTML
