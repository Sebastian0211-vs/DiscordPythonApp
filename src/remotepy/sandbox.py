"""Run untrusted Python code in a locked-down, throwaway Docker container.

The script and its data files are piped in over stdin as JSON, so nothing from
the host is mounted into the container. Isolation applied to every run:

- no network (--network none), or a firewalled network that only reaches the
  public internet (SANDBOX_NETWORK=remotepy-net, see sandbox/network-setup.sh)
- read-only root filesystem, small noexec tmpfs for /tmp
- non-root user, all capabilities dropped, no-new-privileges
- memory (no swap), CPU, process count and open-file limits
- hard wall-clock timeout, container killed and removed afterwards
- optional gVisor runtime (SANDBOX_RUNTIME=runsc) for a user-space kernel
"""

import asyncio
import base64
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from .config import SandboxConfig

# Extra seconds on top of the script timeout for container start and result upload.
STARTUP_GRACE = 20.0
# Runs inside a throwaway container on the sandbox network. The firewall drops traffic
# to the VPS, so a connection to the network gateway (the VPS) on a closed port times out.
# Without the firewall the VPS answers straight away (refused), and runs must be stopped.
FIREWALL_PROBE = """
import socket, struct
gateway = None
for line in open("/proc/net/route").read().splitlines()[1:]:
    fields = line.split()
    if len(fields) > 2 and fields[1] == "00000000":
        gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
if gateway is None:
    print("UNKNOWN no default route")
else:
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((gateway, 9))
        print("OPEN", gateway)
    except (socket.timeout, TimeoutError):
        print("FILTERED", gateway)
    except ConnectionRefusedError:
        print("OPEN", gateway)
    except OSError as e:
        print("FILTERED", gateway, e)
"""

# Absolute cap on what we read back from the container (runner output is ~1.4x file size).
HOST_READ_CAP = 16 * 1024 * 1024


@dataclass
class OutputFile:
    name: str
    data: bytes


@dataclass
class CellOutput:
    kind: str  # "text" or "image"
    text: str = ""
    name: str = ""
    data: bytes = b""


@dataclass
class NotebookCell:
    type: str  # "code" or "markdown"
    source: str
    execution_count: int | None = None
    outputs: list[CellOutput] = field(default_factory=list)
    error: bool = False


@dataclass
class RunResult:
    exit_code: int | None
    timed_out: bool
    duration: float
    output: str
    output_truncated: bool = False
    files: list[OutputFile] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    error: str | None = None  # sandbox-level failure (OOM, crash, docker error)
    cells: list[NotebookCell] | None = None  # notebooks only
    notebook: bytes | None = None  # notebooks only: the executed .ipynb

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0


def docker_command(cfg: SandboxConfig, name: str) -> list[str]:
    cmd = [
        cfg.docker_bin, "run",
        "--rm", "-i",
        "--name", name,
        "--label", "remotepy=sandbox",
        "--network", cfg.network,
        "--read-only",
        "--tmpfs", f"/tmp:rw,nosuid,nodev,noexec,size={cfg.tmpfs_size},mode=1777",
        "--user", "10001:10001",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", cfg.memory,
        "--memory-swap", cfg.memory,
        "--cpus", cfg.cpus,
        "--pids-limit", str(cfg.pids_limit),
        "--ulimit", "nofile=256:256",
        "--ulimit", "nproc=256:256",
        "--ipc", "private",
        "--hostname", "sandbox",
        "--log-driver", "none",
        "-e", f"RUN_TIMEOUT={cfg.timeout}",
        "-e", f"NOTEBOOK_TIMEOUT={cfg.notebook_timeout}",
        "-e", f"MAX_OUTPUT_BYTES={cfg.max_output_bytes}",
    ]
    if cfg.runtime:
        cmd += ["--runtime", cfg.runtime]
    cmd.append(cfg.image)
    return cmd


