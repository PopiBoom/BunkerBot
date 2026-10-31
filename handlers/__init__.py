"""Роутеры обработчиков."""
from .user import router as user_router
from .admin import router as admin_router
from .help import router as help_router
from .quotes import router as quotes_router
from .stats import router as stats_router
from .roast import router as roast_router

__all__ = ["user_router", "admin_router", "help_router", "quotes_router", "stats_router", "roast_router"]
