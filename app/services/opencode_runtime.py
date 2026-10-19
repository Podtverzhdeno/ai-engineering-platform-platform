import os

import httpx
from fastapi import HTTPException

OPENCODE_FLAG_ENV = "AIPLATFORM_OPENCODE_LIVE"
OPENCODE_URL_ENV = "AIPLATFORM_OPENCODE_URL"
OPENCODE_TIMEOUT_ENV = "AIPLATFORM_OPENCODE_TIMEOUT"


def is_live_enabled() -> bool:
    return bool(os.environ.get(OPENCODE_FLAG_ENV))


def opencode_url() -> str:
    return os.environ.get(OPENCODE_URL_ENV, "http://127.0.0.1:4096")


def _timeout() -> float:
    try:
        return float(os.environ.get(OPENCODE_TIMEOUT_ENV, "300"))
    except ValueError:
        return 300.0


def _auth() -> tuple[str, str] | None:
    user = os.environ.get("AIPLATFORM_OPENCODE_USER") or os.environ.get("OPENCODE_SERVER_USERNAME") or "opencode"
    password = os.environ.get("AIPLATFORM_OPENCODE_PASSWORD") or os.environ.get("OPENCODE_SERVER_PASSWORD")
    return (user, password) if password else None


class RuntimeRefused(HTTPException):
    def __init__(self, message: str, code: str = "OPENCODE_NOT_CONFIGURED"):
        super().__init__(status_code=503, detail={"error": code, "message": message})


def _request(method: str, path: str, json_body: dict | None, timeout: float | None = None) -> httpx.Response:
    if not is_live_enabled():
        raise RuntimeRefused("set AIPLATFORM_OPENCODE_LIVE=1 and run `opencode serve`; otherwise task stays WAITING_MODEL", "WAITING_MODEL")
    try:
        response = httpx.request(
            method,
            opencode_url() + path,
            json=json_body,
            auth=_auth(),
            timeout=timeout or _timeout(),
        )
    except httpx.HTTPError as exc:
        raise RuntimeRefused(f"opencode server unreachable at {opencode_url()}: {exc}", "OPENCODE_UNAVAILABLE") from exc
    if response.status_code in (401, 403):
        raise RuntimeRefused("opencode server rejected credentials (basic auth)", "OPENCODE_AUTH_REQUIRED")
    response.raise_for_status()
    return response


def health() -> dict:
    response = _request("GET", "/provider/auth", None, timeout=15.0)
    data = response.json()
    providers = {
        pid: (methods[0].get("type") if isinstance(methods, list) and methods else None)
        for pid, methods in (data or {}).items()
    } if isinstance(data, dict) else {}
    return {"url": opencode_url(), "providers": providers}


def create_session(task_contract: dict = None, **_) -> str:
    title = (task_contract or {}).get("title", "AIPLATFORM task")
    response = _request("POST", "/session", {"title": title})
    return response.json()["id"]


def post_message(session_id: str, text: str) -> dict:
    response = _request(
        "POST",
        f"/session/{session_id}/message",
        {"parts": [{"type": "text", "text": text}]},
    )
    return response.json()


def list_sessions() -> list[dict]:
    return _request("GET", "/session", None, timeout=30.0).json()
