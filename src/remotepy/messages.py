"""Pure helpers: pull code out of a Discord message and format a run result for Discord."""

import re

from .sandbox import RunResult

DISCORD_LIMIT = 2000
CODE_BLOCK = re.compile(r"```(?:python3?|py)[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_code_block(text: str) -> str | None:
    """Return the first ```python / ```py fenced block in a message, or None."""
    m = CODE_BLOCK.search(text or "")
    return m.group(1) if m else None


def _escape_fence(text: str) -> str:
    # A literal ``` in the output would close our code block early.
    return text.replace("```", "`​``")


def status_line(r: RunResult) -> tuple[str, str]:
    """(reaction emoji, human status)"""
    if r.error:
        return "💥", r.error
    if r.timed_out:
        return "⏱️", f"timed out after {r.duration:.0f}s (partial output below)"
    if r.exit_code == 0:
        return "✅", f"exit 0 · {r.duration:.2f}s"
    return "❌", f"exit {r.exit_code} · {r.duration:.2f}s"


def format_result(r: RunResult) -> tuple[str, list[tuple[str, bytes]]]:
    """Build the reply text and attachments. Long output is attached as output.txt
    and the message shows its tail (tracebacks are at the end)."""
    emoji, status = status_line(r)
    header = f"{emoji} **{status}**"
    attachments: list[tuple[str, bytes]] = []
    notes: list[str] = []

    output = r.output
    if r.output_truncated:
        notes.append("output was cut at the size limit")
    if r.files_skipped:
        notes.append((f"{len(r.files_skipped)} file(s) not sent (limit): " + ", ".join(r.files_skipped[:5]))[:300])

    if not output.strip():
        body = "" if r.error else "\n*(no output)*"
    else:
        shown = _escape_fence(output.rstrip("\n"))
        # Reserve room for header, fences and a footer of notes.
        room = DISCORD_LIMIT - len(header) - 60 - sum(len(n) + 3 for n in notes)
        if len(shown) > room:
            attachments.append(("output.txt", output.encode()))
            shown = "…" + shown[-(room - 1):]
            notes.insert(0, "full output in output.txt")
        body = f"\n```\n{shown}\n```"

    footer = ("\n-# " + " · ".join(notes)) if notes else ""
    attachments += [(f.name, f.data) for f in r.files]
    return (header + body + footer)[:DISCORD_LIMIT], attachments[:10]
