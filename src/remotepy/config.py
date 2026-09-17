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
    network: str = "none"  # "remotepy-net" (see sandbox/network-setup.sh) for internet access
    timeout: float = 30.0  # seconds a script may run
    notebook_timeout: float = 120.0  # seconds a whole notebook may run
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
            network=e("SANDBOX_NETWORK", cls.network) or "none",
            timeout=float(e("RUN_TIMEOUT", cls.timeout)),
            notebook_timeout=float(e("NOTEBOOK_TIMEOUT", cls.notebook_timeout)),
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
    user_ids: frozenset[int]
    channel_ids: frozenset[int] = field(default_factory=frozenset)  # empty = any chat
    max_code_bytes: int = 100_000
    max_data_bytes: int = 8_000_000  # total size of data files per run
    pages_url: str = ""  # public base URL of the web container, e.g. https://plots.example.com
    pages_dir: str = "/pages"
    pages_ttl_days: float = 7.0

    @classmethod
    def from_env(cls) -> "BotConfig":
        cfg = cls(
            token=os.environ.get("DISCORD_TOKEN", ""),
            user_ids=_ids("ALLOWED_USER_IDS"),
            channel_ids=_ids("ALLOWED_CHANNEL_IDS"),
            pages_url=os.environ.get("PAGES_URL", "").strip(),
            pages_dir=os.environ.get("PAGES_DIR", cls.pages_dir),
            pages_ttl_days=float(os.environ.get("PAGES_TTL_DAYS", cls.pages_ttl_days)),
        )
        if not cfg.token:
            raise SystemExit("DISCORD_TOKEN is not set")
        if not cfg.user_ids:
            raise SystemExit("ALLOWED_USER_IDS is empty: nobody would be allowed to run code")
        return cfg
