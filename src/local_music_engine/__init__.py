"""Local Music Engine public package."""

from .storage import SCHEMA_VERSION, ProjectStore

__all__ = ["ProjectStore", "SCHEMA_VERSION"]
__version__ = "0.1.0"
