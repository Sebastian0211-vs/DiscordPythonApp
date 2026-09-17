import os
import time

from remotepy.pages import PagePublisher


def test_publish_and_cleanup(tmp_path):
    pub = PagePublisher(str(tmp_path), "https://plots.example.com/", ttl_days=1)
    links = pub.publish([("plotly_1.html", b"<html>1</html>"), ("../../evil name.html", b"x")])
    assert [n for n, _ in links] == ["plotly_1.html", "evil name.html"]
    token = links[0][1].split("/")[3]
    assert len(token) >= 20 and links[1][1].endswith("/evil%20name.html")
    assert (tmp_path / token / "plotly_1.html").read_bytes() == b"<html>1</html>"
    assert oct((tmp_path / token).stat().st_mode)[-3:] == "755"
    assert oct((tmp_path / token / "plotly_1.html").stat().st_mode)[-3:] == "644"

    assert pub.cleanup() == 0  # fresh
    old = time.time() - 2 * 86400
    os.utime(tmp_path / token, (old, old))
    assert pub.cleanup() == 1 and not (tmp_path / token).exists()


def test_publish_nothing(tmp_path):
    assert PagePublisher(str(tmp_path), "https://x", 1).publish([]) == []
