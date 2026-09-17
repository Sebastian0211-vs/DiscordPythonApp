"""Integration tests: need Docker and the sandbox image.

    docker build -t remotepy-sandbox:latest sandbox/
    uv run pytest
"""

import asyncio
import shutil
import subprocess
import textwrap

import pytest

from remotepy.config import SandboxConfig
from remotepy.sandbox import Sandbox


def _image_available() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", SandboxConfig.image], capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _image_available(), reason="docker or sandbox image not available")

CFG = SandboxConfig(timeout=5, memory="256m", pids_limit=64)


def run(code: str, cfg: SandboxConfig = CFG):
    return asyncio.run(Sandbox(cfg).run(textwrap.dedent(code)))


def test_hello_and_stderr():
    r = run("import sys\nprint('hi')\nprint('err', file=sys.stderr)")
    assert r.ok and "hi" in r.output and "err" in r.output


def test_exception_gives_traceback():
    r = run("1/0")
    assert r.exit_code == 1 and "ZeroDivisionError" in r.output


def test_libs_and_plot_file():
    r = run("""
        import numpy, pandas, scipy, sympy, sklearn
        import matplotlib.pyplot as plt
        plt.plot([1, 2, 3]); plt.savefig("plot.png")
        print("ok")
    """, SandboxConfig(timeout=30))  # first import after a (re)build is slow: cold disk cache
    assert r.ok, r.output
    assert [f.name for f in r.files] == ["plot.png"] and r.files[0].data[:4] == b"\x89PNG"


def test_no_network():
    r = run("""
        import socket
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=2)
            print("CONNECTED")
        except OSError as e:
            print("blocked", e)
    """)
    assert "blocked" in r.output and "CONNECTED" not in r.output


def test_not_root_and_readonly_fs():
    r = run("""
        import os
        print("uid", os.getuid())
        for p in ("/etc/evil", "/opt/runner.py", "/usr/lib/x"):
            try:
                open(p, "w"); print("WROTE", p)
            except OSError:
                print("ro", p)
    """)
    assert "uid 10001" in r.output and "WROTE" not in r.output


def test_timeout_keeps_partial_output():
    r = run("import time\nprint('start', flush=True)\nwhile True: time.sleep(0.1)")
    assert r.timed_out and "start" in r.output


def test_memory_limit():
    r = run("x = bytearray(1024 * 1024 * 1024)\nprint('ALLOCATED')")
    assert "ALLOCATED" not in r.output
    assert not r.ok


def test_fork_bomb_contained():
    r = run("""
        import os, time
        n = 0
        try:
            while True:
                if os.fork() == 0:
                    time.sleep(30)
                    os._exit(0)
                n += 1
        except OSError:
            print("fork limited after", n)
    """)
    assert "fork limited" in r.output


def test_huge_output_truncated():
    r = run("import sys\nwhile True: sys.stdout.write('A' * 65536)", SandboxConfig(timeout=2))
    assert r.output_truncated and len(r.output) <= SandboxConfig.max_output_bytes


def test_container_removed():
    run("print(1)")
    out = subprocess.run(["docker", "ps", "-aq", "--filter", "label=remotepy=sandbox"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


def test_concurrent_runs():
    async def many():
        sb = Sandbox(CFG)
        return await asyncio.gather(*(sb.run(f"print({i})") for i in range(4)))
    results = asyncio.run(many())
    assert [r.output.strip() for r in results] == ["0", "1", "2", "3"]


def test_data_files_readable_and_not_echoed():
    csv = b"x,y\n1,2\n3,4\n"
    r = asyncio.run(Sandbox(CFG).run(
        "import pandas as pd\nprint(pd.read_csv('data.csv')['y'].sum())\nopen('result.txt', 'w').write('done')",
        [("data.csv", csv)],
    ))
    assert r.ok, r.output
    assert r.output.strip() == "6"
    assert [f.name for f in r.files] == ["result.txt"]  # the untouched input isn't sent back


def test_modified_data_file_is_returned():
    r = asyncio.run(Sandbox(CFG).run("open('data.csv', 'a').write('5,6\\n')", [("data.csv", b"x,y\n")]))
    assert [(f.name, f.data) for f in r.files] == [("data.csv", b"x,y\n5,6\n")]


def test_uploaded_module_importable():
    r = asyncio.run(Sandbox(CFG).run("import helper\nprint(helper.twice(21))", [("helper.py", b"def twice(n):\n    return 2 * n\n")]))
    assert r.ok and r.output.strip() == "42", r.output


def test_data_file_names_cannot_escape_workdir():
    r = asyncio.run(Sandbox(CFG).run(
        "import os\nprint(sorted(os.listdir('.')))",
        [("../../opt/evil.py", b"x"), (".hidden", b"x"), ("ok.txt", b"x")],
    ))
    assert r.output.strip() == "['evil.py', 'ok.txt']"


def test_plt_show_saves_figures_and_ipython_available():
    r = run("""
        from IPython.display import display
        import matplotlib.pyplot as plt
        plt.plot([1, 2]); plt.show()
        plt.plot([3, 4]); plt.show()
    """, SandboxConfig(timeout=30))
    assert r.ok, r.output
    assert [f.name for f in r.files] == ["figure_1.png", "figure_2.png"]
