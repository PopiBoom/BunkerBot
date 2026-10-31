"""Роутеры обработчиков."""
from .user import router as user_router
from .admin import router as admin_router
from .roast import router as roast_router

__all__ = ["user_router", "admin_router", "roast_router"]
