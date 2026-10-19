from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import database_url

_URL = database_url()


class Base(DeclarativeBase):
    pass


_connect_args = {"check_same_thread": False} if _URL.startswith("sqlite") else {}
engine = create_engine(_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
