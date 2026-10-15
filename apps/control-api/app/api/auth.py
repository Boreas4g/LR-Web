from fastapi import APIRouter, Depends, HTTPException, status

from ..config import get_settings
from ..dependencies import get_current_user, synthetic_operator
from ..schemas import AuthResponse, CurrentUserResponse, LoginRequest, RegisterRequest, User
from ..store import store

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=AuthResponse)
def login(request: LoginRequest) -> AuthResponse:
    result = store.authenticate(request.username, request.password)
    if not result:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    token, user = result
    return AuthResponse(token=token, user=user)


@router.post("/register", response_model=User, status_code=status.HTTP_201_CREATED)
def register(request: RegisterRequest) -> User:
    if not get_settings().auth_enabled:
        return synthetic_operator()
    try:
        return store.register(request.username, request.password, request.name)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/me", response_model=CurrentUserResponse)
def me(user: User = Depends(get_current_user)) -> CurrentUserResponse:
    return CurrentUserResponse(user=user)
