"""All settings come from environment variables (.env in the project root)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


LLM_BASE_URL = env("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = env("LLM_API_KEY")
LLM_MODEL = env("LLM_MODEL", "gpt-4o-mini")
# Comma-separated models tried in order when the main one is rate-limited (same provider).
LLM_FALLBACK_MODELS = [m.strip() for m in env("LLM_FALLBACK_MODELS").split(",") if m.strip()]

AGORA_APP_ID = env("AGORA_APP_ID")
AGORA_APP_CERTIFICATE = env("AGORA_APP_CERTIFICATE")
AGORA_CUSTOMER_ID = env("AGORA_CUSTOMER_ID")
AGORA_CUSTOMER_SECRET = env("AGORA_CUSTOMER_SECRET")
AGORA_CHANNEL = env("AGORA_CHANNEL", "fieldpilot-demo")
AGORA_ASR_LANGUAGE = env("AGORA_ASR_LANGUAGE", "en-US")

def _public_url(raw: str) -> str:
    url = raw.strip().strip("'\"").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


PUBLIC_BASE_URL = _public_url(env("PUBLIC_BASE_URL"))
BRAIN_API_KEY = env("BRAIN_API_KEY", "change-me")

MURF_API_KEY = env("MURF_API_KEY")
MURF_VOICE_ID = env("MURF_VOICE_ID", "Matthew")
MURF_LOCALE = env("MURF_LOCALE", "en-US")
MURF_MODEL = env("MURF_MODEL", "FALCON")
AGORA_TTS_PARAMS_JSON = env("AGORA_TTS_PARAMS_JSON")

SLACK_WEBHOOK_URL = env("SLACK_WEBHOOK_URL")

DB_PATH = Path(env("FIELDPILOT_DB", str(ROOT / "data" / "fieldpilot.db")))
MANUALS_DIR = ROOT / "backend" / "manuals"
FRONTEND_DIR = ROOT / "frontend"

# The demo technician who is "logged in" on the device.
DEFAULT_TECHNICIAN_ID = env("DEFAULT_TECHNICIAN_ID", "T-07")
