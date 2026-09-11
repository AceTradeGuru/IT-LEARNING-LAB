"""
Royal Grind — configuration.

Everything that differs between a laptop and production comes from the
environment. Nothing secret is committed, and the app refuses to start in
production without the values it needs rather than falling back to a
development default — a hardcoded fallback secret key is indistinguishable
from no secret key at all.

The telephony and compliance settings are here rather than in the database
because they are deployment facts, not tenant preferences. Which phone
platform is wired up is the operator's decision; whether a given tenant
records calls is theirs, and lives on `orgs`.
"""

import os
import secrets


def _bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    ENV = os.environ.get("GRIND_ENV", "development")
    IS_PROD = ENV == "production"

    DEBUG = _bool("GRIND_DEBUG", default=not IS_PROD)

    # In production the key must be supplied. In development we mint an ephemeral
    # one, which logs everyone out on restart — annoying, and much safer than a
    # shared constant that ends up in git.
    SECRET_KEY = os.environ.get("SECRET_KEY") or (None if IS_PROD else secrets.token_hex(32))

    # SQLite when unset; a Postgres URL switches engines. See db.py.
    DATABASE_URL = os.environ.get("DATABASE_URL", "")

    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", default=IS_PROD)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = int(os.environ.get("SESSION_HOURS", "12")) * 3600

    # 8 MB covers a large prospect-list CSV. Transcripts posted by the telephony
    # webhook are the other large body this app accepts.
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_KB", "8192")) * 1024

    # ------------------------------------------------------------ AI budget --
    # Per-org, per-hour ceiling on model-backed endpoints. The cost of an AI
    # feature is unbounded without one, because a loop costs the customer nothing.
    AI_CALLS_PER_HOUR = int(os.environ.get("AI_CALLS_PER_HOUR", "120"))
    # How many rows the plain-English search will put in front of the model.
    AI_SEARCH_MAX_COMPANIES = int(os.environ.get("AI_SEARCH_MAX_COMPANIES", "80"))

    # ------------------------------------------------------------ telephony --
    # 'simulated' runs the built-in call simulator — no account, no minutes, no
    # cost, and the demo works on a plane. 'vonage' uses the Voice API.
    TELEPHONY_PROVIDER = os.environ.get("TELEPHONY_PROVIDER", "simulated")
    VONAGE_APPLICATION_ID = os.environ.get("VONAGE_APPLICATION_ID", "")
    VONAGE_PRIVATE_KEY = os.environ.get("VONAGE_PRIVATE_KEY", "")
    VONAGE_NUMBER = os.environ.get("VONAGE_NUMBER", "")
    # Public base URL the phone platform calls back on. Without it, call events
    # never arrive and every call stays 'ringing' forever.
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")
    # Shared secret on the webhook. An unauthenticated webhook that writes call
    # records is an unauthenticated write endpoint.
    TELEPHONY_WEBHOOK_SECRET = os.environ.get("TELEPHONY_WEBHOOK_SECRET", "")

    # ----------------------------------------------------------- enrichment --
    # 'offline' generates deterministic pseudo-verification for the demo and
    # labels every field as such. A real provider key switches it.
    ENRICHMENT_PROVIDER = os.environ.get("ENRICHMENT_PROVIDER", "offline")
    ENRICHMENT_PROVIDER_URL = os.environ.get("ENRICHMENT_PROVIDER_URL", "")
    ENRICHMENT_API_KEY = os.environ.get("ENRICHMENT_API_KEY", "")
    # A verified number older than this is no longer verified. People change jobs.
    VERIFICATION_TTL_DAYS = int(os.environ.get("VERIFICATION_TTL_DAYS", "90"))

    # Retention clock on prospect personal data, in days, unless the org sets it.
    RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "730"))


def validate(config):
    """
    Fail fast on a misconfigured production boot.

    Returns a list rather than raising, so the caller prints every problem at
    once instead of one per restart.
    """
    problems = []
    if config.IS_PROD:
        if not config.SECRET_KEY:
            problems.append("SECRET_KEY is required when GRIND_ENV=production.")
        if config.DEBUG:
            problems.append("GRIND_DEBUG must be off in production — the Werkzeug "
                            "debugger is remote code execution.")
        if not config.SESSION_COOKIE_SECURE:
            problems.append("SESSION_COOKIE_SECURE must be on in production (serve over HTTPS).")
        if config.TELEPHONY_PROVIDER == "vonage":
            if not (config.VONAGE_APPLICATION_ID and config.VONAGE_PRIVATE_KEY):
                problems.append("TELEPHONY_PROVIDER=vonage needs VONAGE_APPLICATION_ID "
                                "and VONAGE_PRIVATE_KEY.")
            if not config.PUBLIC_BASE_URL:
                problems.append("TELEPHONY_PROVIDER=vonage needs PUBLIC_BASE_URL so call "
                                "events have somewhere to land.")
            if not config.TELEPHONY_WEBHOOK_SECRET:
                problems.append("TELEPHONY_WEBHOOK_SECRET is required — the call webhook "
                                "writes to the database and must not be open.")
    return problems
