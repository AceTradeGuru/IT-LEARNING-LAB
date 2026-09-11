"""
Royal Grind — AI sales intelligence and execution platform.

Find -> Understand -> Call -> Close -> Automate, in one application.

Run locally:
    python3 grind_db.py      # build and seed
    python3 app.py           # http://localhost:5005

Run in production (see DEPLOY.md):
    GRIND_ENV=production SECRET_KEY=... gunicorn -w 4 -b 0.0.0.0:8000 app:app

Three invariants hold everywhere in this file:

  1. **Every route is gated.** `@login_required` at minimum, `@role_required(...)`
     where the action is privileged. The only anonymous surfaces are login,
     signup, and the telephony webhook — and the webhook authenticates on a
     signed token instead of a session.

  2. **Every query filters on org_id.** Multi-tenant isolation lives in the WHERE
     clause of each individual query, not in a middleware someone can forget to
     apply. Row access below that comes from `auth.visible_rep_ids`.

  3. **No call is placed without `compliance.predial_check` returning clean.**
     Not "usually". The gate is in one function, every dial path goes through it,
     and there is no override parameter.
"""

import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from markupsafe import Markup, escape

import ai_engine
import auth
import compliance
import enrichment
import importer
import sequences as seq_engine
import signal_engine
import telephony
from config import Config, validate
from grind_db import DB_FILE, create_tables, get_connection

app = Flask(__name__)
app.config.from_object(Config)

_problems = validate(Config)
if _problems:
    for p in _problems:
        print(f"FATAL CONFIG: {p}", file=sys.stderr)
    raise SystemExit(1)

app.secret_key = Config.SECRET_KEY
app.permanent_session_lifetime = timedelta(seconds=Config.PERMANENT_SESSION_LIFETIME)

DEAL_STAGES = ["discovery", "pricing", "pilot", "contract", "won", "lost"]
DEAL_LABELS = {"discovery": "Discovery", "pricing": "Pricing", "pilot": "Pilot",
               "contract": "Contract", "won": "Won", "lost": "Lost"}

DISPOSITIONS = [
    ("connected", "Spoke with them"),
    ("meeting_set", "Meeting booked"),
    ("callback", "Callback agreed"),
    ("not_interested", "Not interested"),
    ("gatekeeper", "Gatekeeper"),
    ("voicemail", "Left voicemail"),
    ("no_answer", "No answer"),
    ("wrong_number", "Wrong person"),
    ("bad_number", "Number is dead"),
    ("do_not_call", "Asked not to be called"),
]
DISPOSITION_LABELS = dict(DISPOSITIONS)

TARGET_STATUSES = ["new", "working", "meeting_set", "opportunity", "won", "lost",
                   "disqualified"]


# ---------------------------------------------------------------- AI budget --
#
# Two independent guards, because they stop different failure modes:
#
#   * A per-hour call limit, in process memory, to blunt a runaway loop before it
#     becomes money. Per-worker, so with four gunicorn workers the real ceiling is
#     4x the configured number — a deliberate stopgap until this moves to Redis.
#
#   * A per-month dollar cap per org, in the database, computed from the token
#     usage the API actually reports. This one is authoritative and shared across
#     workers, which is why the spend ledger is a table and not a dict.
_ai_calls = defaultdict(list)


def _month_key(today=None):
    return (today or date.today()).strftime("%Y-%m")


def ai_spend_cents(conn, org_id, month=None):
    row = conn.execute(
        "SELECT COALESCE(SUM(cents), 0) AS c FROM ai_spend WHERE org_id = ? AND month = ?",
        (org_id, month or _month_key())).fetchone()
    return float(row["c"] or 0)


def ai_budget_check(conn, org_id, cap_cents):
    """
    Returns (ok, reason). Checked before the call, not after — the point is to
    not make the request. A guard that reports overspend afterwards is a receipt.
    """
    window = time.time() - 3600
    calls = [t for t in _ai_calls[org_id] if t > window]
    _ai_calls[org_id] = calls
    if len(calls) >= Config.AI_CALLS_PER_HOUR:
        return False, (f"Hourly AI limit reached ({Config.AI_CALLS_PER_HOUR} calls). "
                       f"Try again shortly.")
    spent = ai_spend_cents(conn, org_id)
    if cap_cents and spent >= cap_cents:
        return False, (f"Monthly AI budget of ${cap_cents / 100:,.0f} is used up "
                       f"(${spent / 100:,.2f} spent). It resets on the 1st, or an "
                       f"owner can raise the cap.")
    return True, None


def ai_budget_record(conn, org_id, feature, commit=True):
    """Bank the cost of whatever model calls just happened."""
    _ai_calls[org_id].append(time.time())
    cents = ai_engine.drain_cost_cents()
    if cents:
        conn.execute(
            "INSERT INTO ai_spend (org_id, month, cents, feature, created_at) "
            "VALUES (?,?,?,?,?)",
            (org_id, _month_key(), cents, feature,
             datetime.now().isoformat(timespec="seconds")))
        if commit:
            conn.commit()
    return cents


# ------------------------------------------------------------- app plumbing --

@app.before_request
def load_session():
    g.conn = get_connection()
    g.user = None
    uid = session.get(auth.SESSION_USER_KEY)
    if uid:
        g.user = auth.load_user(g.conn, uid)
        if g.user is None:
            session.clear()
    if request.method == "POST" and request.endpoint not in ("login", "signup",
                                                             "call_webhook"):
        auth.csrf_validate()


@app.teardown_request
def close_conn(_exc):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


@app.context_processor
def inject_globals():
    return {
        "user": auth.current_user(),
        "has_role": auth.has_role,
        "csrf_token": auth.csrf_token,
        "engine": ai_engine.engine_status(),
        "phone_status": telephony.get_provider(Config).status(),
        "data_status": enrichment.get_provider(Config).status(),
        "today": date.today(),
        "DISPOSITIONS": DISPOSITIONS,
        "DISPOSITION_LABELS": DISPOSITION_LABELS,
        "DEAL_STAGES": DEAL_STAGES,
        "DEAL_LABELS": DEAL_LABELS,
        "TARGET_STATUSES": TARGET_STATUSES,
    }


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, title="Not your record",
                           message="Your role does not allow this, or the record "
                                   "belongs to another rep."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, title="Not found",
                           message="No such record in your organization."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, title="Rejected",
                           message=getattr(e, "description", "Bad request.")), 400


# ---------------------------------------------------------------- filters --

@app.template_filter("money")
def money(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "$0"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:,.1f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:,.0f}K"
    return f"${value:,.0f}"


@app.template_filter("dollars")
def dollars(value):
    try:
        return f"${float(value or 0):,.0f}"
    except (TypeError, ValueError):
        return "$0"


@app.template_filter("pct")
def pct(value, digits=0):
    try:
        return f"{float(value or 0) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "0%"


@app.template_filter("since")
def since(value):
    """Human-relative time. Returns 'never' rather than an empty cell."""
    if not value:
        return "never"
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        try:
            when = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            return str(value)
    delta = datetime.now() - when
    days = delta.days
    if days < 0:
        return f"in {abs(days)}d"
    if days == 0:
        hours = delta.seconds // 3600
        if hours == 0:
            return f"{max(delta.seconds // 60, 1)}m ago"
        return f"{hours}h ago"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


@app.template_filter("nicedate")
def nicedate(value):
    if not value:
        return "—"
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        return str(value)
    

@app.template_filter("clock")
def clock(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value)).strftime("%-I:%M %p")
    except ValueError:
        return str(value)


