from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from pathlib import Path


def sha256sum(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(
    url: str,
    destination: Path,
    *,
    expected_sha256: str | None = None,
    timeout: int = 60,
) -> Path:
    """Download a file atomically, optionally checking its SHA-256 digest."""
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        if expected_sha256 is None or sha256sum(destination) == expected_sha256:
            return destination
        print(f"Checksum mismatch; downloading {destination.name} again.", file=sys.stderr)

    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "RobotDog-fatigue-demo/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response, partial.open("wb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            output.write(block)
            downloaded += len(block)
            if total:
                percent = 100.0 * downloaded / total
                print(
                    f"\rDownloading {destination.name}: {percent:5.1f}%"
                    f" ({downloaded / 1024 / 1024:.1f} MB)",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
    if total:
        print(file=sys.stderr)

    if expected_sha256:
        actual = sha256sum(partial)
        if actual != expected_sha256:
            partial.unlink(missing_ok=True)
            raise RuntimeError(
                f"SHA-256 mismatch for {destination.name}: "
                f"expected {expected_sha256}, got {actual}"
            )
    os.replace(partial, destination)
    return destination

