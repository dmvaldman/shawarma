"""Stop only command processes rooted in one validated agent checkout."""

import json
import os
import signal
import sys
import time
from pathlib import Path
from uuid import UUID


def cancel(directory: str) -> None:
    path = Path(directory)
    if len(path.parts) != 4 or path.parts[:2] != ("/", "workspaces"):
        raise ValueError("Invalid agent workspace")
    UUID(path.parts[2])
    UUID(path.parts[3])
    target = str(path)
    tracked: dict[int, str] = {}
    processes: dict[int, tuple[int, str]] = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        pid = int(proc.name)
        if pid == os.getpid():
            continue
        try:
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            parent, born = int(fields[1]), fields[19]
            processes[pid] = (parent, born)
            cwd = os.readlink(proc / "cwd").removesuffix(" (deleted)")
            if cwd == target or cwd.startswith(target + "/"):
                tracked[pid] = born
        except (OSError, ValueError):
            continue
    # Descendants may have changed directory; preserve original PID birth times.
    while True:
        children = {
            pid: born
            for pid, (parent, born) in processes.items()
            if parent in tracked and pid not in tracked
        }
        if not children:
            break
        tracked.update(children)

    def alive(pid: int, born: str) -> bool:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[19] == born and fields[0] != "Z"
        except (OSError, ValueError):
            return False

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, born in tracked.items():
            if alive(pid, born):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        time.sleep(0.5)
    if any(alive(pid, born) for pid, born in tracked.items()):
        raise RuntimeError("Agent commands did not stop")


if __name__ == "__main__":
    cancel(json.load(sys.stdin)["directory"])
    print('{"stopped":true}')
