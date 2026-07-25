"""Authoritative operating-mode and command-epoch arbitration.

This module is deliberately independent of DimOS and hardware so every mode
transition can be proven with ordinary unit tests. The curiosity supervisor is
the only runtime owner of one ``ModeController`` instance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
import time

from nightwatch.contracts import InteractionState, OperatingMode


_MODE_ALIASES = {
    "exploration": OperatingMode.AUTONOMOUS,
    "explore": OperatingMode.AUTONOMOUS,
    "autonomous": OperatingMode.AUTONOMOUS,
    "cruise": OperatingMode.SLEEP_ANALYSIS,
    "sleep": OperatingMode.SLEEP_ANALYSIS,
    "sleepiness": OperatingMode.SLEEP_ANALYSIS,
    "sleep_analysis": OperatingMode.SLEEP_ANALYSIS,
    "manual": OperatingMode.MANUAL,
}

_SCAN_STATES = {
    InteractionState.SCHEDULED_SCAN,
    InteractionState.FORCED_SCAN,
}


@dataclass(frozen=True, slots=True)
class ModeSnapshot:
    mode: OperatingMode
    epoch: int
    interaction_state: InteractionState
    scan_requested: bool
    manual_sequence: int
    manual_command_age_s: float | None


@dataclass(frozen=True, slots=True)
class Transition:
    changed: bool
    previous: OperatingMode
    current: OperatingMode
    epoch: int


class ModeController:
    """Thread-safe source of truth for mode, scan, and manual command epochs."""

    def __init__(
        self,
        initial: OperatingMode = OperatingMode.AUTONOMOUS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = RLock()
        self._clock = clock
        self._mode = initial
        self._last_autonomous = (
            initial
            if initial is not OperatingMode.MANUAL
            else OperatingMode.AUTONOMOUS
        )
        self._epoch = 0
        self._interaction_state = InteractionState.IDLE
        self._scan_requested = False
        self._manual_sequence = 0
        self._manual_command_at: float | None = None

    @staticmethod
    def parse_mode(value: str | OperatingMode) -> OperatingMode:
        if isinstance(value, OperatingMode):
            return value
        normalized = str(value).strip().lower().replace("-", "_")
        try:
            return _MODE_ALIASES[normalized]
        except KeyError as exc:
            choices = ", ".join(mode.value for mode in OperatingMode)
            raise ValueError(
                f"unsupported operating mode {value!r}; choose {choices}"
            ) from exc

    def transition(self, value: str | OperatingMode) -> Transition:
        selected = self.parse_mode(value)
        with self._lock:
            previous = self._mode
            if selected is previous:
                return Transition(False, previous, selected, self._epoch)
            if previous is not OperatingMode.MANUAL:
                self._last_autonomous = previous
            if selected is not OperatingMode.MANUAL:
                self._last_autonomous = selected
            self._mode = selected
            self._epoch += 1
            self._interaction_state = InteractionState.IDLE
            self._scan_requested = False
            self._manual_command_at = None
            return Transition(True, previous, selected, self._epoch)

    def resume_autonomy(self) -> Transition:
        """Leave Manual Override for the most recently selected auto mission."""
        with self._lock:
            target = self._last_autonomous
        return self.transition(target)

    def accept_manual(
        self,
        *,
        epoch: int,
        sequence: int,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Accept one in-order command for the current Manual mode epoch."""

        with self._lock:
            if self._mode is not OperatingMode.MANUAL:
                return False, "manual override is not active"
            if int(epoch) != self._epoch:
                return False, "stale mode epoch"
            if int(sequence) <= self._manual_sequence:
                return False, "stale manual sequence"
            self._manual_sequence = int(sequence)
            self._manual_command_at = self._clock() if now is None else float(now)
            return True, "accepted"

    def manual_expired(
        self,
        *,
        timeout_s: float = 0.5,
        now: float | None = None,
    ) -> bool:
        with self._lock:
            if (
                self._mode is not OperatingMode.MANUAL
                or self._manual_command_at is None
            ):
                return False
            current = self._clock() if now is None else float(now)
            return current - self._manual_command_at > max(0.0, timeout_s)

    def request_scan(self) -> tuple[bool, str]:
        with self._lock:
            if self._mode is not OperatingMode.SLEEP_ANALYSIS:
                return False, "sleep scan requires Sleep Analysis mode"
            if self._interaction_state not in {
                InteractionState.IDLE,
                *_SCAN_STATES,
            }:
                return False, "a person interaction or escort is active"
            self._scan_requested = True
            return True, "accepted"

    def consume_scan_request(self) -> bool:
        with self._lock:
            requested = self._scan_requested
            self._scan_requested = False
            return requested

    def begin_scan(self, *, forced: bool) -> bool:
        with self._lock:
            if self._mode is not OperatingMode.SLEEP_ANALYSIS:
                return False
            if self._interaction_state not in {
                InteractionState.IDLE,
                *_SCAN_STATES,
            }:
                return False
            self._interaction_state = (
                InteractionState.FORCED_SCAN
                if forced
                else InteractionState.SCHEDULED_SCAN
            )
            return True

    def set_interaction(self, state: InteractionState) -> bool:
        with self._lock:
            if (
                state is not InteractionState.IDLE
                and self._mode is not OperatingMode.SLEEP_ANALYSIS
            ):
                return False
            self._interaction_state = state
            if state not in _SCAN_STATES:
                self._scan_requested = False
            return True

    def end_scan(self) -> None:
        with self._lock:
            if self._interaction_state in _SCAN_STATES:
                self._interaction_state = InteractionState.IDLE
            self._scan_requested = False

    def snapshot(self, *, now: float | None = None) -> ModeSnapshot:
        with self._lock:
            current = self._clock() if now is None else float(now)
            age = (
                None
                if self._manual_command_at is None
                else max(0.0, current - self._manual_command_at)
            )
            return ModeSnapshot(
                mode=self._mode,
                epoch=self._epoch,
                interaction_state=self._interaction_state,
                scan_requested=self._scan_requested,
                manual_sequence=self._manual_sequence,
                manual_command_age_s=age,
            )
