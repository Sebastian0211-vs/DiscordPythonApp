"""Runs inside the sandbox container.

Reads a JSON job from stdin:

    {"code": str, "files": [{"name": str, "data": base64}]}

The data files are written into the script's working directory, so the script can
open them by name (pd.read_csv("data.csv")) or import them (import helper).

Then it runs the script with a timeout and prints ONE JSON document on stdout:

    {"exit_code": int | null, "timed_out": bool, "duration": float,
     "output": str, "output_truncated": bool,
     "files": [{"name": str, "data": base64}], "files_skipped": [str]}

stdout and stderr of the script are merged, like in a terminal.
Files the script creates or modifies in its working directory (plt.savefig("plot.png"))
are sent back; input files it left untouched are not.
"""

import base64
import hashlib
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
MAX_FILES = int(os.environ.get("MAX_FILES", "8"))
MAX_FILES_BYTES = int(os.environ.get("MAX_FILES_BYTES", str(8 * 1024 * 1024)))

RUN_DIR = Path("/tmp/.run")
WORK_DIR = Path("/tmp/work")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_inputs(files: list[dict]) -> dict[str, str]:
    """Write data files into WORK_DIR by base name only. Returns {name: sha256}."""
    written = {}
    for f in files:
        name = Path(str(f.get("name", ""))).name
        if not name or name.startswith("."):
            continue
        path = WORK_DIR / name
        path.write_bytes(base64.b64decode(f["data"]))
        written[name] = _sha(path)
    return written


def collect_files(inputs: dict[str, str]) -> tuple[list[dict], list[str]]:
    files, skipped, total = [], [], 0
    for path in sorted(WORK_DIR.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = str(path.relative_to(WORK_DIR))
        if rel in inputs and _sha(path) == inputs[rel]:
            continue  # an input file the script didn't change
        size = path.stat().st_size
        if len(files) >= MAX_FILES or total + size > MAX_FILES_BYTES:
            skipped.append(rel)
            continue
        total += size
        files.append({"name": rel.replace("/", "_"), "data": base64.b64encode(path.read_bytes()).decode()})
    return files, skipped


def main() -> None:
    job = json.loads(sys.stdin.buffer.read())
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    script = RUN_DIR / "main.py"
    script.write_text(job["code"])
    inputs = write_inputs(job.get("files", []))
    out_path = RUN_DIR / "output.log"

    # Pre-built matplotlib font cache, so plots don't rebuild it on every run.
    if Path("/opt/mplcache").is_dir():
        shutil.copytree("/opt/mplcache", os.environ.get("MPLCONFIGDIR", "/tmp/.mpl"), dirs_exist_ok=True)

    # Let the script import .py files that were uploaded as data (import helper).
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(WORK_DIR), env.get("PYTHONPATH", "")) if p)

    timed_out = False
    start = time.monotonic()
    with open(out_path, "wb") as out, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(script)],
            cwd=WORK_DIR,
            env=env,
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

    files, skipped = collect_files(inputs)
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
