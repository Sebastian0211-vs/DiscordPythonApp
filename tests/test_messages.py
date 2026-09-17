from remotepy.messages import DISCORD_LIMIT, extract_code_block, format_result
from remotepy.sandbox import OutputFile, RunResult


def test_extracts_python_block():
    msg = "look:\n```python\nprint('hi')\n```\nthanks"
    assert extract_code_block(msg) == "print('hi')\n"


def test_extracts_py_and_case_insensitive():
    assert extract_code_block("```PY\nx = 1\n```") == "x = 1\n"


def test_ignores_other_languages_and_plain_text():
    assert extract_code_block("```js\nconsole.log(1)\n```") is None
    assert extract_code_block("```\nprint(1)\n```") is None
    assert extract_code_block("just chatting") is None


def test_short_output_inline():
    content, files = format_result(RunResult(0, False, 0.5, "hello\n"))
    assert "exit 0" in content and "hello" in content and files == []


def test_fence_in_output_is_escaped():
    content, _ = format_result(RunResult(0, False, 0.1, "```\n@everyone"))
    assert content.count("```") == 2


def test_long_output_attached_and_tail_shown():
    out = "x" * 5000 + "Traceback: boom"
    content, files = format_result(RunResult(1, False, 1.0, out))
    assert len(content) <= DISCORD_LIMIT
    assert "Traceback: boom" in content
    assert files[0] == ("output.txt", out.encode())


def test_files_and_limits():
    r = RunResult(0, False, 1.0, "", files=[OutputFile("plot.png", b"png")], files_skipped=["big.bin"])
    content, files = format_result(r)
    assert ("plot.png", b"png") in files
    assert "not sent" in content


def test_error_and_timeout():
    assert "out of memory" in format_result(RunResult(137, False, 0, "", error="killed, most likely out of memory"))[0]
    assert "timed out" in format_result(RunResult(None, True, 30, "partial"))[0]


def test_any_language_fallback():
    assert extract_code_block("```\nprint(1)\n```", any_language=True) == "print(1)\n"
    assert extract_code_block("```js\nx\n```\n```py\ny\n```", any_language=True) == "y\n"


def test_prefix_counts_toward_limit():
    prefix = "@seb ran [a code block](https://discord.com/channels/@me/1/2)\n"
    content, _ = format_result(RunResult(0, False, 1.0, "y" * 5000), prefix=prefix)
    assert content.startswith(prefix) and len(content) <= DISCORD_LIMIT
    assert content.count("```") == 2


# ------------------------------------------------------------------ notebooks
from remotepy.messages import BYTES_PER_MESSAGE, EMBED_CHARS_PER_MESSAGE, notebook_messages  # noqa: E402
from remotepy.sandbox import CellOutput, NotebookCell  # noqa: E402


def _nb_result(cells, **kw):
    return RunResult(kw.pop("exit_code", 0), kw.pop("timed_out", False), 1.5, "", cells=cells,
                     notebook=b"{}", **kw)


def test_notebook_one_embed_per_cell_with_image():
    cells = [
        NotebookCell("markdown", "# Title"),
        NotebookCell("code", "print(1)", 1, [CellOutput("text", "1\n")]),
        NotebookCell("code", "plt.show()", 2, [CellOutput("image", name="cell3_1.png", data=b"png")]),
    ]
    msgs = notebook_messages(_nb_result(cells), "@seb ran notebook `a.ipynb`\n", "a.ipynb")
    assert len(msgs) == 1
    m = msgs[0]
    assert "all 2 code cells ran" in m.content
    assert [e.title for e in m.embeds] == ["", "In [1]", "In [2]"]
    assert m.embeds[2].image == "cell3_1.png"
    assert [n for n, _ in m.files] == ["cell3_1.png", "a.executed.ipynb"]


def test_notebook_error_and_not_run_cells():
    cells = [
        NotebookCell("code", "1/0", 1, [CellOutput("text", "ZeroDivisionError: division by zero\n")], error=True),
        NotebookCell("code", "print('never')", None),
    ]
    msgs = notebook_messages(_nb_result(cells, exit_code=1), "", "a.ipynb")
    assert "stopped at In [1]" in msgs[0].content
    assert msgs[0].embeds[0].color != msgs[0].embeds[1].color
    assert msgs[0].embeds[1].title == "In [ ] (not run)"


def test_notebook_splits_messages_within_limits():
    big = "x" * 1400
    cells = [NotebookCell("code", big, i, [CellOutput("text", big),
                                           CellOutput("image", name=f"c{i}.png", data=b"0" * 3_000_000)])
             for i in range(1, 30)]
    msgs = notebook_messages(_nb_result(cells), "", "a.ipynb")
    assert 1 < len(msgs) <= 9
    for m in msgs:
        assert len(m.embeds) <= 10 and len(m.files) <= 10
        assert sum(e.size for e in m.embeds) <= EMBED_CHARS_PER_MESSAGE
        assert sum(len(d) for _, d in m.files) <= BYTES_PER_MESSAGE
        assert all(len(e.description) <= 4096 for e in m.embeds)
    assert "not shown" in msgs[-1].content
