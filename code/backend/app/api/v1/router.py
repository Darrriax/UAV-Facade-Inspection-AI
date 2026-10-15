"""Aggregate router mounted once in the application factory."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, projects, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(projects.router)

__all__ = ["api_router"]
