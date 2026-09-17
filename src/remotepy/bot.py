"""Discord user-installable app: run Python from any chat, including private group DMs.

The app is installed on your Discord account (not added to a server), so it can't
read chat messages. You trigger it explicitly:

  /run                          opens a box to paste code
  /run file:<script.py>         runs an attached .py file
  /run data1:<data.csv> ...     adds up to 3 data files (with either of the above)
  Right-click a message > Apps > Run Python
                                runs the ```python block or .py file in that message;
                                the message's other attachments are the data files
  /ids                          shows your user ID and this chat's ID (only to you)

Data files land in the script's working directory: pd.read_csv("data.csv") just works,
and extra .py files can be imported. The output is posted publicly in the chat.
"""

import hashlib
import io
import logging
from collections.abc import Sequence

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
        placeholder="import pandas as pd\nprint(pd.read_csv('data.csv').describe())",
        max_length=4000,
    )

    def __init__(self, bot: "RunnerBot", data: Sequence[discord.Attachment]):
        super().__init__()
        self.bot = bot
        self.data = list(data)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.code.value
        code = extract_code_block(raw, any_language=True) or raw  # tolerate pasted ``` fences
        await self.bot.execute(interaction, code=code, data=self.data, label="some code", attach_code=True)


def _names(atts: Sequence[discord.Attachment]) -> str:
    return ", ".join(f"`{a.filename}`" for a in atts)[:300]


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
        @app_commands.describe(
            file="A .py file to run. Leave empty to paste code instead.",
            data1="A data file the code needs (csv, json, txt, another .py to import...)",
            data2="Another data file",
            data3="Another data file",
        )
        async def run(
            interaction: discord.Interaction,
            file: discord.Attachment | None = None,
            data1: discord.Attachment | None = None,
            data2: discord.Attachment | None = None,
            data3: discord.Attachment | None = None,
        ) -> None:
            if not await bot._check_allowed(interaction):
                return
            data = [a for a in (data1, data2, data3) if a is not None]
            if file is not None and not file.filename.lower().endswith(".py"):
                await interaction.response.send_message(
                    "`file` must be a `.py` script. Put data files in `data1`, `data2`, `data3`.", ephemeral=True)
                return
            if problem := bot._size_problem(file, data):
                await interaction.response.send_message(problem, ephemeral=True)
                return
            if file is None:
                await interaction.response.send_modal(CodeModal(bot, data))
                return
            label = f"`{file.filename}`"
            await bot.execute(interaction, code_file=file, data=data, label=label, attach_code=True)

        async def run_message(interaction: discord.Interaction, message: discord.Message) -> None:
            if not await bot._check_allowed(interaction):
                return
            scripts = [a for a in message.attachments if a.filename.lower().endswith(".py")]
            code = extract_code_block(message.content, any_language=True)
            if code is not None:
                # Code block is the script; every attachment (including .py modules) is data.
                code_file, data = None, list(message.attachments)
                label = f"[a code block]({message.jump_url})"
            elif scripts:
                code_file = scripts[0]
                data = [a for a in message.attachments if a is not code_file]
                label = f"[`{code_file.filename}`]({message.jump_url})"
            else:
                await interaction.response.send_message(
                    "No code found: the message needs a ```python block or a `.py` file.", ephemeral=True)
                return
            if code is not None and len(code.encode()) > bot.cfg.max_code_bytes:
                await interaction.response.send_message("That code block is too large.", ephemeral=True)
                return
            if problem := bot._size_problem(code_file, data):
                await interaction.response.send_message(problem, ephemeral=True)
                return
            await bot.execute(interaction, code=code, code_file=code_file, data=data, label=label)

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

    def _size_problem(self, code_file: discord.Attachment | None, data: Sequence[discord.Attachment]) -> str | None:
        if code_file is not None and code_file.size > self.cfg.max_code_bytes:
            return f"`{code_file.filename}` is too large (max {self.cfg.max_code_bytes // 1000} KB)."
        total = sum(a.size for a in data)
        if total > self.cfg.max_data_bytes:
            return f"Data files are too large together ({total / 1e6:.1f} MB, max {self.cfg.max_data_bytes / 1e6:.0f} MB)."
        return None

    async def execute(
        self,
        interaction: discord.Interaction,
        *,
        label: str,
        code: str | None = None,
        code_file: discord.Attachment | None = None,
        data: Sequence[discord.Attachment] = (),
        attach_code: bool = False,
    ) -> None:
        """Download what's needed, run it and post the result publicly as the interaction's response."""
        await interaction.response.defer(thinking=True)
        try:
            if code_file is not None:
                code = (await code_file.read()).decode("utf-8", errors="replace")
            files = [(a.filename, await a.read()) for a in data]
        except discord.HTTPException as exc:
            await interaction.followup.send(f"💥 Couldn't download the attachments: {exc}")
            return
        assert code is not None
        if data:
            label += f" with {_names(data)}"

        digest = hashlib.sha256(code.encode()).hexdigest()[:12]
        log.info("Run by %s (%s) in channel %s: %s sha256=%s",
                 interaction.user, interaction.user.id, interaction.channel_id, label, digest)
        try:
            result = await self.sandbox.run(code, files)
        except Exception:
            log.exception("Sandbox failure")
            await interaction.followup.send("💥 Internal error while running the script (see bot logs).")
            return

        log.info("Run sha256=%s finished: %s", digest, status_line(result)[1])
        content, attachments = format_result(result, prefix=f"{interaction.user.mention} ran {label}\n")
        if attach_code:
            script_name = code_file.filename if code_file is not None else "main.py"
            attachments = [(script_name, code.encode()), *attachments][:10]
        out = [discord.File(io.BytesIO(d), filename=n) for n, d in attachments]
        try:
            await interaction.followup.send(content, files=out)
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
