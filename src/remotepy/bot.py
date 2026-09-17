"""Discord user-installable app: run Python from any chat, including private group DMs.

The app is installed on your Discord account (not added to a server), so it can't
read chat messages. You trigger it explicitly:

  /run                      opens a box to paste code
  /run file:<script.py>     runs an attached .py file
  Right-click a message > Apps > Run Python
                            runs the ```python block or .py file in that message
  /ids                      shows your user ID and this chat's ID (only to you)

The output is posted publicly in the chat, so everyone in the group sees it.
"""

import hashlib
import io
import logging

import discord
from discord import app_commands

from .config import BotConfig, SandboxConfig
from .messages import extract_code_block, format_result, status_line
from .sandbox import Sandbox

log = logging.getLogger("remotepy")

# Installed on user accounts only, usable in servers, the bot DM and group DMs.
INSTALLS = app_commands.AppInstallationType(guild=False, user=True)
CONTEXTS = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)


class CodeModal(discord.ui.Modal, title="Run Python"):
    code = discord.ui.TextInput(
        label="Code",
        style=discord.TextStyle.paragraph,
        placeholder="import numpy as np\nprint(np.arange(10).sum())",
        max_length=4000,
    )

    def __init__(self, bot: "RunnerBot"):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.code.value
        code = extract_code_block(raw, any_language=True) or raw  # tolerate pasted ``` fences
        await self.bot.execute(interaction, code, label="some code", attach_code=True)


class RunnerBot(discord.Client):
    def __init__(self, cfg: BotConfig, sandbox: Sandbox):
        super().__init__(
            intents=discord.Intents.none(),  # interactions only, no message reading
            # Script output must never ping anyone.
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.cfg = cfg
        self.sandbox = sandbox
        self.tree = app_commands.CommandTree(self, allowed_installs=INSTALLS, allowed_contexts=CONTEXTS)
        self._register_commands()

    async def setup_hook(self) -> None:
        synced = await self.tree.sync()
        log.info("Synced %d global commands: %s", len(synced), [c.name for c in synced])

    async def on_ready(self) -> None:
        log.info("Logged in as %s (app id %s)", self.user, self.application_id)

    # ---------------------------------------------------------------- commands
    def _register_commands(self) -> None:
        bot = self

        @self.tree.command(name="run", description="Run Python in the sandbox and post the output here")
        @app_commands.describe(file="A .py file to run. Leave empty to paste code instead.")
        async def run(interaction: discord.Interaction, file: discord.Attachment | None = None) -> None:
            if not await bot._check_allowed(interaction):
                return
            if file is None:
                await interaction.response.send_modal(CodeModal(bot))
                return
            if not file.filename.lower().endswith(".py"):
                await interaction.response.send_message("That isn't a `.py` file.", ephemeral=True)
                return
            if file.size > bot.cfg.max_code_bytes:
                await interaction.response.send_message(
                    f"`{file.filename}` is too large (max {bot.cfg.max_code_bytes // 1000} KB).", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            code = (await file.read()).decode("utf-8", errors="replace")
            await bot.execute(interaction, code, label=f"`{file.filename}`", attach_code=True, deferred=True)

        async def run_message(interaction: discord.Interaction, message: discord.Message) -> None:
            if not await bot._check_allowed(interaction):
                return
            att = next((a for a in message.attachments if a.filename.lower().endswith(".py")), None)
            if att is not None:
                if att.size > bot.cfg.max_code_bytes:
                    await interaction.response.send_message("That file is too large.", ephemeral=True)
                    return
                await interaction.response.defer(thinking=True)
                code = (await att.read()).decode("utf-8", errors="replace")
                await bot.execute(interaction, code, label=f"[`{att.filename}`]({message.jump_url})", deferred=True)
                return
            code = extract_code_block(message.content, any_language=True)
            if code is None:
                await interaction.response.send_message(
                    "No code found: the message needs a ```python block or a `.py` file.", ephemeral=True)
                return
            if len(code.encode()) > bot.cfg.max_code_bytes:
                await interaction.response.send_message("That code block is too large.", ephemeral=True)
                return
            await bot.execute(interaction, code, label=f"[a code block]({message.jump_url})")

        self.tree.add_command(app_commands.ContextMenu(name="Run Python", callback=run_message))

        @self.tree.command(name="ids", description="Show your user ID and this chat's ID (only visible to you)")
        async def ids(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                f"User ID: `{interaction.user.id}`\nThis chat's ID: `{interaction.channel_id}`", ephemeral=True)

    # ----------------------------------------------------------------- helpers
    async def _check_allowed(self, interaction: discord.Interaction) -> bool:
        reason = None
        if interaction.user.id not in self.cfg.user_ids:
            reason = "You're not allowed to run code with this app."
        elif self.cfg.channel_ids and interaction.channel_id not in self.cfg.channel_ids:
            reason = "Running code isn't enabled in this chat."
        if reason:
            log.warning("Denied %s (%s) in channel %s: %s",
                        interaction.user, interaction.user.id, interaction.channel_id, reason)
            await interaction.response.send_message(reason, ephemeral=True)
            return False
        return True

    async def execute(self, interaction: discord.Interaction, code: str, *, label: str,
                      attach_code: bool = False, deferred: bool = False) -> None:
        """Run code and post the result publicly as the interaction's response."""
        if not deferred:
            await interaction.response.defer(thinking=True)

        digest = hashlib.sha256(code.encode()).hexdigest()[:12]
        log.info("Run by %s (%s) in channel %s: %s sha256=%s",
                 interaction.user, interaction.user.id, interaction.channel_id, label, digest)
        try:
            result = await self.sandbox.run(code)
        except Exception:
            log.exception("Sandbox failure")
            await interaction.followup.send("💥 Internal error while running the script (see bot logs).")
            return

        log.info("Run sha256=%s finished: %s", digest, status_line(result)[1])
        prefix = f"{interaction.user.mention} ran {label}\n"
        content, attachments = format_result(result, prefix=prefix)
        if attach_code:
            attachments = [("main.py", code.encode()), *attachments][:10]
        files = [discord.File(io.BytesIO(data), filename=name) for name, data in attachments]
        try:
            await interaction.followup.send(content, files=files)
        except discord.HTTPException as exc:
            log.warning("Reply with files failed (%s), retrying without", exc)
            await interaction.followup.send(content[:1900] + "\n-# (attachments could not be uploaded)")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = BotConfig.from_env()
    bot = RunnerBot(cfg, Sandbox(SandboxConfig.from_env()))
    bot.run(cfg.token, log_handler=None)


if __name__ == "__main__":
    main()
