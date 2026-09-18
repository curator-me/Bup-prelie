"""Centralised runtime configuration.

Loads a ``.env`` file from the project root (if present) before anything reads the
environment, then exposes the LLM settings as plain module constants. Real environment
variables always win over ``.env`` values so containers and CI can override them.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

load_dotenv(ENV_FILE, override=False)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


# --- LLM provider ------------------------------------------------------------
LLM_API_KEY: str = _env("ANTHROPIC_API_KEY")
LLM_BASE_URL: str = _env("ANTHROPIC_BASE_URL")
DEFAULT_MODEL_ID = "deepseek-v4-pro"
LLM_MODEL_ID: str = _env("LLM_MODEL_ID", DEFAULT_MODEL_ID)


def _env_float(name: str, default: float) -> float:
    try:
        value = float(_env(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


LLM_TIMEOUT_SECONDS: float = _env_float("LLM_TIMEOUT_SECONDS", 8.0)

__all__ = [
    "ENV_FILE",
    "PROJECT_ROOT",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL_ID",
    "LLM_TIMEOUT_SECONDS",
    "DEFAULT_MODEL_ID",
]
