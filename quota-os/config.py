"""
QuotaOS — configuration.

Everything that differs between a laptop and production comes from the environment.
Nothing secret is committed, and the app refuses to start in production without the
values it needs rather than silently falling back to a development default — a
hardcoded fallback secret key is indistinguishable from no secret key at all.
"""

import os
import secrets


def _bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    ENV = os.environ.get("QUOTAOS_ENV", "development")
    IS_PROD = ENV == "production"

    DEBUG = _bool("QUOTAOS_DEBUG", default=not IS_PROD)

    # In production the key must be supplied. In development we mint an ephemeral
    # one, which logs everyone out on restart — annoying, and much safer than a
    # shared constant that ends up in git.
    SECRET_KEY = os.environ.get("SECRET_KEY") or (None if IS_PROD else secrets.token_hex(32))

    # sqlite path today; a DATABASE_URL pointing at Postgres is the next migration.
    DATABASE_URL = os.environ.get("DATABASE_URL", "")

    # Session cookie hardening. Secure requires HTTPS, so it tracks the environment.
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", default=IS_PROD)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = int(os.environ.get("SESSION_HOURS", "12")) * 3600

    # Requests larger than this are rejected before they reach a view. Resume text
    # is the only large field the app accepts.
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_KB", "512")) * 1024

    # Per-org, per-hour ceilings on the model-backed endpoints. The cost of an AI
    # feature is unbounded without one, because a loop costs the customer nothing.
    AI_CALLS_PER_HOUR = int(os.environ.get("AI_CALLS_PER_HOUR", "60"))
    AI_SEARCH_MAX_CANDIDATES = int(os.environ.get("AI_SEARCH_MAX_CANDIDATES", "60"))

    # Days a candidate record is retained after its last activity, unless the org
    # sets otherwise. Personal data needs a clock on it.
    CANDIDATE_RETENTION_DAYS = int(os.environ.get("CANDIDATE_RETENTION_DAYS", "365"))


def validate(config):
    """
    Fail fast on a misconfigured production boot.

    Returning a list rather than raising lets the caller print every problem at
    once instead of one per restart.
    """
    problems = []
    if config.IS_PROD:
        if not config.SECRET_KEY:
            problems.append("SECRET_KEY is required when QUOTAOS_ENV=production.")
        if config.DEBUG:
            problems.append("QUOTAOS_DEBUG must be off in production — the Werkzeug "
                            "debugger is remote code execution.")
        if not config.SESSION_COOKIE_SECURE:
            problems.append("SESSION_COOKIE_SECURE must be on in production (serve over HTTPS).")
    return problems
