"""Authentication request/response contracts.

Account creation is not part of this module: it is an Admin action, so it lives
in ``app.schemas.user`` (``UserCreate``) behind ``POST /users``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")
