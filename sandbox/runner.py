"""Runs inside the sandbox container.

Reads the user's script from stdin, runs it with a timeout, then prints ONE
JSON document on stdout:

    {"exit_code": int | null, "timed_out": bool, "duration": float,
     "output": str, "output_truncated": bool,
     "files": [{"name": str, "data": base64}], "files_skipped": [str]}

stdout and stderr of the script are merged, like in a terminal.
Any file the script writes in its working directory (e.g. plt.savefig("plot.png"))
is sent back as an attachment, within limits.
"""

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT = float(os.environ.get("RUN_TIMEOUT", "30"))
MAX_OUTPUT = int(os.environ.get("MAX_OUTPUT_BYTES", str(1_000_000)))
MAX_FILES = int(os.environ.get("MAX_FILES", "9"))
MAX_FILES_BYTES = int(os.environ.get("MAX_FILES_BYTES", str(8 * 1024 * 1024)))

RUN_DIR = Path("/tmp/.run")
WORK_DIR = Path("/tmp/work")


def collect_files() -> tuple[list[dict], list[str]]:
    files, skipped, total = [], [], 0
    for path in sorted(WORK_DIR.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = str(path.relative_to(WORK_DIR))
        size = path.stat().st_size
        if len(files) >= MAX_FILES or total + size > MAX_FILES_BYTES:
            skipped.append(rel)
            continue
        total += size
        files.append({"name": rel.replace("/", "_"), "data": base64.b64encode(path.read_bytes()).decode()})
    return files, skipped


def main() -> None:
    code = sys.stdin.buffer.read()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    script = RUN_DIR / "main.py"
    script.write_bytes(code)
    out_path = RUN_DIR / "output.log"

    # Pre-built matplotlib font cache, so plots don't rebuild it on every run.
    if Path("/opt/mplcache").is_dir():
        shutil.copytree("/opt/mplcache", os.environ.get("MPLCONFIGDIR", "/tmp/.mpl"), dirs_exist_ok=True)

    timed_out = False
    start = time.monotonic()
    with open(out_path, "wb") as out, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(script)],
            cwd=WORK_DIR,
            stdin=devnull,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group, so we can kill children too
        )
        try:
            proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    duration = time.monotonic() - start

    with open(out_path, "rb") as f:
        raw = f.read(MAX_OUTPUT + 1)
    truncated = len(raw) > MAX_OUTPUT
    output = raw[:MAX_OUTPUT].decode("utf-8", errors="replace")

    files, skipped = collect_files()
    result = {
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "duration": round(duration, 3),
        "output": output,
        "output_truncated": truncated,
        "files": files,
        "files_skipped": skipped,
    }
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
