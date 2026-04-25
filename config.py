import os
import json
import logging
import re
from pathlib import Path
from typing import Any

# Base Directory
BASE_DIR = Path(__file__).resolve().parent

# Logging setup (shared)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("watchlist-monitor")

def _load_env():
    """Load variables from .env file if it exists."""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

_load_env()

# API Keys and Secrets (Cleaned)
GEMINI_API_KEY = re.sub(r'[^A-Za-z0-9_\-\.]', '', os.environ.get("GEMINI_API_KEY", ""))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def load_models_config():
    """Load model names from models.json; fallback to empty defaults."""
    defaults = {
        "GEMINI_MODEL_PRIORITY": [],
        "GEMINI_MODEL_FALLBACKS": [],
        "GEMINI_GEMMA_FALLBACK": [],
    }
    
    models_file = BASE_DIR / "models.json"
    if models_file.exists():
        try:
            config = json.loads(models_file.read_text(encoding="utf-8"))
            # If a key is missing, we use an empty list.
            for key, val in defaults.items():
                if key not in config or not isinstance(config[key], (list, tuple)):
                    config[key] = val
            return config
        except (json.JSONDecodeError, OSError):
            log.error("CRITICAL: models.json is corrupt or unreadable!")
            
    return defaults


# Load the models
MODELS = load_models_config()
GEMINI_MODEL_PRIORITY = tuple(MODELS["GEMINI_MODEL_PRIORITY"])
GEMINI_MODEL_FALLBACKS = tuple(MODELS["GEMINI_MODEL_FALLBACKS"])
GEMINI_GEMMA_FALLBACK = tuple(MODELS["GEMINI_GEMMA_FALLBACK"])
