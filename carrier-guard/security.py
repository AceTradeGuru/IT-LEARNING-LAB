"""
CarrierGuard — CSRF, rate limiting, audit trail, and response headers.

Written for a public signup form, which means assuming every request is
hostile until proven otherwise.
"""

import hmac
import json
import logging
import secrets

from flask import g, request, session

log = logging.getLogger("carrierguard.security")

CSRF_FIELD = "_csrf"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------- CSRF

def csrf_token():
    """Per-session token, minted lazily and reused for the session's lifetime."""
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_valid():
    """
    True if the request carries the session's token.

    The API is exempt and does not need this: CSRF works because browsers
    attach cookies automatically, and they never attach an Authorization
    header automatically. A bearer-token endpoint is not forgeable this way.
    """
    expected = session.get("csrf", "")
    if not expected:
        return False
    presented = request.form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER, "")
    return hmac.compare_digest(str(presented), str(expected))


# ---------------------------------------------------------------- client IP

def client_ip():
    """
    The caller's address, honoring X-Forwarded-For only when a trusted proxy
    count is configured.

    Never trust the header blindly. Anyone can send `X-Forwarded-For: 1.2.3.4`
    directly, and a rate limiter keyed on a spoofable value is decoration —
    an attacker just rotates the header and every bucket stays empty.
    ProxyFix (wired in app.py) strips exactly the configured number of hops.
    """
    return request.remote_addr or "unknown"


# ---------------------------------------------------------------- rate limits

def hit(conn, bucket, limit, window_seconds):
    """
    Count one event against `bucket`. Returns (allowed, count, retry_after).

    One atomic upsert: read-then-write would let concurrent requests both see
    a count under the limit and both proceed, which is exactly the race a
    credential-stuffing script produces on purpose.
    """
    row = conn.execute("""
        INSERT INTO rate_limits (bucket, window_start, count)
             VALUES (%(bucket)s, now(), 1)
        ON CONFLICT (bucket) DO UPDATE SET
            count = CASE
                WHEN rate_limits.window_start
                     < now() - make_interval(secs => %(window)s) THEN 1
                ELSE rate_limits.count + 1 END,
            window_start = CASE
                WHEN rate_limits.window_start
                     < now() - make_interval(secs => %(window)s) THEN now()
                ELSE rate_limits.window_start END
        RETURNING count,
                  EXTRACT(EPOCH FROM (window_start + make_interval(secs => %(window)s)
                                      - now()))::int AS retry_after
    """, {"bucket": bucket, "window": window_seconds}).fetchone()

    return row["count"] <= limit, row["count"], max(0, row["retry_after"])


def purge_rate_limits(conn, older_than_seconds=86400):
    """Drop expired buckets. Called from the nightly job, not per request."""
    return conn.execute(
        "DELETE FROM rate_limits WHERE window_start < now() - make_interval(secs => %s)",
        (older_than_seconds,)).rowcount


# Tuned so a human never notices and a script always does.
LIMITS = {
    # Credential stuffing. Keyed by IP *and* by the email being tried, because
    # one key alone is trivially evaded: a botnet rotates IPs against one
    # account, and one host sprays many accounts from a single IP.
    "login_ip":     (20, 900),      # 20 attempts / 15 min / IP
    "login_email":  (10, 900),      # 10 attempts / 15 min / account
    "signup_ip":    (5, 3600),      # 5 new accounts / hour / IP
    "api_org":      (120, 60),      # 120 API calls / min / org
    "report_org":   (30, 3600),     # 30 fraud reports / hour / org
    "check_org":    (240, 60),      # burst ceiling on top of the plan quota
}


def limited(conn, name, key):
    """Apply a named limit. Returns (allowed, retry_after_seconds)."""
    limit, window = LIMITS[name]
    allowed, count, retry_after = hit(conn, f"{name}:{key}", limit, window)
    if not allowed:
        log.warning("rate limit hit name=%s key=%s count=%s", name, key, count)
    return allowed, retry_after


# ---------------------------------------------------------------- audit

def record(conn, action, target=None, outcome="ok", detail=None,
           org_id=None, user_id=None):
    """
    Write an audit event.

    org_id/user_id default to the current request's identity but can be passed
    explicitly — a failed login has no session to read them from, and that is
    precisely the event worth recording.
    """
    org = g.get("org") if g else None
    user = g.get("user") if g else None

    conn.execute(
        """INSERT INTO audit_events
             (org_id, user_id, action, target, outcome, ip, user_agent, detail)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (org_id if org_id is not None else (org["id"] if org else None),
         user_id if user_id is not None else (user["user_id"] if user else None),
         action, target, outcome, client_ip(),
         (request.user_agent.string or "")[:500] if request else None,
         json.dumps(detail) if detail else None))


# ---------------------------------------------------------------- headers

def apply_headers(response, is_production):
    """
    Response hardening.

    `script-src 'none'` is the load-bearing directive, and the app can afford it
    because it ships zero JavaScript. That removes injected-script attacks
    entirely rather than mitigating them.

    `style-src` keeps 'unsafe-inline' because the templates use inline `style=`
    attributes, which that directive also governs. Dropping it would need every
    one of those refactored into classes — worth doing eventually, not worth a
    broken layout for brokers on older corporate browsers today. The residual
    risk is small and specifically bounded: CSS-based exfiltration needs an
    outbound channel, and `img-src 'self'` plus `connect-src 'self'` close it.
    """
    response.headers.setdefault("Content-Security-Policy", "; ".join([
        "default-src 'self'",
        "script-src 'none'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ]))
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()")

    if is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    # Verdict pages name a specific carrier and a specific broker's intent.
    # A shared proxy caching one and serving it to the next subscriber would
    # be both a tenancy breach and a wrong answer.
    if response.headers.get("Content-Type", "").startswith("text/html"):
        response.headers.setdefault("Cache-Control", "no-store, private")

    return response
