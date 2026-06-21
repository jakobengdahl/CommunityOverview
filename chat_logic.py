"""Compatibility shim for tests and legacy imports.

Prefer importing from `backend.chat_logic` in application code.
"""

from backend.chat_logic import *  # noqa: F401,F403
