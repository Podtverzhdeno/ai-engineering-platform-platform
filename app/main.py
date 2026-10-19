import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api import approvals, changes, coding, discovery, evals, memory, planning, projects, pslc, quality, releases
from app.db import Base, SessionLocal, engine
from app.gates import GateDenied
from app.models import User

ADMIN_ID = "usr-admin"
INDEX_PATH = os.path.join(os.path.dirname(__file__), "static", "index.html")


def seed(db) -> None:
    if db.get(User, ADMIN_ID) is None:
        db.add(User(id=ADMIN_ID, name="platform-admin"))
        db.commit()


def create_app() -> FastAPI:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)

    app = FastAPI(title="AI Platform Control Plane", version="0.1.0")

    @app.exception_handler(GateDenied)
    async def gate_denied_handler(request: Request, exc: GateDenied):
        return JSONResponse(
            status_code=403,
            content={
                "error": exc.reason,
                "missing": exc.missing,
                "required_event": exc.required_event,
                "ref": exc.ref,
            },
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/")
    def workspace() -> FileResponse:
        return FileResponse(INDEX_PATH, media_type="text/html")

    app.include_router(projects.router)
    app.include_router(approvals.router)
    app.include_router(coding.router)
    app.include_router(discovery.router)
    app.include_router(changes.router)
    app.include_router(planning.router)
    app.include_router(quality.router)
    app.include_router(releases.router)
    app.include_router(pslc.router)
    app.include_router(evals.router)
    app.include_router(memory.router)
    return app


app = create_app()
