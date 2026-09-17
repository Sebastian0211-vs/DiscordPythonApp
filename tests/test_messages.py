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
