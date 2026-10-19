import io
import os
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import redirect_stdout

import httpx
import pytest


@pytest.fixture()
def live_server():
    db = tempfile.mkdtemp(prefix="aip-cli-").replace("\\", "/")
    env = {**os.environ, "AIPLATFORM_DB_URL": "sqlite:///" + db + "/cli.db"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "7988", "--log-level", "critical"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=open(os.path.join(tempfile.gettempdir(), "aip-uvicorn-err.log"), "a", encoding="utf-8"),
    )
    url = "http://127.0.0.1:7988"
    for _ in range(80):
        try:
            httpx.get(url + "/healthz", timeout=1.0)
            break
        except Exception:
            time.sleep(0.25)
    os.environ["AIPLATFORM_URL"] = url
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_cli_user(live_server):
    from app.cli import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["user", "cli-" + uuid.uuid4().hex[:6]])
    assert buf.getvalue().strip().startswith("usr-")


def test_cli_demo_full_pipeline(live_server):
    from app.cli import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["demo"])
    out = buf.getvalue()
    assert "DEMO COMPLETE" in out
    assert "readiness: GO" in out
    assert "merge: MERGED" in out
    assert "production deploy: REFUSED" in out
    assert "chain_ok=True" in out
    assert "recall_planted': 1.0" in out