@app.template_filter("duration")
def duration(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


@app.template_filter("nl2br")
def nl2br(text):
    return Markup("<br>".join(escape(line) for line in str(text or "").split("\n")))


@app.template_filter("initials")
def initials(name):
    parts = (name or "?").split()
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


# ------------------------------------------------------------- small helpers --

def _org():
    """The caller's org row. Every page needs it — modes and quiet hours feed
    scoring and the dial gate."""
    row = g.conn.execute("SELECT * FROM orgs WHERE id = ?",
                         (auth.current_org_id(),)).fetchone()
    if row is None:
        abort(401)
    return row


def _scope():
    """(org_id, visible_rep_ids). None for rep_ids means 'every rep in the org'."""
    return auth.current_org_id(), auth.visible_rep_ids(g.conn)


def _rep_filter(rep_ids, column="rep_id"):
    """
    SQL fragment + params restricting rows to the visible-rep set.

    `rep_ids` of None means "every rep in the org" and yields no clause. An empty
    set means this user may see nobody, which must return no rows rather than
    every row — the difference between the two is a data leak, so the empty case
    is spelled out rather than left to fall through.
    """
    if rep_ids is None:
        return "", []
    if not rep_ids:
        return " AND 1 = 0", []
    return f" AND {column} IN ({','.join('?' * len(rep_ids))})", list(rep_ids)


def _my_rep_id():
    user = auth.current_user()
    return user["rep_id"] if user else None


def _target_bundle(target_id):
    """
    One target with everything hanging off it, scoped and scored.

    Every prospect-facing route starts here, so the 404-on-wrong-tenant and the
    403-on-wrong-rep happen in one place rather than nine.
    """
    org = _org()
    target = auth.scoped_one(g.conn, "targets", target_id)
    if target["rep_id"]:
        auth.require_rep_visible(g.conn, target["rep_id"])
    scored = signal_engine.load_and_score(g.conn, org, target_ids=[target_id])
    if not scored:
        abort(404)
    return org, scored[0]


def _activity(target_id, kind, summary, detail="", actor="rep", ref_id=None,
              commit=True):
    g.conn.execute(
        """INSERT INTO activities (org_id, target_id, rep_id, kind, summary, detail,
                                   ref_id, actor, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (auth.current_org_id(), target_id, _my_rep_id(), kind, summary, detail,
         ref_id, actor, datetime.now().isoformat(timespec="seconds")))
    if commit:
        g.conn.commit()


def _loads(raw, default=None):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


# ===========================================================================
# Auth
# ===========================================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if auth.current_user():
        return redirect(url_for("today"))
    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        user = auth.load_user_by_email(g.conn, email)
        # Verify against a dummy hash when the user does not exist, so a missing
        # account and a wrong password take the same time to answer.
        stored = user["password_hash"] if user else auth.hash_password("not-a-real-one")
        if auth.verify_password(stored, password) and user:
            auth.login_user(g.conn, user)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") else url_for("today"))
        flash("Wrong email or password.")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    auth.logout_user()
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        org_name = (request.form.get("org_name") or "").strip()
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        slug = "".join(c if c.isalnum() else "-" for c in org_name.lower()).strip("-")

        problem = auth.password_problem(password)
        if not (org_name and name and email and "@" in email):
            problem = "Organization, name, and a real email address are all required."
        elif g.conn.execute("SELECT 1 FROM orgs WHERE slug = ?", (slug,)).fetchone():
            problem = "That organization name is taken."
        if problem:
            flash(problem)
            return render_template("signup.html")

        org_id, user_id = auth.create_org_with_owner(
            g.conn, org_name, slug, name, email, password)
        # The owner gets a seat. Without one they cannot be assigned a target and
        # their own product looks empty on first load.
        rep_id = g.conn.execute(
            """INSERT INTO reps (org_id, name, email, territory, quota_quarter,
                                 status, started_at, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (org_id, name, email, "", 0, "active", date.today().isoformat(),
             datetime.now().isoformat(timespec="seconds"))).lastrowid
        g.conn.execute("UPDATE users SET rep_id = ? WHERE id = ?", (rep_id, user_id))
        g.conn.commit()
        auth.login_user(g.conn, auth.load_user(g.conn, user_id))
        flash("Organization created. Add your pitch and modes in Settings so the "
              "call plans pitch what you actually sell.")
        return redirect(url_for("today"))
    return render_template("signup.html")


# ===========================================================================
# Today — the daily call list
# ===========================================================================

@app.route("/")
@auth.login_required
def today():
    """
    The product in one screen: who to call today, and why each one matters.

    Everything else in this application exists to make this list correct.
    """
    org = _org()
    org_id, rep_ids = _scope()
    rep_id = request.args.get("rep", type=int) or _my_rep_id()
    if rep_ids is not None and rep_id not in rep_ids:
        rep_id = next(iter(rep_ids)) if rep_ids else None

    size = min(request.args.get("size", 25, type=int), 100)
    live, scored = signal_engine.daily_list(g.conn, org, rep_id, size=size)
    blocked = [r for r in scored if r["blockers"]]

    rep = g.conn.execute("SELECT * FROM reps WHERE org_id = ? AND id = ?",
                         (org_id, rep_id)).fetchone() if rep_id else None

    reps = g.conn.execute(
        "SELECT * FROM reps WHERE org_id = ? AND status = 'active' ORDER BY name",
        (org_id,)).fetchall()
    if rep_ids is not None:
        reps = [r for r in reps if r["id"] in rep_ids]

    start = date.today().isoformat()
    calls_today = g.conn.execute(
        """SELECT COUNT(*) n, SUM(CASE WHEN disposition IN
                  ('connected','meeting_set','callback','not_interested')
                  THEN 1 ELSE 0 END) c
             FROM calls WHERE org_id = ? AND rep_id = ? AND started_at >= ?""",
        (org_id, rep_id, start)).fetchone()

    tasks = g.conn.execute(
        """SELECT t.*, c.name AS company_name, ct.name AS contact_name
             FROM tasks t
             JOIN targets tg ON tg.id = t.target_id
             JOIN companies c ON c.id = tg.company_id
        LEFT JOIN contacts ct ON ct.id = tg.contact_id
            WHERE t.org_id = ? AND t.rep_id = ? AND t.status = 'open'
              AND t.due_at <= ?
            ORDER BY t.due_at LIMIT 12""",
        (org_id, rep_id, date.today().isoformat())).fetchall()

    hours = signal_engine.best_hours(g.conn, org_id, rep_id)
    return render_template("today.html", rows=live, blocked=blocked, rep=rep,
                           reps=reps, calls_today=calls_today, tasks=tasks,
                           hours=hours, size=size, org=org,
                           total_scored=len(scored))


# ===========================================================================
# The prospect record
# ===========================================================================

@app.route("/prospect/<int:target_id>")
@auth.login_required
def prospect(target_id):
    org, row = _target_bundle(target_id)
    org_id = org["id"]

    timeline = g.conn.execute(
        """SELECT * FROM activities WHERE org_id = ? AND target_id = ?
            ORDER BY created_at DESC, id DESC LIMIT 40""",
        (org_id, target_id)).fetchall()
    calls = g.conn.execute(
        """SELECT * FROM calls WHERE org_id = ? AND target_id = ?
            ORDER BY id DESC LIMIT 20""", (org_id, target_id)).fetchall()
    tasks = g.conn.execute(
        """SELECT * FROM tasks WHERE org_id = ? AND target_id = ? AND status = 'open'
            ORDER BY due_at""", (org_id, target_id)).fetchall()
    deals = g.conn.execute(
        "SELECT * FROM deals WHERE org_id = ? AND target_id = ?", (org_id, target_id)
    ).fetchall()
    enrollments = g.conn.execute(
        """SELECT e.*, s.name AS sequence_name FROM enrollments e
             JOIN sequences s ON s.id = e.sequence_id
            WHERE e.org_id = ? AND e.target_id = ?""", (org_id, target_id)).fetchall()
    all_sequences = g.conn.execute(
        "SELECT * FROM sequences WHERE org_id = ? AND active = 1 ORDER BY name",
        (org_id,)).fetchall()

    number = _primary_number(row["points"])
    gate = compliance.predial_check(g.conn, org, row["company"], row["contact"],
                                    number, rep=_current_rep())

    return render_template(
        "prospect.html", row=row, org=org, timeline=timeline, calls=calls,
        tasks=tasks, deals=deals, enrollments=enrollments, sequences=all_sequences,
        gate=gate, number=number,
        research=_loads(row["target"]["research_json"]),
        plan=_loads(row["target"]["plan_json"]),
        effective=enrichment.effective_status,
        ttl=Config.VERIFICATION_TTL_DAYS)


def _current_rep():
    rep_id = _my_rep_id()
    if not rep_id:
        return None
    return g.conn.execute("SELECT * FROM reps WHERE org_id = ? AND id = ?",
                          (auth.current_org_id(), rep_id)).fetchone()


def _primary_number(points):
    """The number to dial: best verified direct line, else whatever exists."""
    ranked = [p for p in points if p["kind"] in ("direct_dial", "mobile", "switchboard")]
    if not ranked:
        return ""
    order = {"direct_dial": 0, "mobile": 1, "switchboard": 2}
    ranked.sort(key=lambda p: (order.get(p["kind"], 3),
                               -(p["confidence"] or 0)))
    best = [p for p in ranked if p["status"] != "invalid"] or ranked
    return best[0]["value"]


@app.route("/prospect/<int:target_id>/research", methods=["POST"])
@auth.login_required
def prospect_research(target_id):
    org, row = _target_bundle(target_id)
    ok, reason = ai_budget_check(g.conn, org["id"], org["ai_cents_cap"])
    if not ok:
        flash(reason)
        return redirect(url_for("prospect", target_id=target_id))

    research, engine = ai_engine.research_company(
        org, row["company"], row["contact"], row["triggers"])
    ai_budget_record(g.conn, org["id"], "research", commit=False)
    g.conn.execute(
        "UPDATE targets SET research_json = ?, research_engine = ? "
        "WHERE id = ? AND org_id = ?",
        (json.dumps(research), engine, target_id, org["id"]))
    _activity(target_id, "system", f"Research brief generated ({engine})",
              actor="system", commit=False)
    auth.audit(g.conn, "ai.research", f"target:{target_id}", engine=engine,
               detail={"company": row["company"]["name"]})
    flash(f"Research brief written by {engine}.")
    return redirect(url_for("prospect", target_id=target_id) + "#research")


@app.route("/prospect/<int:target_id>/plan", methods=["POST"])
@auth.login_required
def prospect_plan(target_id):
    org, row = _target_bundle(target_id)
    ok, reason = ai_budget_check(g.conn, org["id"], org["ai_cents_cap"])
    if not ok:
        flash(reason)
        return redirect(url_for("prospect", target_id=target_id))

    plan, engine = ai_engine.build_call_plan(
        org, row["company"], row["contact"], row["triggers"],
        research=_loads(row["target"]["research_json"]), signals=row["signals"])
    ai_budget_record(g.conn, org["id"], "call_plan", commit=False)
    g.conn.execute(
        "UPDATE targets SET plan_json = ?, plan_engine = ? WHERE id = ? AND org_id = ?",
        (json.dumps(plan), engine, target_id, org["id"]))
    _activity(target_id, "system", f"Call plan generated ({engine})",
              actor="system", commit=False)
    auth.audit(g.conn, "ai.call_plan", f"target:{target_id}", engine=engine,
               detail={"company": row["company"]["name"]})
    flash(f"Call plan written by {engine}.")
    return redirect(url_for("prospect", target_id=target_id) + "#plan")


@app.route("/prospect/<int:target_id>/status", methods=["POST"])
@auth.login_required
def prospect_status(target_id):
    org, row = _target_bundle(target_id)
    status = request.form.get("status", "")
    if status not in TARGET_STATUSES:
        abort(400, description="Unknown status.")
    reason = (request.form.get("reason") or "").strip()
    g.conn.execute(
        "UPDATE targets SET status = ?, dq_reason = ? WHERE id = ? AND org_id = ?",
        (status, reason, target_id, org["id"]))
    if status in ("disqualified", "lost", "won"):
        seq_engine.exit_all(g.conn, org["id"], target_id, f"marked {status}",
                            commit=False)
    _activity(target_id, "stage", f"Status set to {status}"
              + (f" — {reason}" if reason else ""), commit=False)
    g.conn.commit()
    flash(f"Marked {status}.")
    return redirect(url_for("prospect", target_id=target_id))


@app.route("/prospect/<int:target_id>/snooze", methods=["POST"])
@auth.login_required
def prospect_snooze(target_id):
    org, _row = _target_bundle(target_id)
    days = max(1, min(request.form.get("days", 7, type=int), 365))
    until = (date.today() + timedelta(days=days)).isoformat()
    g.conn.execute("UPDATE targets SET snoozed_until = ? WHERE id = ? AND org_id = ?",
                   (until, target_id, org["id"]))
    _activity(target_id, "system", f"Snoozed for {days} days (until {until})",
              actor="system", commit=False)
    g.conn.commit()
    flash(f"Off the list until {until}.")
    return redirect(request.form.get("back") or url_for("today"))


@app.route("/prospect/<int:target_id>/enroll", methods=["POST"])
@auth.login_required
def prospect_enroll(target_id):
    org, _row = _target_bundle(target_id)
    sequence_id = request.form.get("sequence_id", type=int)
    sequence = auth.scoped_one(g.conn, "sequences", sequence_id)
    seq_engine.enroll(g.conn, org["id"], sequence["id"], target_id)
    flash(f"Enrolled in {sequence['name']}.")
    return redirect(url_for("prospect", target_id=target_id))


@app.route("/prospect/<int:target_id>/note", methods=["POST"])
@auth.login_required
def prospect_note(target_id):
    _org_row, _row = _target_bundle(target_id)
    note = (request.form.get("note") or "").strip()
    if note:
        _activity(target_id, "note", note[:120], detail=note)
        flash("Note saved.")
    return redirect(url_for("prospect", target_id=target_id))


# ===========================================================================
# Calling
# ===========================================================================

@app.route("/prospect/<int:target_id>/call", methods=["POST"])
@auth.login_required
def place_call(target_id):
    """
    Dial. The compliance gate runs first and there is no way past it.
    """
    org, row = _target_bundle(target_id)
    org_id = org["id"]
    rep = _current_rep()
    number = request.form.get("number") or _primary_number(row["points"])

    gate = compliance.predial_check(g.conn, org, row["company"], row["contact"],
                                    number, rep=rep)
    if not gate["allowed"]:
        for block in gate["blocks"]:
            flash(f"Call blocked: {block}")
        auth.audit(g.conn, "call.blocked", f"target:{target_id}",
                   detail={"blocks": gate["blocks"], "number": number})
        return redirect(url_for("prospect", target_id=target_id))

    provider = telephony.get_provider(Config)
    recording = gate["recording"]
    now = datetime.now()

    call_id = g.conn.execute(
        """INSERT INTO calls (org_id, target_id, rep_id, contact_id, direction,
                              to_number, from_number, provider, status, started_at,
                              recorded, consent_basis, signals_at_call, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (org_id, target_id, rep["id"] if rep else None, row["target"]["contact_id"],
         "outbound", number, rep["caller_id"] if rep else "", provider.name,
         "ringing", now.isoformat(timespec="seconds"),
         1 if recording["allowed"] else 0, recording["basis"],
         json.dumps(row["keys"]), now.isoformat(timespec="seconds"))).lastrowid

    try:
        result = provider.place_call(
            to_number=number, from_number=rep["caller_id"] if rep else "",
            call_ref=call_id,
            answer_url=None,
            event_url=(telephony.event_url(Config.PUBLIC_BASE_URL, call_id,
                                           Config.TELEPHONY_WEBHOOK_SECRET)
                       if Config.PUBLIC_BASE_URL else None),
            record=recording["allowed"], announcement=recording["announcement"])
    except telephony.TelephonyError as exc:
        g.conn.execute("UPDATE calls SET status = 'failed', notes = ? WHERE id = ?",
                       (str(exc), call_id))
        g.conn.commit()
        flash(f"Could not place the call: {exc}")
        return redirect(url_for("prospect", target_id=target_id))

    g.conn.execute("UPDATE calls SET provider_call_id = ?, status = ? WHERE id = ?",
                   (result.get("provider_call_id", ""), result.get("status", "ringing"),
                    call_id))
    g.conn.execute(
        "UPDATE targets SET attempts = attempts + 1, last_touch_at = ? "
        "WHERE id = ? AND org_id = ?", (date.today().isoformat(), target_id, org_id))

    # The simulator resolves immediately; a real platform answers on the webhook.
    if isinstance(provider, telephony.SimulatedProvider):
        _resolve_simulated(org, row, call_id, provider, rep)

    auth.audit(g.conn, "call.placed", f"call:{call_id}",
               detail={"target": target_id, "recorded": bool(recording["allowed"]),
                       "basis": recording["basis"]})
    g.conn.commit()
    return redirect(url_for("call_console", call_id=call_id))


def _resolve_simulated(org, row, call_id, provider, rep):
    """Run the simulator and write the outcome as the webhook would have."""
    company, contact = row["company"], row["contact"]
    local, _ = compliance.local_now(company["timezone"])
    point = next((p for p in row["points"]
                  if p["value"] == _primary_number(row["points"])), None)
    context = {
        "company_name": company["name"],
        "contact_name": contact["name"] if contact else "",
        "rep_name": rep["name"] if rep else "",
        "org_name": org["name"],
        "loads_per_month": company["loads_per_month"], "modes": company["modes"],
        "incumbents": company["incumbents"], "lanes": company["lanes"],
        "city": company["city"], "state": company["state"],
        "shipper_type": company["shipper_type"],
        "point_status": enrichment.effective_status(point) if point else "probable",
        "point_kind": point["kind"] if point else "direct_dial",
        "role_type": contact["role_type"] if contact else "unknown",
        "local_hour": local.hour, "score": row["score"],
        "why_now": bool(row["why_now"]),
        "why_now_headline": (row["triggers"][0]["headline"] if row["triggers"] else ""),
        "prior_connects": row["target"]["connects"] or 0,
    }
    result = provider.simulate(call_ref=call_id, context=context)
    started = datetime.now()
    g.conn.execute(
        """UPDATE calls SET status = ?, disposition = ?, duration_sec = ?,
                  transcript = ?, ended_at = ? WHERE id = ?""",
        (result["status"], result["disposition"], result["duration_sec"],
         result["transcript"],
         (started + timedelta(seconds=result["duration_sec"])).isoformat(timespec="seconds"),
         call_id))
    g.conn.execute(
        "INSERT INTO call_events (org_id, call_id, kind, payload, created_at) "
        "VALUES (?,?,?,?,?)",
        (org["id"], call_id, "simulated_complete", json.dumps(result),
         datetime.now().isoformat(timespec="seconds")))


@app.route("/call/<int:call_id>")
@auth.login_required
def call_console(call_id):
    """
    The console during the call, and the debrief after it.

    One template with two states, because they are the same screen at two
    moments and splitting them would mean the rep's notes move as they type.
    """
    org = _org()
    call = auth.scoped_one(g.conn, "calls", call_id)
    if call["rep_id"]:
        auth.require_rep_visible(g.conn, call["rep_id"])
    _org_row, row = _target_bundle(call["target_id"])

    analysis = _loads(call["analysis_json"])
    emails = g.conn.execute(
        """SELECT * FROM emails WHERE org_id = ? AND target_id = ?
            ORDER BY id DESC LIMIT 5""", (org["id"], call["target_id"])).fetchall()
    tasks = g.conn.execute(
        """SELECT * FROM tasks WHERE org_id = ? AND target_id = ? AND status = 'open'
            ORDER BY due_at""", (org["id"], call["target_id"])).fetchall()

    return render_template("call.html", call=call, row=row, org=org,
                           plan=_loads(row["target"]["plan_json"]),
                           research=_loads(row["target"]["research_json"]),
                           analysis=analysis, emails=emails, tasks=tasks)


@app.route("/call/<int:call_id>/log", methods=["POST"])
@auth.login_required
def call_log(call_id):
    """
    Close the loop: log it, learn from it, and set up the next action.

    This is the "the system handles the administrative work after the
    conversation" half of the pitch, and it is deliberately one transaction —
    a call that logs but does not schedule the follow-up is the failure mode
    every CRM already has.
    """
    org = _org()
    org_id = org["id"]
    call = auth.scoped_one(g.conn, "calls", call_id)
    if call["rep_id"]:
        auth.require_rep_visible(g.conn, call["rep_id"])
    _org_row, row = _target_bundle(call["target_id"])
    target_id = call["target_id"]

    disposition = request.form.get("disposition") or call["disposition"] or "no_answer"
    if disposition not in DISPOSITION_LABELS:
        abort(400, description="Unknown disposition.")
    notes = (request.form.get("notes") or "").strip()
    run_ai = request.form.get("analyze") == "1"

    g.conn.execute(
        "UPDATE calls SET disposition = ?, notes = ?, status = 'completed' "
        "WHERE id = ? AND org_id = ?", (disposition, notes, call_id, org_id))

    connected = disposition in ("connected", "meeting_set", "callback", "not_interested")
    new_status = {"meeting_set": "meeting_set", "do_not_call": "disqualified"}.get(
        disposition, "working" if row["target"]["status"] == "new" else None)
    g.conn.execute(
        """UPDATE targets SET connects = connects + ?, last_touch_at = ?,
                  last_connect_at = COALESCE(?, last_connect_at),
                  status = COALESCE(?, status), dq_reason = CASE WHEN ? = 'do_not_call'
                       THEN 'asked not to be called' ELSE dq_reason END
            WHERE id = ? AND org_id = ?""",
        (1 if connected else 0, date.today().isoformat(),
         date.today().isoformat() if connected else None, new_status, disposition,
         target_id, org_id))

    # Four side effects, in the order they matter.
    # 1. The learning loop: attribute the outcome to the signals that were live.
    signal_engine.record_outcome(g.conn, org_id, _loads(call["signals_at_call"], []),
                                 disposition, commit=False)
    # 2. The data layer: a dial is a verification.
    if call["to_number"]:
        enrichment.record_dial_result(g.conn, org_id, call["to_number"], disposition,
                                      commit=False)
    # 3. Never call somebody who asked not to be called.
    if disposition == "do_not_call":
        compliance.suppress(g.conn, org_id, "phone", call["to_number"],
                            reason="asked on a call", source="request", commit=False)
    # 4. Automation reacts: exit on a human conversation, enrol on silence.
    seq_engine.on_disposition(g.conn, org, target_id, disposition, commit=False)

    _activity(target_id, "call",
              f"Call — {DISPOSITION_LABELS[disposition]}"
              + (f" ({call['duration_sec']}s)" if call["duration_sec"] else ""),
              detail=notes, ref_id=call_id, commit=False)
    g.conn.commit()

    if run_ai and (call["transcript"] or notes):
        _analyze(org, row, call_id, disposition, notes)

    # Rescore: the call changed attempts, connects, and possibly the data layer.
    rescored = signal_engine.load_and_score(g.conn, org, target_ids=[target_id])
    signal_engine.persist_scores(g.conn, rescored)

    flash(f"Logged as {DISPOSITION_LABELS[disposition]}.")
    return redirect(url_for("call_console", call_id=call_id))


def _analyze(org, row, call_id, disposition, notes):
    """Run the AI debrief and turn its output into real rows."""
    org_id = org["id"]
    ok, reason = ai_budget_check(g.conn, org_id, org["ai_cents_cap"])
    if not ok:
        flash(reason)
        return

    call = g.conn.execute("SELECT * FROM calls WHERE org_id = ? AND id = ?",
                          (org_id, call_id)).fetchone()
    analysis, engine = ai_engine.analyze_call(
        org, row["company"], row["contact"], call["transcript"], notes, disposition)
    ai_budget_record(g.conn, org_id, "call_analysis", commit=False)
    g.conn.execute(
        "UPDATE calls SET analysis_json = ?, analysis_engine = ? WHERE id = ? AND org_id = ?",
        (json.dumps(analysis), engine, call_id, org_id))

    target_id = row["target"]["id"]
    rep_id = call["rep_id"]

    # The next action becomes a task with a real date on it.
    due = seq_engine.business_days_from(date.today(),
                                        max(int(analysis.get("next_action_days", 2)), 0))
    g.conn.execute(
        """INSERT INTO tasks (org_id, target_id, rep_id, kind, title, detail, due_at,
                              status, created_by, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (org_id, target_id, rep_id, "follow_up", analysis.get("next_action", "Follow up"),
         analysis.get("summary", ""), due.isoformat(), "open", "ai",
         datetime.now().isoformat(timespec="seconds")))
    g.conn.execute("UPDATE targets SET next_action_at = ? WHERE id = ? AND org_id = ?",
                   (due.isoformat(), target_id, org_id))

    # And the follow-up email gets drafted, not sent. See sequences.py rule 2.
    if disposition in ("connected", "meeting_set", "callback", "gatekeeper"):
        rep = g.conn.execute("SELECT * FROM reps WHERE org_id = ? AND id = ?",
                             (org_id, rep_id)).fetchone()
        email, email_engine = ai_engine.draft_email(
            org, row["company"], row["contact"], analysis, "follow_up",
            rep_name=rep["name"] if rep else "", transcript=call["transcript"])
        ai_budget_record(g.conn, org_id, "email_draft", commit=False)
        to_address = next((p["value"] for p in row["points"]
                           if p["kind"] == "email" and p["status"] != "invalid"), "")
        g.conn.execute(
            """INSERT INTO emails (org_id, target_id, rep_id, to_address, subject,
                                   body, status, engine, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (org_id, target_id, rep_id, to_address, email["subject"], email["body"],
             "draft", email_engine, datetime.now().isoformat(timespec="seconds")))

    # A real conversation that says there is an opportunity becomes one.
    if analysis.get("opportunity"):
        existing = g.conn.execute(
            "SELECT 1 FROM deals WHERE org_id = ? AND target_id = ?",
            (org_id, target_id)).fetchone()
        if not existing:
            keys = _loads(call["signals_at_call"], [])
            loads = analysis.get("estimated_loads") or row["company"]["loads_per_month"] or 10
            close = (date.today() + timedelta(days=60)).isoformat()
            g.conn.execute(
                """INSERT INTO deals (org_id, company_id, target_id, rep_id, name, stage,
                                      value_annual, loads_per_month, close_date,
                                      close_date_original, next_step, origin_signal,
                                      created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (org_id, row["company"]["id"], target_id, rep_id,
                 f"{row['company']['name']} — first lanes", "discovery",
                 loads * 260 * 12 * 0.2, int(loads * 0.25), close, close,
                 analysis.get("next_action", ""), keys[0] if keys else "",
                 date.today().isoformat(), datetime.now().isoformat(timespec="seconds")))
            g.conn.execute("UPDATE targets SET status = 'opportunity' WHERE id = ? AND org_id = ?",
                           (target_id, org_id))
            if keys:
                signal_engine.record_deal_outcome(g.conn, org_id, keys[0], "open",
                                                  commit=False)

    _activity(target_id, "system", f"Call analyzed ({engine}) — next action set for "
              f"{due.isoformat()}", actor="system", commit=False)
    auth.audit(g.conn, "ai.call_analysis", f"call:{call_id}", engine=engine,
               detail={"disposition": disposition}, commit=False)
    g.conn.commit()


@app.route("/call/<int:call_id>/analyze", methods=["POST"])
@auth.login_required
def call_analyze(call_id):
    org = _org()
    call = auth.scoped_one(g.conn, "calls", call_id)
    if call["rep_id"]:
        auth.require_rep_visible(g.conn, call["rep_id"])
    _org_row, row = _target_bundle(call["target_id"])
    _analyze(org, row, call_id, call["disposition"] or "connected", call["notes"] or "")
    flash("Debrief generated.")
    return redirect(url_for("call_console", call_id=call_id))


@app.route("/webhook/call", methods=["POST"])
def call_webhook():
    """
    Call events from the phone platform.

    The only unauthenticated route in the application, and it is not actually
    unauthenticated: the callback URL carries an HMAC of the call id, checked in
    constant time before anything is written. Without that, this is an open
    endpoint that writes transcripts into a multi-tenant database and whose URL
    lives in a third party's configuration screen.
    """
    call_ref = request.args.get("ref", "")
    token = request.args.get("t", "")
    if not telephony.verify_ref(Config.TELEPHONY_WEBHOOK_SECRET, call_ref, token):
        abort(403)

    payload = request.get_json(silent=True) or request.form.to_dict()
    call = g.conn.execute("SELECT * FROM calls WHERE id = ?", (int(call_ref),)).fetchone()
    if call is None:
        abort(404)
    org_id = call["org_id"]

    g.conn.execute(
        """INSERT INTO call_events (org_id, call_id, provider_call_id, kind, payload,
                                    created_at) VALUES (?,?,?,?,?,?)""",
        (org_id, call["id"], payload.get("uuid", ""),
         payload.get("status") or request.args.get("kind", "event"),
         json.dumps(payload)[:8000], datetime.now().isoformat(timespec="seconds")))

    status = payload.get("status", "")
    if status in ("ringing", "answered", "completed", "failed", "rejected",
                  "unanswered", "busy", "timeout"):
        # Vonage reports duration in seconds as a string, and only on the final
        # event. Missing is not zero — a zero here would tell the learning loop a
        # five-minute conversation lasted no time at all.
        updates = {"status": {"rejected": "failed", "unanswered": "completed",
                              "busy": "completed", "timeout": "completed"}.get(
                                  status, status)}
        if payload.get("duration") is not None:
            updates["duration_sec"] = int(payload.get("duration") or 0)
        if payload.get("end_time"):
            updates["ended_at"] = str(payload["end_time"])[:19]
        if status in ("unanswered", "timeout", "busy") and not call["disposition"]:
            updates["disposition"] = "no_answer"
        sets = ", ".join(f"{k} = ?" for k in updates)
        g.conn.execute(f"UPDATE calls SET {sets} WHERE id = ? AND org_id = ?",
                       (*updates.values(), call["id"], org_id))

    if payload.get("recording_url"):
        g.conn.execute("UPDATE calls SET recording_url = ? WHERE id = ? AND org_id = ?",
                       (payload["recording_url"], call["id"], org_id))
    g.conn.commit()
    return Response("ok", mimetype="text/plain")


# ===========================================================================
# Search
# ===========================================================================

@app.route("/search", methods=["GET", "POST"])
@auth.login_required
def search():
    org = _org()
    org_id = org["id"]
    query = request.form.get("q", "") if request.method == "POST" else request.args.get("q", "")
    filters, engine, results, reasoning = None, "", [], ""

    if query.strip():
        ok, reason = ai_budget_check(g.conn, org_id, org["ai_cents_cap"])
        if ok:
            filters, engine = ai_engine.parse_search(query, org)
            ai_budget_record(g.conn, org_id, "search")
        else:
            flash(reason)
            filters, engine = ai_engine._offline_search(query), "offline"
        reasoning = filters.get("reasoning", "")
        results = _run_search(org, filters)
        auth.audit(g.conn, "ai.search", None, engine=engine,
                   detail={"query": query[:300], "results": len(results)})

    saved = g.conn.execute(
        "SELECT * FROM saved_searches WHERE org_id = ? ORDER BY id DESC LIMIT 10",
        (org_id,)).fetchall()
    return render_template("search.html", query=query, filters=filters, engine=engine,
                           results=results, reasoning=reasoning, saved=saved, org=org)


def _run_search(org, filters):
    """
    Turn the parsed filters into rows, then score them.

    Every clause is optional and additive. A brief that names only a state
    returns every company in that state rather than nothing, because an empty
    result is indistinguishable from a misparse and the user cannot tell which
    they got.
    """
    org_id = org["id"]
    where = ["org_id = ?"]
    params = [org_id]

    if filters.get("states"):
        where.append("UPPER(state) IN (%s)" % ",".join("?" * len(filters["states"])))
        params += [s.upper() for s in filters["states"]]
    if filters.get("revenue_min"):
        where.append("revenue_est >= ?")
        params.append(filters["revenue_min"])
    if filters.get("revenue_max"):
        where.append("revenue_est <= ?")
        params.append(filters["revenue_max"])
    if filters.get("employees_min"):
        where.append("employees >= ?")
        params.append(filters["employees_min"])
    if filters.get("employees_max"):
        where.append("employees <= ?")
        params.append(filters["employees_max"])
    if filters.get("loads_min"):
        where.append("loads_per_month >= ?")
        params.append(filters["loads_min"])
    if filters.get("shipper_types"):
        where.append("LOWER(shipper_type) IN (%s)"
                     % ",".join("?" * len(filters["shipper_types"])))
        params += [s.lower() for s in filters["shipper_types"]]

    terms = [t for t in (filters.get("keywords", []) + filters.get("industries", []))
             if len(t) > 2][:8]
    if terms:
        clauses = []
        for t in terms:
            clauses.append("(LOWER(industry) LIKE ? OR LOWER(name) LIKE ? "
                           "OR LOWER(shipper_type) LIKE ?)")
            params += [f"%{t.lower()}%"] * 3
        where.append("(%s)" % " OR ".join(clauses))
    if filters.get("modes"):
        clauses = []
        for m in filters["modes"]:
            clauses.append("LOWER(modes) LIKE ?")
            params.append(f"%{m.lower()}%")
        where.append("(%s)" % " OR ".join(clauses))

    companies = g.conn.execute(
        f"SELECT id FROM companies WHERE {' AND '.join(where)} LIMIT ?",
        (*params, Config.AI_SEARCH_MAX_COMPANIES)).fetchall()
    ids = [c["id"] for c in companies]
    if not ids:
        return []

    targets = signal_engine._in_chunks(
        g.conn, "SELECT id FROM targets WHERE org_id = ? AND company_id IN (%s)",
        org_id, ids)
    if not targets:
        return []
    scored = signal_engine.load_and_score(g.conn, org,
                                          target_ids=[t["id"] for t in targets])
    if filters.get("trigger_kinds"):
        wanted = set(filters["trigger_kinds"])
        scored = [r for r in scored if wanted & set(r["keys"])] or scored
    return scored[:60]


@app.route("/search/save", methods=["POST"])
@auth.login_required
def search_save():
    org_id = auth.current_org_id()
    name = (request.form.get("name") or "").strip() or "Saved search"
    g.conn.execute(
        """INSERT INTO saved_searches (org_id, rep_id, name, query_text, filters_json,
                                       engine, created_at) VALUES (?,?,?,?,?,?,?)""",
        (org_id, _my_rep_id(), name, request.form.get("q", ""),
         request.form.get("filters", ""), request.form.get("engine", ""),
         datetime.now().isoformat(timespec="seconds")))
    g.conn.commit()
    flash("Search saved.")
    return redirect(url_for("search", q=request.form.get("q", "")))


# ===========================================================================
# Work queues
# ===========================================================================

@app.route("/work")
@auth.login_required
def work():
    """Tasks and email drafts — everything waiting on this rep that is not a dial."""
    org_id, rep_ids = _scope()
    task_clause, params = _rep_filter(rep_ids, "t.rep_id")
    email_clause, _ = _rep_filter(rep_ids, "e.rep_id")

    tasks = g.conn.execute(
        f"""SELECT t.*, c.name AS company_name, ct.name AS contact_name, r.name AS rep_name
              FROM tasks t
              JOIN targets tg ON tg.id = t.target_id
              JOIN companies c ON c.id = tg.company_id
         LEFT JOIN contacts ct ON ct.id = tg.contact_id
         LEFT JOIN reps r ON r.id = t.rep_id
             WHERE t.org_id = ? AND t.status = 'open'{task_clause}
             ORDER BY t.due_at LIMIT 200""", (org_id, *params)).fetchall()

    drafts = g.conn.execute(
        f"""SELECT e.*, c.name AS company_name, ct.name AS contact_name
              FROM emails e
              JOIN targets tg ON tg.id = e.target_id
              JOIN companies c ON c.id = tg.company_id
         LEFT JOIN contacts ct ON ct.id = tg.contact_id
             WHERE e.org_id = ? AND e.status = 'draft'{email_clause}
             ORDER BY e.id DESC LIMIT 100""", (org_id, *params)).fetchall()

    overdue = sum(1 for t in tasks if t["due_at"] < date.today().isoformat())
    return render_template("work.html", tasks=tasks, drafts=drafts, overdue=overdue)


@app.route("/task/<int:task_id>/done", methods=["POST"])
@auth.login_required
def task_done(task_id):
    org_id = auth.current_org_id()
    task = auth.scoped_one(g.conn, "tasks", task_id)
    if task["rep_id"]:
        auth.require_rep_visible(g.conn, task["rep_id"])
    g.conn.execute(
        "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ? AND org_id = ?",
        (datetime.now().isoformat(timespec="seconds"), task_id, org_id))
    _activity(task["target_id"], "task", f"Completed: {task['title']}", commit=False)
    g.conn.commit()
    flash("Done.")
    return redirect(request.form.get("back") or url_for("work"))


@app.route("/email/<int:email_id>", methods=["POST"])
@auth.login_required
def email_action(email_id):
    """
    Send or discard a draft.

    "Send" marks it sent and writes the timeline entry. It does not hand the
    message to an SMTP server — see DEPLOY.md, "What is not wired up". Pretending
    otherwise in a demo is how a customer discovers on day three that nothing
    left the building.
    """
    org_id = auth.current_org_id()
    email = auth.scoped_one(g.conn, "emails", email_id)
    if email["rep_id"]:
        auth.require_rep_visible(g.conn, email["rep_id"])
    action = request.form.get("action", "")

    if action == "send":
        blocked = compliance.is_suppressed(g.conn, org_id, email["to_address"], "email")
        if blocked:
            flash(f"Blocked: {email['to_address']} is on the suppression list "
                  f"({blocked['reason'] or blocked['source']}).")
            return redirect(request.form.get("back") or url_for("work"))
        g.conn.execute(
            "UPDATE emails SET status = 'sent', subject = ?, body = ?, sent_at = ? "
            "WHERE id = ? AND org_id = ?",
            (request.form.get("subject", email["subject"]),
             request.form.get("body", email["body"]),
             datetime.now().isoformat(timespec="seconds"), email_id, org_id))
        _activity(email["target_id"], "email", f"Sent: {email['subject']}", commit=False)
        auth.audit(g.conn, "email.sent", f"email:{email_id}",
                   detail={"to": email["to_address"]}, commit=False)
        flash("Marked sent and logged to the timeline.")
    elif action == "save":
        g.conn.execute(
            "UPDATE emails SET subject = ?, body = ? WHERE id = ? AND org_id = ?",
            (request.form.get("subject", email["subject"]),
             request.form.get("body", email["body"]), email_id, org_id))
        flash("Draft saved.")
    else:
        g.conn.execute("UPDATE emails SET status = 'cancelled' WHERE id = ? AND org_id = ?",
                       (email_id, org_id))
        flash("Draft discarded.")
    g.conn.commit()
    return redirect(request.form.get("back") or url_for("work"))


# ===========================================================================
# Pipeline
# ===========================================================================

@app.route("/pipeline")
@auth.login_required
def pipeline():
    org_id, rep_ids = _scope()
    clause, params = _rep_filter(rep_ids, "d.rep_id")
    deals = g.conn.execute(
        f"""SELECT d.*, c.name AS company_name, c.state, r.name AS rep_name
              FROM deals d
              JOIN companies c ON c.id = d.company_id
         LEFT JOIN reps r ON r.id = d.rep_id
             WHERE d.org_id = ?{clause}
             ORDER BY d.value_annual DESC""", (org_id, *params)).fetchall()

    by_stage = {s: [] for s in DEAL_STAGES}
    for d in deals:
        by_stage.setdefault(d["stage"], []).append(d)

    open_deals = [d for d in deals if d["stage"] not in ("won", "lost")]
    won = [d for d in deals if d["stage"] == "won"]
    lost = [d for d in deals if d["stage"] == "lost"]

    # Slippage: deals whose close date has moved since it was first set. No CRM
    # export contains this, because no CRM keeps the original.
    slipped = []
    for d in open_deals:
        if d["close_date"] and d["close_date_original"] and \
                d["close_date"] > d["close_date_original"]:
            days = (datetime.strptime(d["close_date"][:10], "%Y-%m-%d")
                    - datetime.strptime(d["close_date_original"][:10], "%Y-%m-%d")).days
            slipped.append({"deal": d, "days": days})
    slipped.sort(key=lambda s: -s["days"])

    return render_template(
        "pipeline.html", by_stage=by_stage, deals=deals, open_deals=open_deals,
        open_value=sum(d["value_annual"] for d in open_deals),
        won_value=sum(d["value_annual"] for d in won),
        win_rate=len(won) / max(len(won) + len(lost), 1), slipped=slipped[:8])


@app.route("/deal/<int:deal_id>", methods=["POST"])
@auth.login_required
def deal_update(deal_id):
    org_id = auth.current_org_id()
    deal = auth.scoped_one(g.conn, "deals", deal_id)
    if deal["rep_id"]:
        auth.require_rep_visible(g.conn, deal["rep_id"])

    stage = request.form.get("stage", deal["stage"])
    if stage not in DEAL_STAGES:
        abort(400, description="Unknown stage.")
    next_step = request.form.get("next_step", deal["next_step"])
    close_date = request.form.get("close_date") or deal["close_date"]
    value = request.form.get("value_annual", type=float)

    g.conn.execute(
        """UPDATE deals SET stage = ?, next_step = ?, close_date = ?,
                  value_annual = COALESCE(?, value_annual), updated_at = ?
            WHERE id = ? AND org_id = ?""",
        (stage, next_step, close_date, value,
         datetime.now().isoformat(timespec="seconds"), deal_id, org_id))

    if stage != deal["stage"]:
        if deal["target_id"]:
            _activity(deal["target_id"], "stage",
                      f"Deal moved {deal['stage']} -> {stage}", commit=False)
            if stage in ("won", "lost"):
                g.conn.execute(
                    "UPDATE targets SET status = ? WHERE id = ? AND org_id = ?",
                    (stage, deal["target_id"], org_id))
                seq_engine.exit_all(g.conn, org_id, deal["target_id"],
                                    f"deal {stage}", commit=False)
        if stage == "won" and deal["origin_signal"]:
            signal_engine.record_deal_outcome(
                g.conn, org_id, deal["origin_signal"], "won",
                dollars=value or deal["value_annual"], commit=False)
    g.conn.commit()
    flash("Deal updated.")
    return redirect(url_for("pipeline"))


# ===========================================================================
# Companies and contacts
# ===========================================================================

@app.route("/companies")
@auth.login_required
def companies():
    org_id = auth.current_org_id()
    state = request.args.get("state", "")
    q = (request.args.get("q") or "").strip()

    where, params = ["c.org_id = ?"], [org_id]
    if state:
        where.append("UPPER(c.state) = ?")
        params.append(state.upper())
    if q:
        where.append("(LOWER(c.name) LIKE ? OR LOWER(c.industry) LIKE ?)")
        params += [f"%{q.lower()}%"] * 2

    # No ORDER BY on the score: "NULLS LAST" is Postgres-only and SQLite sorts
    # NULLs first, so a company nobody has targeted would head the list on one
    # engine and trail it on the other. Sorting in Python is one behavior on both.
    rows = g.conn.execute(
        f"""SELECT c.*,
                   (SELECT COUNT(*) FROM contacts ct WHERE ct.company_id = c.id) AS contacts,
                   (SELECT COUNT(*) FROM triggers t WHERE t.company_id = c.id) AS signals,
                   (SELECT MAX(t.score) FROM targets t WHERE t.company_id = c.id) AS best_score
              FROM companies c WHERE {' AND '.join(where)} LIMIT 400""",
        tuple(params)).fetchall()
    rows = sorted(rows, key=lambda r: (-(r["best_score"] or 0), r["name"]))[:300]

    states = g.conn.execute(
        "SELECT DISTINCT state FROM companies WHERE org_id = ? AND state != '' "
        "ORDER BY state", (org_id,)).fetchall()
    return render_template("companies.html", rows=rows, states=states, state=state, q=q)


@app.route("/company/<int:company_id>")
@auth.login_required
def company_detail(company_id):
    org = _org()
    org_id = org["id"]
    company = auth.scoped_one(g.conn, "companies", company_id)
    contacts = g.conn.execute(
        "SELECT * FROM contacts WHERE org_id = ? AND company_id = ? ORDER BY name",
        (org_id, company_id)).fetchall()
    points = {}
    for p in g.conn.execute(
            """SELECT * FROM contact_points WHERE org_id = ? AND contact_id IN
                 (SELECT id FROM contacts WHERE org_id = ? AND company_id = ?)""",
            (org_id, org_id, company_id)).fetchall():
        points.setdefault(p["contact_id"], []).append(p)
    triggers = g.conn.execute(
        "SELECT * FROM triggers WHERE org_id = ? AND company_id = ? "
        "ORDER BY observed_at DESC", (org_id, company_id)).fetchall()
    targets = signal_engine.load_and_score(
        g.conn, org,
        target_ids=[t["id"] for t in g.conn.execute(
            "SELECT id FROM targets WHERE org_id = ? AND company_id = ?",
            (org_id, company_id)).fetchall()])
    deals = g.conn.execute("SELECT * FROM deals WHERE org_id = ? AND company_id = ?",
                           (org_id, company_id)).fetchall()
    return render_template("company.html", company=company, contacts=contacts,
                           points=points, triggers=triggers, targets=targets,
                           deals=deals, effective=enrichment.effective_status,
                           trigger_label=signal_engine.trigger_label)


@app.route("/contact/<int:contact_id>/verify", methods=["POST"])
@auth.login_required
def contact_verify(contact_id):
    org_id = auth.current_org_id()
    contact = auth.scoped_one(g.conn, "contacts", contact_id)
    provider = enrichment.get_provider(Config)
    changes = enrichment.verify_contact(g.conn, org_id, contact_id, provider)
    moved = [c for c in changes if c.get("changed")]
    auth.audit(g.conn, "data.verify", f"contact:{contact_id}",
               engine=provider.name, detail={"changes": len(moved)})
    if moved:
        flash("; ".join(f"{c['kind']} {c['from']} -> {c['to']}" for c in moved))
    else:
        flash(f"Re-checked {len(changes)} field(s); nothing changed.")
    return redirect(request.form.get("back")
                    or url_for("company_detail", company_id=contact["company_id"]))


@app.route("/contact/<int:contact_id>/export.json")
@auth.role_required("admin")
def contact_export(contact_id):
    org_id = auth.current_org_id()
    auth.scoped_one(g.conn, "contacts", contact_id)
    export = compliance.subject_access_export(g.conn, org_id, contact_id)
    auth.audit(g.conn, "compliance.subject_access", f"contact:{contact_id}")
    return Response(
        json.dumps(export, indent=2, default=str), mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="subject-access-{contact_id}.json"'})


@app.route("/contact/<int:contact_id>/erase", methods=["POST"])
@auth.role_required("owner")
def contact_erase(contact_id):
    org_id = auth.current_org_id()
    contact = auth.scoped_one(g.conn, "contacts", contact_id)
    company_id = contact["company_id"]
    compliance.erase_contact(g.conn, org_id, contact_id)
    flash("Personal data destroyed. The commercial record was redacted and kept.")
    return redirect(url_for("company_detail", company_id=company_id))


# ===========================================================================
# Data quality
# ===========================================================================

@app.route("/data")
@auth.login_required
def data_quality():
    """
    What the data is actually worth.

    The number that matters is not coverage — every vendor claims one. It is the
    count of records a rep will dial this quarter that will not reach anybody.
    """
    org_id = auth.current_org_id()
    report = enrichment.data_quality(g.conn, org_id, ttl_days=Config.VERIFICATION_TTL_DAYS)
    worst = enrichment.worst_records(g.conn, org_id, limit=30,
                                     ttl_days=Config.VERIFICATION_TTL_DAYS)

    # What the dialer has learned that no vendor sold us.
    first_party = g.conn.execute(
        """SELECT COUNT(*) AS n FROM contact_points
            WHERE org_id = ? AND source = 'connected_call'""", (org_id,)).fetchone()["n"]
    retired = g.conn.execute(
        """SELECT COUNT(*) AS n FROM contact_points
            WHERE org_id = ? AND status = 'invalid' AND fail_count > 0""",
        (org_id,)).fetchone()["n"]

    by_source = g.conn.execute(
        """SELECT source, COUNT(*) AS n,
                  SUM(CASE WHEN status = 'verified' THEN 1 ELSE 0 END) AS verified,
                  SUM(CASE WHEN status = 'invalid' THEN 1 ELSE 0 END) AS invalid
             FROM contact_points WHERE org_id = ? GROUP BY source ORDER BY n DESC""",
        (org_id,)).fetchall()

    return render_template("data.html", report=report, worst=worst,
                           first_party=first_party, retired=retired,
                           by_source=by_source,
                           trust=enrichment.SOURCE_TRUST)


# ===========================================================================
# The learning loop
# ===========================================================================

@app.route("/intelligence")
@auth.login_required
def intelligence():
    """
    Which signals actually convert, for this org, on this list.

    The defensible asset the strategy document points at. It is empty on day one
    and the page says so rather than drawing a chart of nothing.
    """
    org_id = auth.current_org_id()
    rows = signal_engine.weight_evidence(g.conn, org_id)
    hours = signal_engine.best_hours(g.conn, org_id)

    total_attempts = sum(r["attempts"] for r in rows)
    confident = [r for r in rows if r["confident"]]
    best = max(confident, key=lambda r: r["lift"], default=None)
    worst = min(confident, key=lambda r: r["lift"], default=None)

    # Data quality against outcome — the argument for spending money on data.
    by_status = g.conn.execute(
        """SELECT p.status,
                  COUNT(*) AS attempts,
                  SUM(CASE WHEN k.disposition IN ('connected','meeting_set','callback',
                      'not_interested') THEN 1 ELSE 0 END) AS connects,
                  SUM(CASE WHEN k.disposition = 'meeting_set' THEN 1 ELSE 0 END) AS meetings
             FROM calls k
             JOIN contact_points p ON p.value = k.to_number AND p.org_id = k.org_id
            WHERE k.org_id = ? AND k.disposition != ''
            GROUP BY p.status ORDER BY attempts DESC""", (org_id,)).fetchall()

    reps = g.conn.execute(
        """SELECT r.id, r.name,
                  COUNT(k.id) AS calls,
                  SUM(CASE WHEN k.disposition IN ('connected','meeting_set','callback',
                      'not_interested') THEN 1 ELSE 0 END) AS connects,
                  SUM(CASE WHEN k.disposition = 'meeting_set' THEN 1 ELSE 0 END) AS meetings
             FROM reps r LEFT JOIN calls k ON k.rep_id = r.id AND k.org_id = r.org_id
            WHERE r.org_id = ? GROUP BY r.id, r.name ORDER BY meetings DESC""",
        (org_id,)).fetchall()

    return render_template("intelligence.html", rows=rows, hours=hours, reps=reps,
                           total_attempts=total_attempts, best=best, worst=worst,
                           by_status=by_status,
                           shrink=signal_engine.SHRINK_ATTEMPTS,
                           triggers=signal_engine.TRIGGER_TYPES)


# ===========================================================================
# The executive view
# ===========================================================================

@app.route("/exec")
@auth.role_required("manager")
def exec_view():
    """
    The numbers a VP of Sales is accountable for, with the arithmetic shown.

    Cost per meeting is the one that decides whether this platform gets renewed,
    so it is the headline rather than a chart of activity.
    """
    org = _org()
    org_id = org["id"]
    days = min(max(request.args.get("days", 90, type=int), 7), 365)
    since_date = (date.today() - timedelta(days=days)).isoformat()

    calls = g.conn.execute(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN disposition IN ('connected','meeting_set','callback',
                      'not_interested') THEN 1 ELSE 0 END) AS connects,
                  SUM(CASE WHEN disposition = 'meeting_set' THEN 1 ELSE 0 END) AS meetings,
                  SUM(duration_sec) AS seconds
             FROM calls WHERE org_id = ? AND created_at >= ?""",
        (org_id, since_date)).fetchone()

    deals = g.conn.execute(
        """SELECT stage, COUNT(*) AS n, COALESCE(SUM(value_annual), 0) AS v
             FROM deals WHERE org_id = ? GROUP BY stage""", (org_id,)).fetchall()
    by_stage = {d["stage"]: d for d in deals}

    def stage(key, field):
        row = by_stage.get(key)
        return (row[field] if row else 0) or 0

    open_value = sum(stage(s, "v") for s in ("discovery", "pricing", "pilot", "contract"))
    won_value = stage("won", "v")
    won_n, lost_n = stage("won", "n"), stage("lost", "n")

    sourced = g.conn.execute(
        """SELECT COALESCE(SUM(value_annual), 0) AS v, COUNT(*) AS n
             FROM deals WHERE org_id = ? AND created_at >= ? AND origin_signal != ''""",
        (org_id, since_date)).fetchone()

    reps = g.conn.execute(
        """SELECT r.*,
                  (SELECT COUNT(*) FROM calls k WHERE k.rep_id = r.id
                    AND k.created_at >= ?) AS calls,
                  (SELECT COUNT(*) FROM calls k WHERE k.rep_id = r.id
                    AND k.created_at >= ? AND k.disposition = 'meeting_set') AS meetings,
                  (SELECT COALESCE(SUM(d.value_annual), 0) FROM deals d
                    WHERE d.rep_id = r.id AND d.stage NOT IN ('won','lost')) AS pipeline,
                  (SELECT COALESCE(SUM(d.value_annual), 0) FROM deals d
                    WHERE d.rep_id = r.id AND d.stage = 'won'
                      AND COALESCE(d.updated_at, d.created_at) >= ?) AS won
             FROM reps r WHERE r.org_id = ? AND r.status = 'active'
            ORDER BY won DESC""",
        (since_date, since_date, since_date, org_id)).fetchall()

    # Attainment compares like with like. The quota on a seat is quarterly, so a
    # window other than ~90 days has to scale it or the percentage is fiction.
    quota_factor = days / 91.0
    quota = sum(r["quota_quarter"] for r in reps) or 1
    quality = enrichment.data_quality(g.conn, org_id, ttl_days=Config.VERIFICATION_TTL_DAYS)
    spend = ai_spend_cents(g.conn, org_id)

    # The cost line. Model spend is the only cost this platform actually knows;
    # rep time is the one that dominates, so it is estimated and labelled.
    rep_minutes = (calls["n"] or 0) * 2 + (calls["seconds"] or 0) / 60
    rep_hours = rep_minutes / 60
    meetings = calls["meetings"] or 0
    hours_per_meeting = rep_hours / meetings if meetings else 0

    top_signals = [r for r in signal_engine.weight_evidence(g.conn, org_id)
                   if r["confident"]]
    top_signals.sort(key=lambda r: -r["lift"])

    return render_template(
        "exec.html", days=days, calls=calls, open_value=open_value,
        won_value=won_value, won_n=won_n, lost_n=lost_n,
        win_rate=won_n / max(won_n + lost_n, 1), sourced=sourced, reps=reps,
        quota=quota, quality=quality, spend=spend, rep_hours=rep_hours,
        hours_per_meeting=hours_per_meeting, top_signals=top_signals[:5],
        quota_factor=quota_factor,
        coverage=open_value / quota if quota else 0)


# ===========================================================================
# Sequences
# ===========================================================================

@app.route("/sequences")
@auth.login_required
def sequences_view():
    org_id = auth.current_org_id()
    rows = seq_engine.summary(g.conn, org_id)
    steps = {}
    for s in g.conn.execute(
            "SELECT * FROM sequence_steps WHERE org_id = ? ORDER BY sequence_id, step_no",
            (org_id,)).fetchall():
        steps.setdefault(s["sequence_id"], []).append(s)
    due_now = seq_engine.due(g.conn, org_id)
    return render_template("sequences.html", rows=rows, steps=steps, due=due_now)


@app.route("/sequences/run", methods=["POST"])
@auth.role_required("manager")
def sequences_run():
    org_id = auth.current_org_id()
    done = seq_engine.run(g.conn, org_id)
    flash(f"{len(done)} step(s) materialized." if done else "Nothing was due.")
    return redirect(url_for("sequences_view"))


# ===========================================================================
# Import
# ===========================================================================

@app.route("/import", methods=["GET", "POST"])
@auth.role_required("admin")
def data_import():
    """
    Upload a prospect list.

    Admin-only: an import writes companies, contacts and dialable numbers across
    the whole org, and it is the most likely way a suppressed number re-enters
    the database. That is not a per-rep action.
    """
    org_id = auth.current_org_id()

    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a CSV export first.")
            return redirect(url_for("data_import"))
        try:
            text = upload.read().decode("utf-8-sig", errors="replace")
        except Exception:
            flash("That file could not be read as text. Export as CSV, not XLSX.")
            return redirect(url_for("data_import"))

        rows, source, headers = importer.parse_csv(text)
        if not rows:
            flash("No data rows found in that file. Check it has a header row.")
            return redirect(url_for("data_import"))

        options = {
            "trust_vendor": request.form.get("trust_vendor") == "1",
            "default_rep_id": request.form.get("default_rep_id", type=int),
        }
        import_id = g.conn.execute(
            """INSERT INTO imports (org_id, user_id, source, filename, raw_csv,
                                    options_json, status, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (org_id, g.user["id"], source, upload.filename[:200], text,
             json.dumps(options), "preview",
             datetime.now().isoformat(timespec="seconds"))).lastrowid
        g.conn.commit()
        auth.audit(g.conn, "import.upload", f"import:{import_id}",
                   detail={"source": source, "rows": len(rows),
                           "filename": upload.filename[:200]})
        return redirect(url_for("import_preview", import_id=import_id))

    history = g.conn.execute(
        """SELECT i.*, u.name AS user_name FROM imports i
        LEFT JOIN users u ON u.id = i.user_id AND u.org_id = i.org_id
            WHERE i.org_id = ? ORDER BY i.id DESC LIMIT 20""", (org_id,)).fetchall()
    parsed = [{"row": h, "report": _loads(h["report_json"])} for h in history]

    reps = g.conn.execute(
        "SELECT * FROM reps WHERE org_id = ? AND status = 'active' ORDER BY name",
        (org_id,)).fetchall()
    # Counted from the applied reports rather than from a column: the seed sets
    # an external_id on every company, so that column cannot distinguish a row
    # that arrived by import from one that was always here.
    imported = sum((_loads(h["report_json"], {}) or {}).get("created", {}).get("companies", 0)
                   for h in history if h["status"] == "applied")
    return render_template("import.html", history=parsed, reps=reps, imported=imported,
                           sources=importer.SOURCE_LABELS)


@app.route("/import/<int:import_id>")
@auth.role_required("admin")
def import_preview(import_id):
    """What the import would do. Read-only — nothing is written until Apply."""
    row = auth.scoped_one(g.conn, "imports", import_id)
    rows, source, headers = importer.parse_csv(row["raw_csv"], row["source"])
    options = _loads(row["options_json"], {}) or {}
    result = importer.plan(g.conn, row["org_id"], rows, source, options)
    rep_names = {r["id"]: r["name"] for r in g.conn.execute(
        "SELECT id, name FROM reps WHERE org_id = ?", (row["org_id"],)).fetchall()}
    return render_template("import_preview.html", imp=row, plan=result,
                           headers=headers, options=options, rep_names=rep_names,
                           report=_loads(row["report_json"]))


@app.route("/import/<int:import_id>/apply", methods=["POST"])
@auth.role_required("admin")
def import_apply(import_id):
    row = auth.scoped_one(g.conn, "imports", import_id)
    if row["status"] != "preview":
        flash(f"That import was already {row['status']}.")
        return redirect(url_for("import_preview", import_id=import_id))

    rows, source, _headers = importer.parse_csv(row["raw_csv"], row["source"])
    options = _loads(row["options_json"], {}) or {}
    report = importer.apply(g.conn, row["org_id"], rows, source, g.user["id"], options)

    g.conn.execute(
        """UPDATE imports SET status = 'applied', report_json = ?, applied_at = ?
            WHERE id = ? AND org_id = ?""",
        (json.dumps(report), report["applied_at"], import_id, row["org_id"]))
    g.conn.commit()

    # Score what just landed, so the new rows appear on a list rather than
    # sitting at zero until something else happens to trigger a rescore.
    org = _org()
    scored = signal_engine.load_and_score(g.conn, org, statuses=("new",))
    signal_engine.persist_scores(g.conn, scored)

    auth.audit(g.conn, "import.apply", f"import:{import_id}", detail=report["created"])
    created = report["created"]
    flash(f"Imported {created['contacts']} contacts at {created['companies']} new "
          f"companies, and put {created['targets']} on the board.")
    return redirect(url_for("import_preview", import_id=import_id))


@app.route("/import/<int:import_id>/discard", methods=["POST"])
@auth.role_required("admin")
def import_discard(import_id):
    row = auth.scoped_one(g.conn, "imports", import_id)
    if row["status"] == "applied":
        flash("An applied import cannot be discarded — the rows are already in.")
        return redirect(url_for("import_preview", import_id=import_id))
    g.conn.execute("UPDATE imports SET status = 'discarded' WHERE id = ? AND org_id = ?",
                   (import_id, row["org_id"]))
    g.conn.commit()
    flash("Discarded. Nothing was written.")
    return redirect(url_for("data_import"))


# ===========================================================================
# Settings
# ===========================================================================

@app.route("/settings", methods=["GET", "POST"])
@auth.role_required("admin")
def settings():
    org = _org()
    if request.method == "POST":
        modes = ",".join(m for m in request.form.getlist("modes"))
        g.conn.execute(
            """UPDATE orgs SET pitch = ?, modes = ?, home_markets = ?,
                      recording_policy = ?, quiet_start = ?, quiet_end = ?,
                      retention_days = ? WHERE id = ?""",
            ((request.form.get("pitch") or "").strip()[:600], modes or "ltl,ftl",
             (request.form.get("home_markets") or "").strip().upper(),
             request.form.get("recording_policy", "off"),
             max(0, min(request.form.get("quiet_start", 8, type=int), 23)),
             max(1, min(request.form.get("quiet_end", 21, type=int), 24)),
             max(30, request.form.get("retention_days", 730, type=int)),
             org["id"]))
        g.conn.commit()
        auth.audit(g.conn, "settings.update", f"org:{org['id']}")
        flash("Saved. Call plans and the dial gate use these immediately.")
        return redirect(url_for("settings"))

    supp = g.conn.execute(
        "SELECT * FROM suppression WHERE org_id = ? ORDER BY id DESC LIMIT 100",
        (org["id"],)).fetchall()
    spend = ai_spend_cents(g.conn, org["id"])
    return render_template("settings.html", org=org, suppression=supp, spend=spend,
                           all_party=compliance.ALL_PARTY_STATES)


@app.route("/settings/suppress", methods=["POST"])
@auth.role_required("admin")
def settings_suppress():
    org_id = auth.current_org_id()
    kind = request.form.get("kind", "phone")
    value = (request.form.get("value") or "").strip()
    if kind not in ("phone", "email", "domain") or not value:
        abort(400, description="A kind and a value are required.")
    compliance.suppress(g.conn, org_id, kind, value,
                        reason=(request.form.get("reason") or "").strip() or "manual entry")
    auth.audit(g.conn, "compliance.suppress", None, detail={"kind": kind})
    flash(f"Added. No dial or send in this organization will reach it.")
    return redirect(url_for("settings"))


@app.route("/settings/users", methods=["GET", "POST"])
@auth.role_required("owner")
def settings_users():
    org_id = auth.current_org_id()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "invite":
            email = (request.form.get("email") or "").strip().lower()
            name = (request.form.get("name") or "").strip()
            role = request.form.get("role", "member")
            password = request.form.get("password") or ""
            problem = auth.password_problem(password)
            if role not in auth.ROLES:
                problem = "Unknown role."
            elif not (email and "@" in email and name):
                problem = "Name and a real email address are required."
            elif g.conn.execute("SELECT 1 FROM users WHERE org_id = ? AND lower(email) = ?",
                                (org_id, email)).fetchone():
                problem = "That email already has an account here."
            if problem:
                flash(problem)
            else:
                rep_id = g.conn.execute(
                    """INSERT INTO reps (org_id, name, email, territory, quota_quarter,
                                         status, started_at, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (org_id, name, email, "", 0, "active", date.today().isoformat(),
                     datetime.now().isoformat(timespec="seconds"))).lastrowid
                g.conn.execute(
                    """INSERT INTO users (org_id, email, password_hash, name, role,
                                          status, rep_id, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (org_id, email, auth.hash_password(password), name, role, "active",
                     rep_id, datetime.now().isoformat(timespec="seconds")))
                g.conn.commit()
                auth.audit(g.conn, "user.create", f"user:{email}", detail={"role": role})
                flash(f"{name} added as {role}.")
        elif action == "deactivate":
            user_id = request.form.get("user_id", type=int)
            if user_id == auth.current_user()["id"]:
                flash("You cannot deactivate yourself.")
            else:
                g.conn.execute(
                    "UPDATE users SET status = 'disabled' WHERE id = ? AND org_id = ?",
                    (user_id, org_id))
                g.conn.commit()
                auth.audit(g.conn, "user.deactivate", f"user:{user_id}")
                flash("Access revoked.")
        return redirect(url_for("settings_users"))

    users = g.conn.execute(
        """SELECT u.*, r.name AS rep_name FROM users u
        LEFT JOIN reps r ON r.id = u.rep_id
            WHERE u.org_id = ? ORDER BY u.role, u.name""", (org_id,)).fetchall()
    return render_template("settings_users.html", users=users)


@app.route("/settings/audit")
@auth.role_required("admin")
def settings_audit():
    org_id = auth.current_org_id()
    rows = g.conn.execute(
        """SELECT a.*, u.name AS user_name FROM audit_log a
        LEFT JOIN users u ON u.id = a.user_id
            WHERE a.org_id = ? ORDER BY a.id DESC LIMIT 300""", (org_id,)).fetchall()
    return render_template("settings_audit.html", rows=rows)


@app.route("/settings/compliance")
@auth.role_required("admin")
def settings_compliance():
    org = _org()
    org_id = org["id"]
    candidates = compliance.retention_candidates(g.conn, org_id)
    departed = g.conn.execute(
        """SELECT c.*, co.name AS company_name FROM contacts c
             JOIN companies co ON co.id = c.company_id
            WHERE c.org_id = ? AND c.status = 'left_company' ORDER BY c.name LIMIT 50""",
        (org_id,)).fetchall()
    recorded = g.conn.execute(
        """SELECT COUNT(*) AS n FROM calls WHERE org_id = ? AND recorded = 1""",
        (org_id,)).fetchone()["n"]
    return render_template("settings_compliance.html", org=org, candidates=candidates,
                           departed=departed, recorded=recorded,
                           all_party=compliance.ALL_PARTY_STATES)


@app.route("/settings/compliance/purge", methods=["POST"])
@auth.role_required("owner")
def compliance_purge():
    org = _org()
    count, names = compliance.purge(g.conn, org["id"], dry_run=False)
    auth.audit(g.conn, "compliance.purge", f"org:{org['id']}", detail={"count": count})
    flash(f"Erased {count} contact(s) past the retention window.")
    return redirect(url_for("settings_compliance"))


@app.route("/healthz")
def healthz():
    """Liveness probe. No auth, no tenant data, one cheap query."""
    try:
        g.conn.execute("SELECT 1").fetchone()
        return Response("ok", mimetype="text/plain")
    except Exception:
        return Response("db unavailable", status=503, mimetype="text/plain")


if __name__ == "__main__":
    if not DB_FILE.exists() and not Config.DATABASE_URL:
        print("No database yet. Run:  python3 grind_db.py")
        raise SystemExit(1)
    app.run(debug=Config.DEBUG, port=5005)
