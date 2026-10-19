import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AIPLATFORM_OPENCODE_LIVE"),
    reason="опциональный live-тест: AIPLATFORM_OPENCODE_LIVE=1 + opencode serve",
)


def test_runtime_health_and_echo():
    from app.services import opencode_runtime as oc

    health = oc.health()
    assert health["url"].startswith("http")

    session_id = oc.create_session({"title": "aiplatform live smoke"})
    assert session_id.startswith("ses_")

    reply = oc.post_message(session_id, "Ответь ровно одним словом: OK")
    assert isinstance(reply, dict)


def test_flag_off_refused():
    from fastapi import HTTPException

    from app.services import opencode_runtime as oc

    ok = False
    old = os.environ.pop("AIPLATFORM_OPENCODE_LIVE", None)
    try:
        oc.create_session({})
    except HTTPException as exc:
        assert exc.detail["error"] == "WAITING_MODEL"
        ok = True
    finally:
        if old:
            os.environ["AIPLATFORM_OPENCODE_LIVE"] = old
    assert ok
