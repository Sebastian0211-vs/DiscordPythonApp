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


NOTEBOOK = {
    "nbformat": 4, "nbformat_minor": 5, "metadata": {},
    "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": "# Hello"},
        {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
         "source": "import pandas as pd\ndf = pd.read_csv('data.csv')\nprint('rows', len(df))\ndf"},
        {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
         "source": "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.show()"},
        {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": "1/0"},
        {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": "print('never')"},
    ],
}


def test_notebook_cell_by_cell():
    import json
    r = asyncio.run(Sandbox(SandboxConfig(notebook_timeout=60)).run(
        json.dumps(NOTEBOOK), [("data.csv", b"a,b\n1,2\n3,4\n")], notebook=True))
    assert r.cells is not None and r.error is None, r
    md, c1, c2, c3, c4 = r.cells
    assert md.type == "markdown"
    text = c1.outputs[0].text
    assert "rows 2" in text and "a  b" in text  # print output and the DataFrame repr
    assert [o.kind for o in c2.outputs] == ["image"] and c2.outputs[0].data[:4] == b"\x89PNG"
    assert c3.error and "ZeroDivisionError" in c3.outputs[0].text
    assert c4.execution_count is None and not c4.outputs  # stopped at the error
    assert r.exit_code == 1 and r.notebook and b"ZeroDivisionError" in r.notebook


def test_notebook_timeout():
    import json
    nb = dict(NOTEBOOK, cells=[{"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                                "source": "import time\nprint('start', flush=True)\ntime.sleep(60)"}])
    r = asyncio.run(Sandbox(SandboxConfig(notebook_timeout=8)).run(json.dumps(nb), notebook=True))
    assert r.timed_out and "start" in r.cells[0].outputs[0].text


def test_invalid_notebook():
    r = asyncio.run(Sandbox(CFG).run("not json", notebook=True))
    assert r.error and "Not a valid notebook" in r.error


def _network_exists(name: str) -> bool:
    return subprocess.run(["docker", "network", "inspect", name], capture_output=True).returncode == 0


@pytest.mark.skipif(not _network_exists("remotepy-net"), reason="run sudo sandbox/network-setup.sh first")
def test_internet_network_blocks_host_and_private_ranges():
    r = run("""
        import socket
        for host, port in [("172.30.254.1", 22), ("10.0.0.1", 80), ("169.254.169.254", 80), ("192.168.1.1", 80)]:
            try:
                socket.create_connection((host, port), timeout=2).close()
                print("REACHED", host)
            except OSError:
                print("blocked", host)
        print("dns", socket.gethostbyname("pypi.org"))
    """, SandboxConfig(timeout=20, network="remotepy-net"))
    assert "REACHED" not in r.output and r.output.count("blocked") == 4, r.output
    assert "dns " in r.output


def test_pil_show_and_display_save_images_in_scripts():
    r = run("""
        from PIL import Image
        from IPython.display import display
        img = Image.new("RGB", (40, 20), "red")
        img.show()
        display(img, "text still prints")
    """, SandboxConfig(timeout=30))
    assert r.ok, r.output
    assert "text still prints" in r.output and "PIL" not in r.output
    assert [f.name for f in r.files] == ["image_1.png", "image_2.png"]
    assert r.files[0].data[:4] == b"\x89PNG"


def test_pil_show_inline_in_notebooks():
    import json
    nb = dict(NOTEBOOK, cells=[{"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                                "source": "from PIL import Image\nImage.new('RGB', (40, 20), 'blue').show()"}])
    r = asyncio.run(Sandbox(SandboxConfig(notebook_timeout=60)).run(json.dumps(nb), notebook=True))
    assert [o.kind for o in r.cells[0].outputs] == ["image"], r.cells
    assert r.files == []


@pytest.mark.skipif(not _network_exists("remotepy-net"), reason="run sudo sandbox/network-setup.sh first")
def test_firewall_check_passes_with_rules():
    assert asyncio.run(Sandbox(SandboxConfig(network="remotepy-net")).check_firewall()) is None


def test_firewall_check_reports_missing_network():
    problem = asyncio.run(Sandbox(SandboxConfig(network="remotepy-missing-net")).check_firewall())
    assert problem and "does not exist" in problem


def test_styled_dataframe_renders_as_png():
    import json
    nb = dict(NOTEBOOK, cells=[{"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                                "source": "import pandas as pd\n"
                                          "pd.DataFrame({'a': [1, 2], 'b': [3, 1]}).corr().style.background_gradient(cmap='coolwarm')"}])
    r = asyncio.run(Sandbox(SandboxConfig(notebook_timeout=60)).run(json.dumps(nb), notebook=True))
    assert [o.kind for o in r.cells[0].outputs] == ["image"], r.cells
    s = run("""
        import pandas as pd
        from IPython.display import display
        display(pd.DataFrame({'a': [1, 2]}).style.highlight_max())
    """, SandboxConfig(timeout=30))
    assert s.ok and [f.name for f in s.files] == ["table_1.png"], s.output


def test_interactive_figures_saved_as_html():
    r = run("""
        import plotly.express as px, altair as alt, pandas as pd
        from bokeh.plotting import figure, show
        px.scatter(x=[1, 2], y=[2, 1]).show()
        alt.Chart(pd.DataFrame({"a": [1, 2]})).mark_point().encode(x="a").show()
        p = figure(); p.line([1, 2], [2, 1]); show(p)
    """, SandboxConfig(timeout=30))
    assert r.ok, r.output
    assert [f.name for f in r.files] == ["altair_1.html", "bokeh_1.html", "plotly_1.html"]
    assert r.output.count("[interactive plot:") == 3
    assert all(len(f.data) < 100_000 for f in r.files)  # libraries come from the CDN


def test_interactive_figures_in_notebook():
    import json
    nb = dict(NOTEBOOK, cells=[
        {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
         "source": "import plotly.express as px\npx.line(x=[1, 2], y=[2, 1])"},
    ])
    r = asyncio.run(Sandbox(SandboxConfig(notebook_timeout=60)).run(json.dumps(nb), notebook=True))
    assert [f.name for f in r.files] == ["plotly_1.html"], r
    assert "interactive plot: plotly_1.html" in r.cells[0].outputs[0].text
