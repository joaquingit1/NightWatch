"""Standalone manual-override teleop for Night Watch.

Sends twist/stop JSON to the DimOS RerunWebSocketServer (default port 3030),
the same inbound channel the Dimensional viewer uses. Inside the stack those
messages publish on tele_cmd_vel, which MovementManager treats as an override:
any teleop message cancels the active navigation goal instantly and locks out
autonomy until a cooldown expires.

(A zenoh publisher from an external process is unreliable on a multi-interface
macOS box: peer scouting advertises the wrong interfaces. The websocket path
terminates inside the mesh, so it always works.)

Usage (own terminal, while a DimOS stack is running):
    cd ~/hackathon2/dimos && source .venv/bin/activate
    python -m nightwatch.teleop

Keys:
    W/S  forward / backward          Q/E  strafe left / right
    A/D  turn left / right           SPACE  EMERGENCY STOP
    Shift  2x boost                  Ctrl   0.5x slow
    ESC / close window  quit (sends one final stop)
"""

import json
import time

import pygame
from websockets.sync.client import connect

WS_URL = "ws://localhost:3030/ws"
LINEAR_SPEED = 0.5  # m/s
ANGULAR_SPEED = 0.8  # rad/s
BOOST = 2.0
SLOW = 0.5
RATE_HZ = 10

HELP_LINES = [
    "NIGHT WATCH MANUAL OVERRIDE",
    "",
    "W/S forward/back    A/D turn",
    "Q/E strafe          SPACE E-STOP",
    "Shift boost         Ctrl slow",
    "ESC quit",
    "",
    "Overrides autonomy while keys held.",
]


class Link:
    """Websocket link that survives stack restarts by reconnecting lazily."""

    def __init__(self) -> None:
        self._ws = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def _send(self, payload: dict) -> None:
        for _attempt in (1, 2):
            if self._ws is None:
                try:
                    self._ws = connect(WS_URL, open_timeout=2)
                except Exception:
                    return  # stack down; drop this message, retry on next tick
            try:
                self._ws.send(json.dumps(payload))
                return
            except Exception:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None  # reconnect on second attempt / next tick

    def send_twist(self, x: float, y: float, wz: float) -> None:
        self._send(
            {
                "type": "twist",
                "linear_x": x,
                "linear_y": y,
                "linear_z": 0.0,
                "angular_x": 0.0,
                "angular_y": 0.0,
                "angular_z": wz,
            }
        )

    def send_stop(self) -> None:
        self._send({"type": "stop"})

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass


def main() -> None:
    link = Link()
    send_twist = link.send_twist
    send_stop = link.send_stop

    pygame.init()
    screen = pygame.display.set_mode((460, 320))
    pygame.display.set_caption("Night Watch Override")
    font = pygame.font.Font(None, 26)
    clock = pygame.time.Clock()

    held: set[int] = set()
    was_active = False
    estop_flash_until = 0.0
    running = True

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_SPACE:
                    held.clear()
                    send_stop()
                    estop_flash_until = time.monotonic() + 1.0
                else:
                    held.add(event.key)
            elif event.type == pygame.KEYUP:
                held.discard(event.key)

        x = y = wz = 0.0
        if pygame.K_w in held:
            x += LINEAR_SPEED
        if pygame.K_s in held:
            x -= LINEAR_SPEED
        if pygame.K_q in held:
            y += LINEAR_SPEED
        if pygame.K_e in held:
            y -= LINEAR_SPEED
        if pygame.K_a in held:
            wz += ANGULAR_SPEED
        if pygame.K_d in held:
            wz -= ANGULAR_SPEED

        mult = 1.0
        if held & {pygame.K_LSHIFT, pygame.K_RSHIFT}:
            mult = BOOST
        elif held & {pygame.K_LCTRL, pygame.K_RCTRL}:
            mult = SLOW
        x, y, wz = x * mult, y * mult, wz * mult

        active = bool(x or y or wz)
        # Send while driving; exactly one stop on release so autonomy can
        # resume after MovementManager's cooldown instead of being starved.
        if active:
            send_twist(x, y, wz)
        elif was_active:
            send_stop()
        was_active = active

        estop = time.monotonic() < estop_flash_until
        screen.fill((120, 20, 20) if estop else (18, 18, 24))
        status = "E-STOP SENT" if estop else ("DRIVING" if active else "idle (autonomy free)")
        lines = [*HELP_LINES, "", f"status: {status}", "click this window to give it focus"]
        for i, line in enumerate(lines):
            screen.blit(font.render(line, True, (240, 240, 240)), (16, 12 + i * 24))
        pygame.display.flip()
        clock.tick(RATE_HZ)

    send_stop()
    link.close()
    pygame.quit()


if __name__ == "__main__":
    main()
