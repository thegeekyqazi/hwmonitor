# settings.py
"""
Persistent user settings stored in a local JSON file.
Used for LLM API keys (kept on the user's machine, never sent anywhere
except to the chosen LLM provider).
"""
import json
import os
from pathlib import Path
from typing import Any, Dict


_SETTINGS_FILE = Path.home() / ".processlens" / "settings.json"


def _ensure_dir():
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_settings() -> Dict[str, Any]:
    if not _SETTINGS_FILE.exists():
        return {}
    try:
        with open(_SETTINGS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(settings: Dict[str, Any]):
    _ensure_dir()
    with open(_SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)


def get_setting(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)


def set_setting(key: str, value: Any):
    settings = load_settings()
    settings[key] = value
    save_settings(settings)


def settings_status() -> Dict[str, bool]:
    """Returns which providers are configured (without exposing the actual keys)."""
    s = load_settings()
    return {
        "claude_configured": bool(s.get("anthropic_api_key")),
        "openai_configured": bool(s.get("openai_api_key")),
        "gemini_configured": bool(s.get("gemini_api_key")),
        "preferred_provider": s.get("preferred_provider", "claude"),
    }