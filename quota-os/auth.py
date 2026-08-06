"""
QuotaOS — authentication, roles, and tenant isolation.

This module replaced the demo's "Viewing as" switcher, which let anyone claim any
identity. Three things live here:

  1. **Who you are.** Session-backed login, passwords hashed with scrypt via
     Werkzeug. No plaintext, no reversible storage, no password ever logged.

  2. **What you can see.** `@login_required` and `@role_required` gate routes;
     `visible_rep_ids()` narrows row access below that, because a manager and an
     admin can both open /org and should not see the same rows.

  3. **Which tenant you belong to.** `current_org_id()` is the value every query in
     app.py filters on. The rule is absolute: no query touches a tenant-owned table
     without an org_id predicate, even when a join would make it redundant. A single
     forgotten filter in a multi-tenant app is a customer-data leak, so the
     redundancy is the safety margin.

Roles, narrowest to widest:

    member   their own rep page and nothing else
    manager  their team, plus the hiring funnel
    admin    everything in the app for their org
    owner    everything, plus users and billing
"""

import functools
import hmac
import secrets
from datetime import date, datetime

from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("member", "manager", "admin", "owner")
_RANK = {r: i for i, r in enumerate(ROLES)}

SESSION_USER_KEY = "uid"
CSRF_KEY = "_csrf"


# ---------------------------------------------------------------- passwords --

def hash_password(raw):
    """Werkzeug picks scrypt by default; we never store or log the raw value."""
    return generate_password_hash(raw)


def verify_password(stored_hash, raw):
    return check_password_hash(stored_hash, raw)


def password_problem(raw):
    """
    Return a complaint string, or None if the password is acceptable.

    Deliberately a length floor rather than a character-class rule — composition
    rules push people toward 'Passw0rd!' and NIST stopped recommending them.
    """
    if not raw or len(raw) < 12:
        return "Password must be at least 12 characters."
    if raw.lower() in {"password1234", "quotaos12345", "123456789012"}:
        return "That password is too common."
    return None


# ------------------------------------------------------------------- users --

def load_user(conn, user_id):
    return conn.execute(
        """SELECT u.*, o.name org_name, o.slug org_slug, o.status org_status,
                  o.plan org_plan, o.ai_cents_cap
             FROM users u JOIN orgs o ON o.id = u.org_id
            WHERE u.id = ? AND u.status = 'active'""",
        (user_id,),
    ).fetchone()


def load_user_by_email(conn, email):
    return conn.execute(
        "SELECT * FROM users WHERE lower(email) = lower(?) AND status = 'active'",
        (email.strip(),),
    ).fetchone()


def login_user(conn, user):
    """Rotate the session id on privilege change — stops session fixation."""
    session.clear()
    session[SESSION_USER_KEY] = user["id"]
    session.permanent = True
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                 (datetime.now().isoformat(timespec="seconds"), user["id"]))
    conn.commit()


def logout_user():
    session.clear()


def current_user():
    return getattr(g, "user", None)


def current_org_id():
    """
    The tenant id every query must filter on.

    Aborts rather than returning None. A helper that can silently return None is a
    helper that eventually produces an unscoped query.
    """
    user = current_user()
    if user is None:
        abort(401)
    return user["org_id"]


# -------------------------------------------------------------- decorators --

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def role_required(minimum):
    """Gate a route on the role ladder. `manager` also admits admin and owner."""
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            user = current_user()
            if _RANK.get(user["role"], -1) < _RANK[minimum]:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def has_role(minimum):
    user = current_user()
    return bool(user) and _RANK.get(user["role"], -1) >= _RANK[minimum]


# ------------------------------------------------------------ row scoping --

def visible_rep_ids(conn):
    """
    Which rep rows this user may read, inside their own org.

    Returns None to mean "every rep in the org" — callers treat None as no extra
    filter. Admins and owners see everything; a manager sees their own team plus
    themselves; a member sees only their own seat.
    """
    user = current_user()
    if user is None:
        abort(401)
    if _RANK[user["role"]] >= _RANK["admin"]:
        return None

    org_id = user["org_id"]
    if user["role"] == "manager" and user["rep_id"]:
        rows = conn.execute(
            "SELECT id FROM reps WHERE org_id = ? AND (manager_id = ? OR id = ?)",
            (org_id, user["rep_id"], user["rep_id"]),
        ).fetchall()
        return {r["id"] for r in rows}

    return {user["rep_id"]} if user["rep_id"] else set()


def require_rep_visible(conn, rep_id):
    """403 unless this user may read this rep. Call before rendering a rep page."""
    allowed = visible_rep_ids(conn)
    if allowed is not None and rep_id not in allowed:
        abort(403)


def scoped_one(conn, table, row_id):
    """
    Fetch a row by id, but only within the caller's org.

    Using this instead of a bare `WHERE id = ?` is what stops an
    incrementing-integer URL from walking another tenant's records.
    """
    if table not in {"reps", "deals", "reqs", "candidates", "applications",
                     "interviews", "courses", "cert_tracks", "class_sessions"}:
        raise ValueError(f"scoped_one called on unexpected table: {table}")
    row = conn.execute(
        f"SELECT * FROM {table} WHERE id = ? AND org_id = ?",
        (row_id, current_org_id()),
    ).fetchone()
    if row is None:
        abort(404)
    return row


# ------------------------------------------------------------------- CSRF --

def csrf_token():
    if CSRF_KEY not in session:
        session[CSRF_KEY] = secrets.token_urlsafe(32)
    return session[CSRF_KEY]


def csrf_validate():
    """
    Constant-time check of the form token against the session token.

    Every state-changing route in this app is a POST, so guarding POST covers the
    whole mutable surface.
    """
    sent = request.form.get(CSRF_KEY, "")
    held = session.get(CSRF_KEY, "")
    if not held or not sent or not hmac.compare_digest(held, sent):
        abort(400, description="CSRF token missing or invalid. Reload and retry.")


# ------------------------------------------------------------- audit trail --

def audit(conn, action, entity=None, engine=None, detail=None, commit=True):
    """
    Record an AI-assisted or consequential action.

    Employment-decision regulations (NYC LL144, the EU AI Act) expect an
    organization to answer "what did the tool output for this candidate, when, on
    what input, and who acted on it". That is only answerable if it was written
    down at the time.
    """
    import json as _json
    user = current_user()
    conn.execute(
        """INSERT INTO audit_log (org_id, user_id, action, entity, engine, detail, ip, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (user["org_id"] if user else None,
         user["id"] if user else None,
         action, entity, engine,
         _json.dumps(detail) if detail is not None else None,
         request.headers.get("X-Forwarded-For", request.remote_addr),
         datetime.now().isoformat(timespec="seconds")),
    )
    if commit:
        conn.commit()


# ------------------------------------------------------------- onboarding --

def create_org_with_owner(conn, org_name, slug, user_name, email, password):
    """
    Sign up a new tenant. Returns (org_id, user_id).

    The first user is always the owner — somebody has to be able to add the second.
    """
    now = date.today().isoformat()
    org_id = conn.execute(
        "INSERT INTO orgs (name, slug, plan, status, created_at) VALUES (?,?,?,?,?)",
        (org_name, slug, "trial", "active", now),
    ).lastrowid
    user_id = conn.execute(
        """INSERT INTO users (org_id, email, password_hash, name, role, status, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (org_id, email.strip().lower(), hash_password(password), user_name,
         "owner", "active", now),
    ).lastrowid
    conn.commit()
    return org_id, user_id
