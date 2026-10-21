from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.auth.security import create_access_token, verify_password
from app.config import Settings, get_settings
from app.db.models import User

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    role: Literal["viewer", "approver", "admin"]


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    async with request.app.state.session_factory() as session:
        user = await session.scalar(
            select(User).where(
                User.username == payload.username,
                User.is_active.is_(True),
            )
        )
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        token = create_access_token(user, settings)
        return LoginResponse(
            access_token=token,
            expires_in=settings.access_token_expiry_minutes * 60,
            role=user.role,
        )
