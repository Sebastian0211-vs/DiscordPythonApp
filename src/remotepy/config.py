"""Settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field


def _ids(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "")
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class SandboxConfig:
    image: str = "remotepy-sandbox:latest"
    runtime: str = ""  # e.g. "runsc" for gVisor; empty = Docker default (runc)
    timeout: float = 30.0  # seconds the script may run
    memory: str = "512m"
    cpus: str = "1.0"
    pids_limit: int = 128
    tmpfs_size: str = "128m"  # scratch space for the script and its output files
    max_concurrent: int = 2
    max_output_bytes: int = 1_000_000
    docker_bin: str = "docker"

    @classmethod
    def from_env(cls) -> "SandboxConfig":
        e = os.environ.get
        return cls(
            image=e("SANDBOX_IMAGE", cls.image),
            runtime=e("SANDBOX_RUNTIME", cls.runtime),
            timeout=float(e("RUN_TIMEOUT", cls.timeout)),
            memory=e("SANDBOX_MEMORY", cls.memory),
            cpus=e("SANDBOX_CPUS", cls.cpus),
            pids_limit=int(e("SANDBOX_PIDS", cls.pids_limit)),
            tmpfs_size=e("SANDBOX_TMPFS", cls.tmpfs_size),
            max_concurrent=int(e("MAX_CONCURRENT", cls.max_concurrent)),
            docker_bin=e("DOCKER_BIN", cls.docker_bin),
        )


@dataclass(frozen=True)
class BotConfig:
    token: str
    channel_ids: frozenset[int]
    user_ids: frozenset[int] = field(default_factory=frozenset)
    role_ids: frozenset[int] = field(default_factory=frozenset)
    max_code_bytes: int = 100_000

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.environ.get("DISCORD_TOKEN", "")
        cfg = cls(
            token=token,
            channel_ids=_ids("ALLOWED_CHANNEL_IDS"),
            user_ids=_ids("ALLOWED_USER_IDS"),
            role_ids=_ids("ALLOWED_ROLE_IDS"),
        )
        if not token:
            raise SystemExit("DISCORD_TOKEN is not set")
        if not cfg.channel_ids:
            raise SystemExit("ALLOWED_CHANNEL_IDS is empty: set the private channel(s) the bot listens in")
        if not (cfg.user_ids or cfg.role_ids):
            raise SystemExit("Set ALLOWED_USER_IDS and/or ALLOWED_ROLE_IDS: nobody is allowed to run code otherwise")
        return cfg
