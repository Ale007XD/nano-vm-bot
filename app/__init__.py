"""
App module initialization
"""
from app.core.config import settings
from app.core.database import get_db, init_db
from app.core.llm_client_v2 import llm_client
from app.core.memory import vector_memory
from app.core.skills_loader import skill_loader

__version__ = settings.APP_VERSION

__all__ = [
    "settings",
    "init_db",
    "get_db",
    "vector_memory",
    "llm_client",
    "skill_loader",
    "__version__",
]
