import os


def database_url() -> str:
    return os.environ.get("AIPLATFORM_DB_URL", "sqlite:///./platform.db")


def lease_seconds() -> int:
    return int(os.environ.get("AIPLATFORM_LEASE_SECONDS", "120"))
