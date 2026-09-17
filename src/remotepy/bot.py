"""Discord bot: runs Python posted in allowed channels and replies with the output.

Triggers, from allowed users in allowed channels (and threads inside them):
  - a message with a .py attachment
  - a message containing a ```python (or ```py) code block
"""

import hashlib
import io
import logging

import discord

from .config import BotConfig, SandboxConfig
from .messages import extract_code_block, format_result, status_line
from .sandbox import Sandbox

log = logging.getLogger("remotepy")

QUEUED = "⏳"


class RunnerBot(discord.Client):
    def __init__(self, cfg: BotConfig, sandbox: Sandbox):
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable it in the Developer Portal
        super().__init__(
            intents=intents,
            # Script output must never ping @everyone, roles or users.
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.cfg = cfg
        self.sandbox = sandbox

    async def on_ready(self) -> None:
        log.info("Logged in as %s, listening in channels %s", self.user, sorted(self.cfg.channel_ids))

    def _channel_allowed(self, channel: discord.abc.Messageable) -> bool:
        ids = {getattr(channel, "id", None), getattr(channel, "parent_id", None)}
        return bool(ids & self.cfg.channel_ids)

    def _user_allowed(self, author: discord.abc.User) -> bool:
        if author.id in self.cfg.user_ids:
            return True
        roles = getattr(author, "roles", [])
        return any(role.id in self.cfg.role_ids for role in roles)

    async def _extract(self, message: discord.Message) -> tuple[str, str] | None:
        """Return (code, source label) or None if the message has no code to run."""
        for att in message.attachments:
            if att.filename.lower().endswith(".py"):
                if att.size > self.cfg.max_code_bytes:
                    raise ValueError(f"{att.filename} is too large ({att.size} bytes, max {self.cfg.max_code_bytes})")
                raw = await att.read()
                return raw.decode("utf-8", errors="replace"), att.filename
        block = extract_code_block(message.content)
        if block is not None:
            if len(block.encode()) > self.cfg.max_code_bytes:
                raise ValueError(f"code block is too large (max {self.cfg.max_code_bytes} bytes)")
            return block, "code block"
        return None

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self._channel_allowed(message.channel):
            return
        try:
            found = await self._extract(message)
        except ValueError as exc:
            await message.reply(f"🚫 {exc}", mention_author=False)
            return
        if found is None:
            return
        if not self._user_allowed(message.author):
            log.warning("Denied run from %s (%s)", message.author, message.author.id)
            await message.add_reaction("🚫")
            return

        code, source = found
        digest = hashlib.sha256(code.encode()).hexdigest()[:12]
        log.info("Run requested by %s (%s) in #%s: %s sha256=%s",
                 message.author, message.author.id, message.channel, source, digest)

        await message.add_reaction(QUEUED)
        try:
            async with message.channel.typing():
                result = await self.sandbox.run(code)
        except Exception:
            log.exception("Sandbox failure")
            await message.reply("💥 Internal error while running the script (see bot logs).", mention_author=False)
            return
        finally:
            try:
                await message.remove_reaction(QUEUED, self.user)
            except discord.HTTPException:
                pass

        emoji, status = status_line(result)
        log.info("Run sha256=%s finished: %s", digest, status)
        content, attachments = format_result(result)
        files = [discord.File(io.BytesIO(data), filename=name) for name, data in attachments]
        try:
            await message.reply(content, files=files, mention_author=False)
        except discord.HTTPException as exc:
            # Usually attachments too large for this server's upload limit.
            log.warning("Reply with files failed (%s), retrying without", exc)
            await message.reply(content[:1900] + "\n-# (attachments could not be uploaded)", mention_author=False)
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = BotConfig.from_env()
    bot = RunnerBot(cfg, Sandbox(SandboxConfig.from_env()))
    bot.run(cfg.token, log_handler=None)


if __name__ == "__main__":
    main()
