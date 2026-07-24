"""End-to-end chat test against the live stack, no browser needed.

Opens the SSE response stream (exactly what the web page consumes), POSTs a
query to /submit_query (exactly what the page's input box does), and prints
every response line for N seconds. Exit code 0 iff at least one agent
response arrived.

Usage: python -m nightwatch.e2e "wave hello" [timeout_seconds]
"""

import sys
import threading
import time

import requests

BASE = "http://localhost:5555"


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "what is your battery level?"
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0

    got: list[str] = []
    stop = threading.Event()

    def listen() -> None:
        try:
            with requests.get(
                f"{BASE}/text_stream/agent_responses", stream=True, timeout=timeout + 10
            ) as r:
                for raw in r.iter_lines(decode_unicode=True):
                    if stop.is_set():
                        return
                    if raw and raw.startswith("data:"):
                        line = raw[5:].strip()
                        if line:
                            got.append(line)
                            print(f"  [response] {line}", flush=True)
        except Exception as e:
            print(f"  [listener error] {e}", flush=True)

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    time.sleep(1.5)  # listener attached before we send

    print(f"[send] {query!r}", flush=True)
    resp = requests.post(f"{BASE}/submit_query", data={"query": query}, timeout=10)
    print(f"[submit_query] HTTP {resp.status_code}", flush=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if got:
            # keep listening a little longer for follow-up lines
            time.sleep(6)
            break
        time.sleep(0.5)
    stop.set()

    print(f"[result] {len(got)} response line(s)")
    return 0 if got else 1


if __name__ == "__main__":
    sys.exit(main())
