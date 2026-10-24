import os
from pathlib import Path

from dotenv import load_dotenv

# Local runs read the repo's .env; GitHub Actions passes real environment variables.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing environment variable {name}")
    return value.strip()


SUPABASE_URL = env("SUPABASE_URL")
SUPABASE_SECRET_KEY = env("SUPABASE_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
APP_URL = os.environ.get("APP_URL", "").strip()
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
# The sender verified in Brevo; when unset, the settings row email_from (kept out of this public code).
EMAIL_FROM = os.environ.get("EMAIL_FROM", "").strip()

# eCourts blocks bursts. Keep requests sequential with a gap between them.
REQUEST_DELAY = float(os.environ.get("HAAZIR_REQUEST_DELAY", "2.5"))
# Max case-detail pages fetched per run (each costs one CAPTCHA round trip).
DETAIL_BUDGET = int(os.environ.get("HAAZIR_DETAIL_BUDGET", "120"))
# A case filed within this many days is "new" even if it was on file at the baseline scan.
RECENT_DAYS = int(os.environ.get("HAAZIR_RECENT_DAYS", "30"))
