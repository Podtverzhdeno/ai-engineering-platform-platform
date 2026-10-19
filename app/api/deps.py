from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User


def current_user(
    x_user_id: str = Header(alias="X-User-Id"),
    db: Session = Depends(get_db),
) -> User:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="X-User-Id required")
    user = db.get(User, x_user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="unknown user")
    return user