class Sandbox:
    def __init__(self, cfg: SandboxConfig):
        self.cfg = cfg
        self._slots = asyncio.Semaphore(cfg.max_concurrent)

    @property
    def queue_full(self) -> bool:
        return self._slots.locked()

    async def run(self, code: str, files: Sequence[tuple[str, bytes]] = (), notebook: bool = False) -> RunResult:
        """Run a script, or a notebook if `notebook` (then `code` is the .ipynb JSON).
        `files` are (name, content) pairs placed in the working directory."""
        async with self._slots:
            return await self._run(code, files, notebook)

    async def _run(self, code: str, files: Sequence[tuple[str, bytes]], notebook: bool) -> RunResult:
        limit = self.cfg.notebook_timeout if notebook else self.cfg.timeout
        job = json.dumps({
            "kind": "notebook" if notebook else "script",
            "code": code,
            "files": [{"name": n, "data": base64.b64encode(d).decode()} for n, d in files],
        }).encode()
        name = f"remotepy-{uuid.uuid4().hex[:12]}"
        proc = await asyncio.create_subprocess_exec(
            *docker_command(self.cfg, name),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(job)
        await proc.stdin.drain()
        proc.stdin.close()

        async def read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
            buf = bytearray()
            while chunk := await stream.read(65536):
                if len(buf) < cap:
                    buf += chunk[: cap - len(buf)]
            return bytes(buf)

        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.gather(read_capped(proc.stdout, HOST_READ_CAP), read_capped(proc.stderr, 64_000)),
                timeout=limit + STARTUP_GRACE,
            )
            await proc.wait()
        except asyncio.TimeoutError:
            await self._kill(name)
            proc.kill()
            await proc.wait()
            return RunResult(None, True, limit, "", error="Sandbox did not finish in time and was killed.")

        return self._parse(proc.returncode, stdout, stderr)

    async def check_firewall(self) -> str | None:
        """None if runs are safe to start, else a human-readable problem.
        With SANDBOX_NETWORK=none there is nothing to check."""
        if self.cfg.network == "none":
            return None
        cmd = [self.cfg.docker_bin, "run", "--rm", "--network", self.cfg.network, "--label", "remotepy=probe",
               *(["--runtime", self.cfg.runtime] if self.cfg.runtime else []),
               "--entrypoint", "python", self.cfg.image, "-I", "-c", FIREWALL_PROBE]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            return "the firewall check did not finish"
        text = out.decode(errors="replace").strip()
        if proc.returncode != 0 or not text:
            detail = err.decode(errors="replace").strip()[:300]
            if "not found" in detail and self.cfg.network in detail:
                return f"Docker network {self.cfg.network} does not exist"
            return f"the firewall check failed: {detail or 'no output'}"
        if text.startswith("OPEN"):
            return f"the sandbox firewall is not active (the VPS is reachable from {self.cfg.network})"
        return None

    async def _kill(self, name: str) -> None:
        killer = await asyncio.create_subprocess_exec(
            self.cfg.docker_bin, "kill", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()

    @staticmethod
    def _parse(returncode: int | None, stdout: bytes, stderr: bytes) -> RunResult:
        try:
            data = json.loads(stdout)
            cells = None
            if "cells" in data:
                cells = [
                    NotebookCell(
                        type=c["type"],
                        source=c["source"],
                        execution_count=c.get("execution_count"),
                        error=c.get("error", False),
                        outputs=[
                            CellOutput(o["kind"], o.get("text", ""), o.get("name", ""),
                                       base64.b64decode(o["data"]) if "data" in o else b"")
                            for o in c.get("outputs", [])
                        ],
                    )
                    for c in data["cells"]
                ]
            return RunResult(
                exit_code=data["exit_code"],
                timed_out=data["timed_out"],
                duration=data["duration"],
                output=data["output"],
                output_truncated=data["output_truncated"],
                files=[OutputFile(f["name"], base64.b64decode(f["data"])) for f in data["files"]],
                files_skipped=data["files_skipped"],
                error=data.get("error"),
                cells=cells,
                notebook=base64.b64decode(data["notebook"]) if data.get("notebook") else None,
            )
        except (ValueError, KeyError, TypeError):
            pass
        if returncode == 137:
            msg = "Sandbox was killed, most likely out of memory."
        elif returncode in (125, 126, 127):
            msg = f"Docker failed to start the sandbox: {stderr.decode(errors='replace').strip()[:500]}"
        else:
            msg = f"Sandbox crashed (exit {returncode}). {stderr.decode(errors='replace').strip()[:500]}"
        return RunResult(returncode, False, 0.0, "", error=msg)


def _cli() -> None:
    """Try the sandbox without Discord:  remotepy-run script.py  (or pipe code on stdin)."""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Run a Python script in the sandbox")
    parser.add_argument("script", nargs="?", help="path to a .py or .ipynb file (default: stdin)")
    parser.add_argument("--data", metavar="FILE", action="append", default=[], help="data file to include (repeatable)")
    parser.add_argument("--save-files", metavar="DIR", help="write output files into DIR")
    args = parser.parse_args()
    code = Path(args.script).read_text() if args.script else sys.stdin.read()
    data = [(Path(p).name, Path(p).read_bytes()) for p in args.data]

    is_nb = bool(args.script and args.script.endswith(".ipynb"))
    result = asyncio.run(Sandbox(SandboxConfig.from_env()).run(code, data, notebook=is_nb))
    print(result.output, end="" if result.output.endswith("\n") or not result.output else "\n")
    status = result.error or ("TIMEOUT" if result.timed_out else f"exit {result.exit_code}")
    print(f"--- {status} in {result.duration:.2f}s, files: {[f.name for f in result.files]}", file=sys.stderr)
    if args.save_files:
        out = Path(args.save_files)
        out.mkdir(parents=True, exist_ok=True)
        for f in result.files:
            (out / Path(f.name).name).write_bytes(f.data)
        for cell in result.cells or []:
            for o in cell.outputs:
                if o.kind == "image":
                    (out / o.name).write_bytes(o.data)
        if result.notebook:
            (out / "executed.ipynb").write_bytes(result.notebook)
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    _cli()
