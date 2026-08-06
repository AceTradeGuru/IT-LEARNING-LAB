"""
CarrierGuard — configuration.

Twelve-factor: everything that differs between a laptop and production comes
from the environment, and nothing secret is ever committed. The one rule worth
stating explicitly is at the bottom — in production, a missing SECRET_KEY is a
hard startup failure, not a silently generated default. A regenerated session
key logs every user out; a hardcoded fallback lets anyone forge a session.
"""

import os
import secrets


def _bool(name, default=False):
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    # 'development' | 'production'. Controls fail-fast behavior below.
    ENV = os.environ.get("APP_ENV", "development").strip().lower()
    IS_PRODUCTION = ENV == "production"

    # Local default matches docker-compose.yml. Managed Postgres overrides it.
    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql://carrierguard:localdev@localhost:5433/carrierguard",
    )
    DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "1"))
    DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))

    SECRET_KEY = os.environ.get("SECRET_KEY", "")

    # Cookie hardening. Secure cookies require HTTPS, so it tracks production.
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", IS_PRODUCTION)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Reject oversized request bodies before they reach a handler.
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", str(256 * 1024)))

    # External data sources. Absent in development; the ingest job falls back
    # to seeded data and says so rather than pretending it refreshed.
    FMCSA_WEBKEY = os.environ.get("FMCSA_WEBKEY", "")
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")

    # A carrier record older than this is stale enough to warn a broker about.
    CARRIER_STALE_DAYS = int(os.environ.get("CARRIER_STALE_DAYS", "3"))


def load():
    """Return the validated config, or die loudly."""
    cfg = Config

    if cfg.IS_PRODUCTION:
        missing = [n for n in ("SECRET_KEY", "DATABASE_URL") if not getattr(cfg, n)]
        if missing:
            raise RuntimeError(
                f"Refusing to start in production without: {', '.join(missing)}. "
                f"Set them in the environment."
            )
    elif not cfg.SECRET_KEY:
        # Development only. Ephemeral by design: sessions die with the process,
        # which is correct locally and would be a bug in production.
        cfg.SECRET_KEY = secrets.token_hex(32)

    return cfg
