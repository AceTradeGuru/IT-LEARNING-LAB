"""
CarrierGuard — authentication, tenancy, and API keys.

Two ways in, one tenant boundary:

  * a browser session, for people
  * an API key, for a TMS firing checks at tender time

Both resolve to an organization, and every query downstream is scoped by it.
The tenant boundary is the thing to be paranoid about here: a broker's check
history is a list of exactly which carriers they are about to do business with.
Leaking it to another subscriber would be worse than leaking nothing at all,
because the subscribers are competitors.
"""

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime
from functools import wraps

from flask import abort, flash, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db

# Plan allowances, checked before a check is billed. Enterprise is unmetered.
PLAN_LIMITS = {"starter": 250, "pro": 1500, "enterprise": None}

API_KEY_PREFIX = "cg_live_"
PREFIX_DISPLAY_LEN = len(API_KEY_PREFIX) + 8

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 12


# ---------------------------------------------------------------- passwords

def hash_password(raw):
    # Werkzeug's default is scrypt — memory-hard, and no extra dependency.
    return generate_password_hash(raw)


def verify_password(stored_hash, raw):
    return check_password_hash(stored_hash, raw)


def password_problem(raw):
    """Return a human-readable problem with a password, or None if it's fine."""
    if len(raw or "") < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    return None


def email_problem(raw):
    if not EMAIL_RE.match((raw or "").strip()):
        return "Enter a valid email address."
    return None


# ---------------------------------------------------------------- API keys

def generate_api_key():
    """
    Return (full_key, prefix, hash).

    The full key is shown to the user exactly once and never stored. We keep a
    SHA-256 of it — fast is correct here, unlike for passwords: the key is 256
    bits of random, so there is no dictionary to attack and no reason to pay
    per-request KDF latency on every API call.
    """
    full = API_KEY_PREFIX + secrets.token_hex(32)
    return full, full[:PREFIX_DISPLAY_LEN], hash_api_key(full)


def hash_api_key(full):
    return hashlib.sha256(full.encode()).hexdigest()


def org_for_api_key(conn, presented):
    """Resolve a presented API key to its organization, or None."""
    if not presented or not presented.startswith(API_KEY_PREFIX):
        return None

    row = conn.execute(
        """SELECT k.id AS key_id, k.key_hash, o.*
           FROM api_keys k
           JOIN organizations o ON o.id = k.org_id
           WHERE k.key_hash = %s AND k.revoked_at IS NULL""",
        (hash_api_key(presented),)).fetchone()
    if row is None:
        return None

    # Constant-time even though the lookup was by hash: keeps the comparison
    # habit consistent so a future change to a scan-based lookup stays safe.
    if not hmac.compare_digest(row["key_hash"], hash_api_key(presented)):
        return None

    conn.execute("UPDATE api_keys SET last_used_at = now() WHERE id = %s",
                 (row["key_id"],))
    return row


# ---------------------------------------------------------------- sessions

def login_user(conn, user):
    session.clear()
    session["user_id"] = user["id"]
    session["org_id"] = user["org_id"]
    session.permanent = True
    conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user["id"],))


def logout_user():
    session.clear()


def load_current(conn):
    """Populate g.user and g.org from the session. Returns the org, or None."""
    user_id = session.get("user_id")
    if not user_id:
        return None

    row = conn.execute(
        """SELECT u.id AS user_id, u.email, u.full_name, u.role, o.*
           FROM users u JOIN organizations o ON o.id = u.org_id
           WHERE u.id = %s""", (user_id,)).fetchone()
    if row is None:
        # The user was deleted out from under a live session.
        session.clear()
        return None

    g.user = row
    g.org = row
    return row


# ---------------------------------------------------------------- decorators

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Sign in to continue.")
            # full_path, not path: a demo-scenario link carries its prefill in the
            # query string, and dropping it would land the user on a blank form.
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def api_key_required(view):
    """
    Authenticate a JSON request by `Authorization: Bearer cg_live_...`.

    Sets g.org and g.api_conn — the connection the key was resolved on, reused
    by the view so the whole request is one transaction.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not presented:
            return jsonify(error="missing_api_key",
                           message="Send your key as: Authorization: Bearer cg_live_..."), 401

        with db.connection() as conn:
            org = org_for_api_key(conn, presented)
            if org is None:
                return jsonify(error="invalid_api_key",
                               message="That API key is not valid or has been revoked."), 401
            if org["status"] != "active":
                return jsonify(error="account_inactive",
                               message=f"This account is {org['status']}."), 402

            g.org = org
            g.api_conn = conn
            return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------- metering

def quota_state(org):
    """Return (used, limit, exceeded) for an organization's current period."""
    limit = PLAN_LIMITS.get(org["plan"])
    used = org["checks_used"]
    return used, limit, (limit is not None and used >= limit)


def consume_check(conn, org_id):
    """
    Count one check against the org's allowance, rolling the period if needed.

    Rolls forward by whole months rather than resetting to "today": otherwise
    every burst of usage would slide the reset date and an org would drift into
    a longer effective billing period than it pays for.
    """
    conn.execute("""
        UPDATE organizations
           SET checks_used = CASE
                   WHEN period_start <= CURRENT_DATE - INTERVAL '1 month' THEN 1
                   ELSE checks_used + 1
               END,
             period_start = CASE
                   WHEN period_start <= CURRENT_DATE - INTERVAL '1 month'
                   THEN period_start + INTERVAL '1 month'
                   ELSE period_start
               END
         WHERE id = %s""", (org_id,))


# ---------------------------------------------------------------- signup

def slugify(name, suffix=""):
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "org"
    return f"{base}-{suffix}" if suffix else base


def create_organization(conn, company_name, email, password, full_name="",
                        plan="starter"):
    """
    Create an org and its first (owner) user in one transaction.

    Returns (user_row, None) or (None, error_message).
    """
    email = (email or "").strip().lower()

    if conn.execute("SELECT 1 FROM users WHERE lower(email) = %s",
                    (email,)).fetchone():
        return None, "An account with that email already exists."

    org = conn.execute(
        """INSERT INTO organizations (name, billing_email, plan, slug)
           VALUES (%s, %s, %s, %s)
           RETURNING id""",
        (company_name, email, plan,
         slugify(company_name, secrets.token_hex(3)))).fetchone()

    user = conn.execute(
        """INSERT INTO users (org_id, email, password_hash, full_name, role)
           VALUES (%s, %s, %s, %s, 'owner')
           RETURNING *""",
        (org["id"], email, hash_password(password), full_name or None)).fetchone()

    return user, None


def utcnow():
    return datetime.now(UTC)
