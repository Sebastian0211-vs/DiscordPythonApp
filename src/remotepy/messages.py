"""Pure helpers: pull code out of a Discord message and format a run result for Discord."""

import re
from dataclasses import dataclass, field

from .sandbox import RunResult

DISCORD_LIMIT = 2000
PY_BLOCK = re.compile(r"```(?:python3?|py)[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
ANY_BLOCK = re.compile(r"```[\w+-]*[ \t]*\r?\n(.*?)```", re.DOTALL)


def extract_code_block(text: str, any_language: bool = False) -> str | None:
    """Return the first ```python / ```py fenced block, or None.
    With any_language, fall back to the first fenced block of any (or no) language."""
    m = PY_BLOCK.search(text or "")
    if not m and any_language:
        m = ANY_BLOCK.search(text or "")
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


def format_result(r: RunResult, prefix: str = "") -> tuple[str, list[tuple[str, bytes]]]:
    """Build the reply text and attachments. Long output is attached as output.txt
    and the message shows its tail (tracebacks are at the end)."""
    emoji, status = status_line(r)
    header = f"{prefix}{emoji} **{status}**"
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


# ------------------------------------------------------------------ notebooks
BLURPLE, RED, GREY, DARK = 0x5865F2, 0xED4245, 0x80848E, 0x2B2D31
EMBEDS_PER_MESSAGE = 10
EMBED_CHARS_PER_MESSAGE = 5800  # Discord allows 6000
FILES_PER_MESSAGE = 10
BYTES_PER_MESSAGE = 9 * 1024 * 1024
MAX_MESSAGES = 8


@dataclass
class EmbedSpec:
    description: str = ""
    title: str = ""
    color: int = BLURPLE
    image: str | None = None  # attachment file name shown in the embed

    @property
    def size(self) -> int:
        return len(self.title) + len(self.description)


@dataclass
class MessageSpec:
    content: str = ""
    embeds: list[EmbedSpec] = field(default_factory=list)
    files: list[tuple[str, bytes]] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (label, url) link buttons


def _clip(text: str, limit: int) -> str:
    text = text.strip("\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tail_clip(text: str, limit: int) -> str:
    text = text.strip("\n")
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def notebook_status(r: RunResult) -> tuple[str, str]:
    cells = [c for c in (r.cells or []) if c.type == "code" and c.source.strip()]
    if r.error:
        return "💥", r.error
    if r.timed_out:
        return "⏱️", f"timed out after {r.duration:.0f}s"
    failed = next((c for c in cells if c.error), None)
    if failed is not None:
        return "❌", f"stopped at In [{failed.execution_count}] with an error · {r.duration:.2f}s"
    return "✅", f"all {len(cells)} code cells ran · {r.duration:.2f}s"


def _cell_embeds(r: RunResult) -> list[tuple[EmbedSpec, tuple[str, bytes] | None]]:
    items: list[tuple[EmbedSpec, tuple[str, bytes] | None]] = []
    for cell in r.cells or []:
        if not cell.source.strip():
            continue
        if cell.type == "markdown":
            items.append((EmbedSpec(description=_clip(cell.source, 1500), color=DARK), None))
            continue
        ran = cell.execution_count is not None
        color = RED if cell.error else (BLURPLE if ran else GREY)
        title = f"In [{cell.execution_count}]" if ran else "In [ ] (not run)"
        src = _clip(_escape_fence(cell.source), 1000 if ran else 300)
        desc = f"```py\n{src}\n```"
        text = "".join(o.text for o in cell.outputs if o.kind == "text")
        if text.strip():
            desc += f"\n```\n{_tail_clip(_escape_fence(text), 1500)}\n```"
        images = [o for o in cell.outputs if o.kind == "image"]
        first = images[0] if images else None
        items.append((EmbedSpec(title=title, description=desc, color=color,
                                image=first.name if first else None),
                      (first.name, first.data) if first else None))
        for img in images[1:]:
            items.append((EmbedSpec(color=color, image=img.name), (img.name, img.data)))
    return items


def notebook_messages(r: RunResult, prefix: str, notebook_name: str,
                      links: list[tuple[str, str]] | None = None) -> list[MessageSpec]:
    """Split a notebook run into Discord messages: one embed per cell (plots inside the
    embed), grouped within Discord's per-message limits. The executed notebook and any
    files the notebook wrote go in the last message."""
    emoji, status = notebook_status(r)
    messages = [MessageSpec(content=_clip(f"{prefix}{emoji} **{status}**", DISCORD_LIMIT))]
    items = _cell_embeds(r)
    shown = 0
    for spec, file in items:
        cur = messages[-1]
        size = len(file[1]) if file else 0
        full = (
            len(cur.embeds) >= EMBEDS_PER_MESSAGE
            or sum(e.size for e in cur.embeds) + spec.size > EMBED_CHARS_PER_MESSAGE
            or (file and len(cur.files) >= FILES_PER_MESSAGE)
            or sum(len(d) for _, d in cur.files) + size > BYTES_PER_MESSAGE
        )
        if full:
            if len(messages) >= MAX_MESSAGES:
                break
            cur = MessageSpec()
            messages.append(cur)
        cur.embeds.append(spec)
        if file:
            cur.files.append(file)
        shown += 1

    notes = []
    if shown < len(items):
        notes.append(f"{len(items) - shown} more cell(s) not shown here, see the executed notebook")
    if r.files_skipped:
        notes.append(f"{len(r.files_skipped)} file(s) not sent (limit)")
    extra: list[tuple[str, bytes]] = []
    if r.notebook:
        stem = notebook_name[:-6] if notebook_name.lower().endswith(".ipynb") else notebook_name
        extra.append((f"{stem}.executed.ipynb", r.notebook))
    extra += [(f.name, f.data) for f in r.files]

    last = messages[-1]
    if extra and (len(last.files) + len(extra) > FILES_PER_MESSAGE
                  or sum(len(d) for _, d in last.files + extra) > BYTES_PER_MESSAGE):
        last = MessageSpec()
        messages.append(last)
    last.files += extra[: FILES_PER_MESSAGE - len(last.files)]
    last.links = list(links or [])[:25]
    if notes:
        note = "-# " + " · ".join(notes)
        last.content = f"{last.content}\n{note}".strip() if last.content else note
    return messages
