"""Runs inside the sandbox container.

Reads a JSON job from stdin:

    {"kind": "script" | "notebook", "code": str, "files": [{"name": str, "data": base64}]}

For a notebook, "code" is the .ipynb JSON. Its cells run one by one in a real Jupyter
kernel, and the result also has "cells" (per-cell source, text output, images) and
"notebook" (the executed .ipynb, base64).

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
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT = float(os.environ.get("RUN_TIMEOUT", "30"))
NOTEBOOK_TIMEOUT = float(os.environ.get("NOTEBOOK_TIMEOUT", "120"))
MAX_CELL_TEXT = 3000
MAX_IMAGES = 30
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


def run_script(code: str) -> dict:
    script = RUN_DIR / "main.py"
    script.write_text(code)
    out_path = RUN_DIR / "output.log"
    timed_out = False
    start = time.monotonic()
    with open(out_path, "wb") as out, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(script)],
            cwd=WORK_DIR,
            env={**os.environ, "REMOTEPY_SCRIPT": "1"},  # sitecustomize: img.show() etc. save PNGs
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
    return {
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "duration": round(duration, 3),
        "output": raw[:MAX_OUTPUT].decode("utf-8", errors="replace"),
        "output_truncated": len(raw) > MAX_OUTPUT,
    }


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def short_traceback(out: dict) -> str:
    """The notebook's frame where it failed plus the exception line; library internals are
    left out of the chat (the executed .ipynb still has the full traceback)."""
    frames = [ANSI.sub("", f) for f in out.get("traceback", [])]
    user = [f for f in frames if f.lstrip().startswith("Cell In[")]
    last = f"{out.get('ename', 'Error')}: {out.get('evalue', '')}"
    return "\n".join([*user[-1:], last])


def _tail(text: str, limit: int = MAX_CELL_TEXT) -> str:
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def run_notebook(source: str) -> dict:
    import nbformat
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError, CellTimeoutError, DeadKernelError

    try:
        nb = nbformat.reads(source, as_version=4)
    except Exception as exc:
        return {"exit_code": None, "timed_out": False, "duration": 0, "output": "",
                "output_truncated": False, "error": f"Not a valid notebook: {exc}"}
    for cell in nb.cells:
        if cell.cell_type == "code":
            cell.outputs = []
            cell.execution_count = None

    # The kernel should use Jupyter's inline plots, not the plt.show()-to-file backend.
    os.environ.pop("MPLBACKEND", None)
    os.environ["REMOTEPY_NOTEBOOK"] = "1"  # sitecustomize: img.show() displays inline
    client = NotebookClient(nb, kernel_name="python3", allow_errors=False, record_timing=False,
                            resources={"metadata": {"path": str(WORK_DIR)}})
    timed_out, failed, error = False, False, None
    start = time.monotonic()
    try:
        with client.setup_kernel():
            for index, cell in enumerate(nb.cells):
                if cell.cell_type != "code":
                    continue
                remaining = NOTEBOOK_TIMEOUT - (time.monotonic() - start)
                if remaining < 1:
                    timed_out = True
                    break
                client.timeout = int(remaining)
                try:
                    client.execute_cell(cell, index)
                except CellTimeoutError:
                    timed_out = True
                    break
                except CellExecutionError:
                    failed = True  # stop like "Run All" does
                    break
                except DeadKernelError:
                    error = "The kernel died, most likely out of memory."
                    break
    except Exception as exc:
        if error is None and not timed_out and not failed:
            error = f"Could not run the notebook: {exc}"
    duration = time.monotonic() - start

    cells, texts, n_images, img_bytes = [], [], 0, 0
    for index, cell in enumerate(nb.cells):
        if cell.cell_type == "markdown":
            cells.append({"type": "markdown", "source": cell.source[:4000]})
            continue
        if cell.cell_type != "code":
            continue
        outputs: list[dict] = []
        has_error = False

        def add_text(text: str) -> None:
            if outputs and outputs[-1]["kind"] == "text":
                outputs[-1]["text"] += text
            else:
                outputs.append({"kind": "text", "text": text})

        for out in cell.get("outputs", []):
            kind = out.get("output_type")
            if kind == "stream":
                add_text(out.get("text", ""))
            elif kind == "error":
                has_error = True
                add_text(short_traceback(out) + "\n")
            elif kind in ("execute_result", "display_data"):
                data = out.get("data", {})
                png = data.get("image/png")
                if png:
                    raw = base64.b64decode(png)
                    if n_images < MAX_IMAGES and img_bytes + len(raw) <= MAX_FILES_BYTES:
                        n_images += 1
                        img_bytes += len(raw)
                        outputs.append({"kind": "image", "name": f"cell{index + 1}_{n_images}.png",
                                        "data": base64.b64encode(raw).decode()})
                    else:
                        add_text("[image not sent: size limit]\n")
                elif "text/plain" in data:
                    add_text(data["text/plain"] + "\n")
        for o in outputs:
            if o["kind"] == "text":
                texts.append(o["text"])
                o["text"] = _tail(o["text"])
        cells.append({
            "type": "code",
            "execution_count": cell.get("execution_count"),
            "source": cell.source[:4000],
            "outputs": outputs,
            "error": has_error,
        })

    notebook_bytes = nbformat.writes(nb).encode()
    output = "".join(texts)
    result = {
        "exit_code": None if timed_out else (1 if failed or error else 0),
        "timed_out": timed_out,
        "duration": round(duration, 3),
        "output": output[:MAX_OUTPUT],
        "output_truncated": len(output) > MAX_OUTPUT,
        "cells": cells,
    }
    if error:
        result["error"] = error
    if len(notebook_bytes) <= MAX_FILES_BYTES:
        result["notebook"] = base64.b64encode(notebook_bytes).decode()
    return result


def main() -> None:
    job = json.loads(sys.stdin.buffer.read())
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    inputs = write_inputs(job.get("files", []))

    # Pre-built matplotlib font cache, so plots don't rebuild it on every run.
    if Path("/opt/mplcache").is_dir():
        shutil.copytree("/opt/mplcache", os.environ.get("MPLCONFIGDIR", "/tmp/.mpl"), dirs_exist_ok=True)

    # Let the code import .py files that were uploaded as data (import helper).
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (str(WORK_DIR), os.environ.get("PYTHONPATH", "")) if p)

    if job.get("kind") == "notebook":
        result = run_notebook(job["code"])
    else:
        result = run_script(job["code"])

    result["files"], result["files_skipped"] = collect_files(inputs)
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
