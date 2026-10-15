from fastapi import Header, HTTPException, status

from .config import get_settings
from .schemas import User
from .store import store


def synthetic_operator() -> User:
    """Built-in identity used for every request while auth is disabled."""
    return User(username="operator", name="操作员", role="系统管理员")


def get_current_user(authorization: str | None = Header(default=None)) -> User:
    if not get_settings().auth_enabled:
        return synthetic_operator()
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    user = store.user_for_token(token)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")
    return user
