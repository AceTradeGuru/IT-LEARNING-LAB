"""
QuotaOS — sales org operating system.

Monitor the floor, certify the bench, and hire the next seat — in one app.

Run locally:
    python3 quota_db.py       # build and seed
    python3 app.py            # http://localhost:5003

Run in production (see DEPLOY.md):
    QUOTAOS_ENV=production SECRET_KEY=... gunicorn -w 4 -b 0.0.0.0:8000 app:app

Two invariants hold everywhere in this file:

  1. **Every route is gated.** `@login_required` at minimum; `@role_required(...)`
     where the action is privileged. There is no anonymous surface beyond the login
     and signup pages.

  2. **Every query filters on org_id.** Multi-tenant isolation is enforced in the
     WHERE clause of each individual query, not by a middleware someone can forget
     to apply. Row-level access below that (a manager sees their team, a rep sees
     themselves) comes from `auth.visible_rep_ids`.
"""

import json
import re
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
import importer
from config import Config, validate
from health_engine import (evaluate_org, evaluate_rep, pipeline_by_stage,
                           quarter_bounds, quarter_label, recommended_courses)
from quota_db import DB_FILE, create_tables, get_connection, seed_sample_data

app = Flask(__name__)
app.config.from_object(Config)

_problems = validate(Config)
if _problems:
    for p in _problems:
        print(f"FATAL CONFIG: {p}", file=sys.stderr)
    raise SystemExit(1)

app.secret_key = Config.SECRET_KEY
app.permanent_session_lifetime = timedelta(seconds=Config.PERMANENT_SESSION_LIFETIME)

# The hiring funnel, in order. Everything about stage movement reads from here.
STAGES = ["sourced", "screened", "ai_interview", "panel", "offer", "hired"]
STAGE_LABELS = {
    "sourced": "Sourced",
    "screened": "Screened",
    "ai_interview": "AI interview",
    "panel": "Panel",
    "offer": "Offer",
    "hired": "Hired",
    "rejected": "Rejected",
}

# ---------------------------------------------------------------- AI budget --
#
# Two independent guards, because they stop different failure modes:
#
#   * A **per-hour call limit**, in process memory, to blunt a runaway loop before
#     it becomes money. Per-worker, so with four gunicorn workers the real ceiling
#     is 4x the configured number — a deliberate stopgap until this moves to Redis.
#
#   * A **per-month dollar cap per org**, in the database, from the actual token
#     usage the API reports. This one is authoritative and shared across workers,
#     which is why the spend ledger is a table and not a dict.
_ai_calls = defaultdict(list)


def _month_key(today=None):
    return (today or date.today()).strftime("%Y-%m")


def ai_spend_cents(conn, org_id, month=None):
    """What this org has spent on model calls in the given month, in cents."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cents), 0) AS c FROM ai_spend WHERE org_id = ? AND month = ?",
        (org_id, month or _month_key()),
    ).fetchone()
    return float(row["c"] or 0)


def ai_budget_check(conn, org_id, cap_cents):
    """
    Returns (ok, reason). Checked before the call, not after — the point is to
    not make the request, and a guard that reports overspend afterwards is a
    receipt, not a limit.
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


def ai_budget_record(conn, org_id):
    """
    Bank the cost of whatever model calls just happened.

    Offline-engine calls cost nothing and record nothing, so a tenant with no API
    key never accrues spend.
    """
    _ai_calls[org_id].append(time.time())
    cents = ai_engine.drain_cost_cents()
    if cents:
        conn.execute(
            "INSERT INTO ai_spend (org_id, month, cents, created_at) VALUES (?,?,?,?)",
            (org_id, _month_key(), cents, datetime.now().isoformat(timespec="seconds")),
        )
    return cents


# --------------------------------------------------------------- lifecycle --

@app.before_request
def load_session():
    """Attach the signed-in user to `g`, then guard every state change."""
    g.user = None
    uid = session.get(auth.SESSION_USER_KEY)
    if uid:
        conn = get_connection()
        g.user = auth.load_user(conn, uid)
        conn.close()
        # A user deactivated or an org suspended mid-session loses access now,
        # not whenever their cookie happens to expire.
        if g.user is None or g.user["org_status"] != "active":
            session.clear()
            g.user = None

    if request.method == "POST":
        auth.csrf_validate()


@app.context_processor
def inject_globals():
    return {
        "user": g.user,
        "engine": ai_engine.engine_status(),
        "stage_labels": STAGE_LABELS,
        "stages": STAGES,
        "today": date.today(),
        "csrf_token": auth.csrf_token,
        "has_role": auth.has_role,
    }


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, title="Not your data",
                           message="Your role doesn't grant access to this page. If you "
                                   "think it should, ask an admin in your organization."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, title="Not found",
                           message="No such record in your organization."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, title="Request rejected",
                           message=getattr(e, "description", "Malformed request.")), 400


# ---------------------------------------------------------------- filters --

@app.template_filter("money")
def money(value):
    """$1,240,000 -> $1.24M. Dashboards get unreadable past six digits."""
    if value is None:
        return "—"
    value = int(value)
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,}"


@app.template_filter("dollars")
def dollars(value):
    return "—" if value is None else f"${int(value):,}"


@app.template_filter("since")
def since(value):
    if not value:
        return "never"
    d = datetime.strptime(value[:10], "%Y-%m-%d").date()
    days = (date.today() - d).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 0:
        return f"in {-days}d"
    return f"{days}d ago"


@app.template_filter("markdown")
def markdown(text):
    """
    Render the small Markdown subset the posting generator emits.

    A full Markdown library would be a dependency for headings, bullets, and bold
    — that isn't a trade worth making for one template. Input is escaped first, so
    a posting can't inject markup.
    """
    if not text:
        return ""
    out, in_list = [], False
    for raw in text.splitlines():
        line = escape(raw.rstrip())
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        if line.startswith("- ") or line.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{line[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level = min(len(m.group(1)) + 1, 5)
            out.append(f"<h{level}>{m.group(2)}</h{level}>")
        elif re.match(r"^\d+\.\s", line):
            out.append(f"<p class='numbered'>{line}</p>")
        else:
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return Markup("\n".join(out))


@app.template_filter("nicedate")
def nicedate(value):
    if not value:
        return "—"
    return datetime.strptime(value[:10], "%Y-%m-%d").date().strftime("%b %-d, %Y")


# ------------------------------------------------------------------- auth --

@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        conn = get_connection()
        user = auth.load_user_by_email(conn, email)
        # One message for both "no such user" and "wrong password" — telling them
        # apart is a free account-enumeration oracle.
        if user is None or not auth.verify_password(user["password_hash"], password):
            conn.close()
            flash("Email or password is incorrect.")
            return render_template("login.html", email=email), 401
        auth.login_user(conn, user)
        conn.close()
        nxt = request.form.get("next") or url_for("dashboard")
        # Only ever redirect within this app — an open redirect is a phishing tool.
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = url_for("dashboard")
        return redirect(nxt)
    return render_template("login.html", email="")


@app.route("/logout", methods=["POST"])
def logout():
    auth.logout_user()
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Self-serve tenant creation. The first user of a new org is its owner."""
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        f = request.form
        org_name = f.get("org_name", "").strip()
        name = f.get("name", "").strip()
        email = f.get("email", "").strip().lower()
        password = f.get("password", "")
        slug = re.sub(r"[^a-z0-9]+", "-", org_name.lower()).strip("-") or "org"

        problem = auth.password_problem(password)
        if not org_name or not name or "@" not in email:
            problem = "Organization, name, and a valid email are all required."
        conn = get_connection()
        if problem is None and auth.load_user_by_email(conn, email):
            problem = "An account with that email already exists."
        if problem is None and conn.execute(
            "SELECT 1 FROM orgs WHERE slug = ?", (slug,)
        ).fetchone():
            slug = f"{slug}-{int(time.time()) % 100000}"
        if problem:
            conn.close()
            flash(problem)
            return render_template("signup.html", form=f), 400

        org_id, user_id = auth.create_org_with_owner(
            conn, org_name, slug, name, email, password)
        user = auth.load_user(conn, user_id)
        auth.login_user(conn, user)
        conn.close()
        flash(f"Welcome to QuotaOS. {org_name} is set up — start by adding your team.")
        return redirect(url_for("dashboard"))
    return render_template("signup.html", form={})


@app.route("/settings/users", methods=["GET", "POST"])
@auth.role_required("owner")
def settings_users():
    """Owner-only user management. Every SaaS tenant needs a second admin."""
    conn = get_connection()
    org_id = auth.current_org_id()
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        name = f.get("name", "").strip()
        role = f.get("role", "member")
        rep_id = f.get("rep_id") or None
        password = f.get("password", "")

        problem = auth.password_problem(password)
        if role not in auth.ROLES:
            problem = "Unknown role."
        if not name or "@" not in email:
            problem = "Name and a valid email are required."
        if problem is None and auth.load_user_by_email(conn, email):
            problem = "That email already has an account."
        # A rep_id from the form is user input: confirm it belongs to this org
        # before storing it, or a login could be bound to another tenant's seat.
        if rep_id:
            owned = conn.execute("SELECT 1 FROM reps WHERE id = ? AND org_id = ?",
                                 (rep_id, org_id)).fetchone()
            if not owned:
                problem = "That seat isn't in your organization."
        if problem:
            flash(problem)
        else:
            conn.execute(
                """INSERT INTO users (org_id, email, password_hash, name, role, rep_id,
                                      status, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (org_id, email, auth.hash_password(password), name, role, rep_id,
                 "active", date.today().isoformat()))
            auth.audit(conn, "user.create", entity=f"user:{email}", detail={"role": role})
            conn.commit()
            flash(f"Added {name} as {role}.")
        conn.close()
        return redirect(url_for("settings_users"))

    users = conn.execute(
        """SELECT u.*, r.name rep_name FROM users u
             LEFT JOIN reps r ON r.id = u.rep_id AND r.org_id = u.org_id
            WHERE u.org_id = ? ORDER BY u.role DESC, u.name""",
        (org_id,),
    ).fetchall()
    reps = conn.execute(
        "SELECT * FROM reps WHERE org_id = ? AND status = 'active' ORDER BY name",
        (org_id,),
    ).fetchall()
    conn.close()
    return render_template("settings_users.html", users=users, reps=reps, roles=auth.ROLES)


@app.route("/settings/audit")
@auth.role_required("admin")
def settings_audit():
    """
    The record of every AI-assisted decision, newest first.

    This exists because employment-decision regulation expects it to, and because
    "the model said 62" is not defensible without the input that produced it.
    """
    conn = get_connection()
    org_id = auth.current_org_id()
    rows = conn.execute(
        """SELECT a.*, u.name user_name FROM audit_log a
             LEFT JOIN users u ON u.id = a.user_id AND u.org_id = a.org_id
            WHERE a.org_id = ? ORDER BY a.created_at DESC, a.id DESC LIMIT 300""",
        (org_id,),
    ).fetchall()
    spent = ai_spend_cents(conn, org_id)
    cap = g.user["ai_cents_cap"]
    conn.close()
    return render_template("settings_audit.html", rows=rows, spent=spent, cap=cap,
                           month=_month_key())


# ------------------------------------------------------------------ floor --

def _scope(conn):
    """
    Resolve this user's view: (manager_id filter, visible rep id set).

    Admins and owners get (None, None) — the whole org. A manager is filtered to
    their own team. A member sees only themselves.
    """
    rep_ids = auth.visible_rep_ids(conn)
    user = g.user
    manager_id = user["rep_id"] if (user["role"] == "manager" and user["rep_id"]) else None
    return manager_id, rep_ids


@app.route("/")
@auth.login_required
def dashboard():
    conn = get_connection()
    org_id = auth.current_org_id()
    manager_id, rep_ids = _scope(conn)
    org = evaluate_org(conn, org_id, manager_id=manager_id, rep_ids=rep_ids)
    funnel = pipeline_by_stage(conn, org_id, manager_id=manager_id, rep_ids=rep_ids)
    peak = max((f["amount"] for f in funnel), default=1) or 1

    teams = []
    if auth.has_role("admin"):
        for m in conn.execute(
            "SELECT * FROM reps WHERE org_id = ? AND role = 'manager' ORDER BY name",
            (org_id,),
        ).fetchall():
            teams.append({"manager": m,
                          "org": evaluate_org(conn, org_id, manager_id=m["id"])})

    lapsed = conn.execute(
        """SELECT r.id, r.name, t.name track FROM certifications c
             JOIN reps r ON r.id = c.rep_id AND r.org_id = c.org_id
             JOIN cert_tracks t ON t.id = c.track_id AND t.org_id = c.org_id
            WHERE c.org_id = ? AND c.status = 'expired' AND t.is_mandatory = 1
              AND r.status = 'active'""",
        (org_id,),
    ).fetchall()
    if rep_ids is not None:
        lapsed = [l for l in lapsed if l["id"] in rep_ids]

    open_reqs = []
    if auth.has_role("manager"):
        open_reqs = conn.execute(
            """SELECT q.*, COUNT(a.id) applicants FROM reqs q
                 LEFT JOIN applications a ON a.req_id = q.id AND a.org_id = q.org_id
                WHERE q.org_id = ? AND q.status = 'open'
                GROUP BY q.id ORDER BY q.created_at DESC""",
            (org_id,),
        ).fetchall()
    conn.close()
    return render_template("dashboard.html", org=org, funnel=funnel, peak=peak,
                           teams=teams, lapsed=lapsed, open_reqs=open_reqs,
                           scoped=bool(manager_id))


@app.route("/org")
@auth.login_required
def org_view():
    conn = get_connection()
    org_id = auth.current_org_id()
    manager_id, rep_ids = _scope(conn)
    org = evaluate_org(conn, org_id, manager_id=manager_id, rep_ids=rep_ids)
    sort = request.args.get("sort", "score")
    keys = {
        "score": lambda e: e["score"],
        "attainment": lambda e: -e["metrics"]["attainment_pct"],
        "pipeline": lambda e: -e["metrics"]["pipeline_q"],
        "name": lambda e: e["rep"]["name"],
    }
    org["evals"].sort(key=keys.get(sort, keys["score"]))
    conn.close()
    return render_template("org.html", org=org, sort=sort)


@app.route("/rep/<int:rep_id>")
@auth.login_required
def rep_detail(rep_id):
    conn = get_connection()
    org_id = auth.current_org_id()
    rep = auth.scoped_one(conn, "reps", rep_id)
    auth.require_rep_visible(conn, rep_id)

    ev = evaluate_rep(conn, rep)
    courses = recommended_courses(conn, org_id, ev["signals"])
    manager = None
    if rep["manager_id"]:
        manager = conn.execute("SELECT * FROM reps WHERE id = ? AND org_id = ?",
                               (rep["manager_id"], org_id)).fetchone()
    certs = conn.execute(
        """SELECT c.*, t.name track_name, t.is_mandatory, t.valid_months
             FROM certifications c JOIN cert_tracks t ON t.id = c.track_id AND t.org_id = c.org_id
            WHERE c.org_id = ? AND c.rep_id = ? ORDER BY t.is_mandatory DESC, t.name""",
        (org_id, rep_id),
    ).fetchall()
    enrollments = conn.execute(
        """SELECT e.*, co.code, co.title, co.category, co.hours
             FROM enrollments e JOIN courses co ON co.id = e.course_id AND co.org_id = e.org_id
            WHERE e.org_id = ? AND e.rep_id = ? ORDER BY e.status, co.code""",
        (org_id, rep_id),
    ).fetchall()
    weeks = conn.execute(
        "SELECT * FROM activities WHERE org_id = ? AND rep_id = ? "
        "ORDER BY week_start DESC LIMIT 8",
        (org_id, rep_id),
    ).fetchall()
    peak_week = max((w["meetings"] + w["demos"] for w in weeks), default=1) or 1
    conn.close()
    return render_template("rep.html", rep=rep, ev=ev, m=ev["metrics"], manager=manager,
                           courses=courses, certs=certs, enrollments=enrollments,
                           weeks=list(reversed(weeks)), peak_week=peak_week)


@app.route("/rep/<int:rep_id>/assign/<int:course_id>", methods=["POST"])
@auth.role_required("manager")
def assign_course(rep_id, course_id):
    """Assign remediation training straight off a health signal."""
    conn = get_connection()
    org_id = auth.current_org_id()
    rep = auth.scoped_one(conn, "reps", rep_id)
    course = auth.scoped_one(conn, "courses", course_id)
    auth.require_rep_visible(conn, rep_id)

    conn.execute(
        """INSERT OR IGNORE INTO enrollments
           (org_id, rep_id, course_id, status, assigned_on, assigned_reason)
           VALUES (?,?,?,?,?,?)""",
        (org_id, rep_id, course_id, "not_started", date.today().isoformat(),
         request.form.get("reason", "Assigned from health signal")),
    )
    auth.audit(conn, "training.assign", entity=f"rep:{rep_id}",
               detail={"course": course["code"]})
    conn.commit()
    conn.close()
    flash(f"Assigned {course['code']} — {course['title']} to {rep['name']}.")
    return redirect(url_for("rep_detail", rep_id=rep_id))


@app.route("/pipeline")
@auth.login_required
def pipeline():
    conn = get_connection()
    org_id = auth.current_org_id()
    manager_id, rep_ids = _scope(conn)

    where = "WHERE d.org_id = ? AND d.stage NOT IN ('closed_won','closed_lost')"
    params = [org_id]
    if manager_id:
        where += " AND r.manager_id = ?"
        params.append(manager_id)
    if rep_ids is not None:
        if not rep_ids:
            conn.close()
            return render_template("pipeline.html", deals=[], reps=[],
                                   q_end=quarter_bounds()[1], selected="",
                                   quarter=quarter_label())
        where += f" AND d.rep_id IN ({','.join('?' * len(rep_ids))})"
        params.extend(rep_ids)
    if request.args.get("rep", "").isdigit():
        where += " AND d.rep_id = ?"
        params.append(int(request.args["rep"]))

    deals = conn.execute(
        f"""SELECT d.*, r.name rep_name, r.id rep_id, r.segment
              FROM deals d JOIN reps r ON r.id = d.rep_id AND r.org_id = d.org_id
              {where} ORDER BY d.close_date""",
        tuple(params),
    ).fetchall()

    rep_where = "WHERE org_id = ? AND role = 'rep' AND status = 'active'"
    rep_params = [org_id]
    if rep_ids is not None:
        rep_where += f" AND id IN ({','.join('?' * len(rep_ids))})"
        rep_params.extend(rep_ids)
    reps = conn.execute(f"SELECT * FROM reps {rep_where} ORDER BY name",
                        tuple(rep_params)).fetchall()
    _, q_end, _ = quarter_bounds()
    conn.close()
    return render_template("pipeline.html", deals=deals, reps=reps, q_end=q_end,
                           selected=request.args.get("rep", ""), quarter=quarter_label())


# ---------------------------------------------------------------- accounts --

CONTACT_ROLES = ["champion", "economic_buyer", "technical", "influencer", "blocker", "user"]


@app.route("/accounts")
@auth.login_required
def accounts():
    """
    Companies you sell to, with contact coverage front and centre.

    Coverage is the column that matters: an account with open pipeline and one
    known contact is the one to fix this week.
    """
    conn = get_connection()
    org_id = auth.current_org_id()
    _, rep_ids = _scope(conn)

    where, params = "WHERE a.org_id = ?", [org_id]
    if rep_ids is not None:
        if not rep_ids:
            conn.close()
            return render_template("accounts.html", accounts=[], q="")
        where += f" AND a.owner_rep_id IN ({','.join('?' * len(rep_ids))})"
        params.extend(rep_ids)
    q = request.args.get("q", "").strip()
    if q:
        where += " AND lower(a.name) LIKE ?"
        params.append(f"%{q.lower()}%")

    rows = conn.execute(
        f"""SELECT a.*, r.name owner_name,
                   (SELECT COUNT(*) FROM contacts c
                     WHERE c.org_id = a.org_id AND c.account_id = a.id) contacts,
                   (SELECT COUNT(*) FROM deals d
                     WHERE d.org_id = a.org_id AND d.account_id = a.id
                       AND d.stage NOT IN ('closed_won','closed_lost')) open_deals,
                   (SELECT COALESCE(SUM(d.amount),0) FROM deals d
                     WHERE d.org_id = a.org_id AND d.account_id = a.id
                       AND d.stage NOT IN ('closed_won','closed_lost')) open_value
              FROM accounts a
              LEFT JOIN reps r ON r.id = a.owner_rep_id AND r.org_id = a.org_id
              {where}
             ORDER BY open_value DESC, a.name LIMIT 300""",
        tuple(params),
    ).fetchall()
    conn.close()
    return render_template("accounts.html", accounts=rows, q=q)


@app.route("/account/<int:account_id>", methods=["GET", "POST"])
@auth.login_required
def account_detail(account_id):
    conn = get_connection()
    org_id = auth.current_org_id()
    acct = auth.scoped_one(conn, "accounts", account_id)

    if request.method == "POST":
        if not auth.has_role("manager"):
            conn.close()
            abort(403)
        f = request.form
        role = f.get("role_type", "influencer")
        if role not in CONTACT_ROLES:
            conn.close()
            abort(400, description="Unknown contact role.")
        if not f.get("name", "").strip():
            conn.close()
            flash("A contact needs a name.")
            return redirect(url_for("account_detail", account_id=account_id))
        conn.execute(
            """INSERT INTO contacts (org_id, account_id, name, title, email, phone,
                                     role_type, is_primary, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (org_id, account_id, f["name"].strip(), f.get("title", "").strip(),
             f.get("email", "").strip(), f.get("phone", "").strip(), role,
             1 if f.get("is_primary") else 0, date.today().isoformat()))
        conn.commit()
        conn.close()
        flash(f"Added {f['name'].strip()} to {acct['name']}.")
        return redirect(url_for("account_detail", account_id=account_id))

    contacts = conn.execute(
        """SELECT * FROM contacts WHERE org_id = ? AND account_id = ?
            ORDER BY is_primary DESC, role_type, name""",
        (org_id, account_id)).fetchall()
    deals = conn.execute(
        """SELECT d.*, r.name rep_name FROM deals d
             JOIN reps r ON r.id = d.rep_id AND r.org_id = d.org_id
            WHERE d.org_id = ? AND d.account_id = ? ORDER BY d.close_date DESC""",
        (org_id, account_id)).fetchall()
    owner = None
    if acct["owner_rep_id"]:
        owner = conn.execute("SELECT * FROM reps WHERE id = ? AND org_id = ?",
                             (acct["owner_rep_id"], org_id)).fetchone()
    conn.close()
    open_deals = [d for d in deals if d["stage"] not in ("closed_won", "closed_lost")]
    return render_template("account.html", a=acct, contacts=contacts, deals=deals,
                           open_deals=open_deals, owner=owner, roles=CONTACT_ROLES)


@app.route("/accounts/new", methods=["GET", "POST"])
@auth.role_required("manager")
def account_new():
    conn = get_connection()
    org_id = auth.current_org_id()
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        if not name:
            conn.close()
            flash("An account needs a name.")
            return redirect(url_for("account_new"))
        dupe = conn.execute("SELECT id FROM accounts WHERE org_id = ? AND lower(name) = ?",
                            (org_id, name.lower())).fetchone()
        if dupe:
            conn.close()
            flash(f"{name} already exists.")
            return redirect(url_for("account_detail", account_id=dupe["id"]))
        owner = f.get("owner_rep_id") or None
        if owner and not conn.execute("SELECT 1 FROM reps WHERE id = ? AND org_id = ?",
                                      (owner, org_id)).fetchone():
            conn.close()
            abort(403)
        aid = conn.execute(
            """INSERT INTO accounts (org_id, name, domain, industry, segment, employees,
                                     website, owner_rep_id, notes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (org_id, name, f.get("domain", "").strip(), f.get("industry", "").strip(),
             f.get("segment", "Mid-Market"),
             int(f["employees"]) if (f.get("employees") or "").isdigit() else None,
             f.get("website", "").strip(), owner, f.get("notes", "").strip(),
             date.today().isoformat())).lastrowid
        conn.commit()
        conn.close()
        flash(f"Created {name}.")
        return redirect(url_for("account_detail", account_id=aid))
    reps = conn.execute(
        "SELECT * FROM reps WHERE org_id = ? AND status = 'active' ORDER BY name",
        (org_id,)).fetchall()
    conn.close()
    return render_template("account_new.html", reps=reps)


@app.route("/contact/<int:contact_id>/delete", methods=["POST"])
@auth.role_required("manager")
def contact_delete(contact_id):
    conn = get_connection()
    org_id = auth.current_org_id()
    row = conn.execute("SELECT * FROM contacts WHERE id = ? AND org_id = ?",
                       (contact_id, org_id)).fetchone()
    if row is None:
        conn.close()
        abort(404)
    conn.execute("DELETE FROM contacts WHERE id = ? AND org_id = ?", (contact_id, org_id))
    conn.commit()
    conn.close()
    flash(f"Removed {row['name']}.")
    return redirect(url_for("account_detail", account_id=row["account_id"]))


# --------------------------------------------------------------- training --

@app.route("/training")
@auth.login_required
def training():
    conn = get_connection()
    org_id = auth.current_org_id()
    courses = conn.execute(
        "SELECT * FROM courses WHERE org_id = ? ORDER BY category, code", (org_id,)
    ).fetchall()
    tracks = conn.execute(
        "SELECT * FROM cert_tracks WHERE org_id = ? ORDER BY is_mandatory DESC, name",
        (org_id,),
    ).fetchall()

    headcount = conn.execute(
        "SELECT COUNT(*) n FROM reps WHERE org_id = ? AND role = 'rep' AND status = 'active'",
        (org_id,),
    ).fetchone()["n"]

    track_rows = []
    for t in tracks:
        # A track scoped to one segment is measured against that segment only.
        # Dividing an Enterprise-only track by the whole org reports a compliance
        # hole that doesn't exist.
        if t["required_for"] in ("all", "rep"):
            eligible = headcount
        else:
            eligible = conn.execute(
                "SELECT COUNT(*) n FROM reps WHERE org_id = ? AND role = 'rep' "
                "AND status = 'active' AND segment = ?", (org_id, t["required_for"]),
            ).fetchone()["n"]

        stats = conn.execute(
            """SELECT
                 SUM(CASE WHEN c.status='certified'   THEN 1 ELSE 0 END) certified,
                 SUM(CASE WHEN c.status='expired'     THEN 1 ELSE 0 END) expired,
                 SUM(CASE WHEN c.status='in_progress' THEN 1 ELSE 0 END) in_progress
               FROM certifications c JOIN reps r ON r.id = c.rep_id AND r.org_id = c.org_id
              WHERE c.org_id = ? AND c.track_id = ? AND r.role = 'rep'
                AND r.status = 'active'""",
            (org_id, t["id"]),
        ).fetchone()
        certified = stats["certified"] or 0
        courses_in = conn.execute(
            """SELECT co.* FROM track_courses tc
                 JOIN courses co ON co.id = tc.course_id AND co.org_id = tc.org_id
                WHERE tc.org_id = ? AND tc.track_id = ? ORDER BY tc.sequence""",
            (org_id, t["id"]),
        ).fetchall()
        track_rows.append({
            "track": t, "courses": courses_in,
            "certified": certified,
            "expired": stats["expired"] or 0,
            "in_progress": stats["in_progress"] or 0,
            "eligible": eligible,
            "missing": max(eligible - certified - (stats["expired"] or 0)
                           - (stats["in_progress"] or 0), 0),
            "pct": round(certified / eligible * 100) if eligible else 0,
        })

    sessions = conn.execute(
        """SELECT s.*, co.code, co.title, co.category,
                  (SELECT COUNT(*) FROM session_signups g
                    WHERE g.session_id = s.id AND g.org_id = s.org_id) taken
             FROM class_sessions s JOIN courses co ON co.id = s.course_id AND co.org_id = s.org_id
            WHERE s.org_id = ? AND s.starts_at >= ? ORDER BY s.starts_at LIMIT 12""",
        (org_id, date.today().isoformat()),
    ).fetchall()

    horizon = (date.today() + timedelta(days=60)).isoformat()
    renewals = conn.execute(
        """SELECT c.*, r.name rep_name, r.id rep_id, r.team, t.name track_name, t.is_mandatory
             FROM certifications c
             JOIN reps r ON r.id = c.rep_id AND r.org_id = c.org_id
             JOIN cert_tracks t ON t.id = c.track_id AND t.org_id = c.org_id
            WHERE c.org_id = ? AND r.status = 'active'
              AND (c.status = 'expired' OR (c.expires_on IS NOT NULL AND c.expires_on <= ?))
            ORDER BY c.expires_on""",
        (org_id, horizon),
    ).fetchall()

    _, rep_ids = _scope(conn)
    if rep_ids is not None:
        renewals = [r for r in renewals if r["rep_id"] in rep_ids]
    reps = conn.execute(
        "SELECT id, name FROM reps WHERE org_id = ? AND status = 'active' ORDER BY name",
        (org_id,),
    ).fetchall()
    conn.close()
    return render_template("training.html", courses=courses, track_rows=track_rows,
                           sessions=sessions, renewals=renewals, headcount=headcount,
                           reps=reps)


@app.route("/session/<int:session_id>/signup", methods=["POST"])
@auth.role_required("manager")
def session_signup(session_id):
    conn = get_connection()
    org_id = auth.current_org_id()
    auth.scoped_one(conn, "class_sessions", session_id)
    rep_id = int(request.form["rep_id"])
    rep = auth.scoped_one(conn, "reps", rep_id)
    conn.execute(
        "INSERT OR IGNORE INTO session_signups (org_id, session_id, rep_id, signed_up_on) "
        "VALUES (?,?,?,?)",
        (org_id, session_id, rep_id, date.today().isoformat()),
    )
    conn.commit()
    conn.close()
    flash(f"{rep['name']} is booked into the session.")
    return redirect(url_for("training"))


# ------------------------------------------------------------- compliance --

@app.route("/settings/compliance")
@auth.role_required("admin")
def settings_compliance():
    """
    Retention status, subject-rights tooling, and the bias-audit export.

    None of this makes an organization compliant. It makes the things a regulator
    or an auditor asks for producible, which is the part software can do.
    """
    conn = get_connection()
    org_id = auth.current_org_id()
    summary = compliance.retention_summary(conn, org_id, Config.CANDIDATE_RETENTION_DAYS)
    due = compliance.due_for_purge(conn, org_id)
    conn.close()
    return render_template("settings_compliance.html", summary=summary, due=due)


@app.route("/settings/compliance/backfill", methods=["POST"])
@auth.role_required("admin")
def compliance_backfill():
    conn = get_connection()
    org_id = auth.current_org_id()
    n = compliance.backfill_retention(conn, org_id, Config.CANDIDATE_RETENTION_DAYS)
    auth.audit(conn, "compliance.backfill", detail={"stamped": n})
    conn.commit()
    conn.close()
    flash(f"Stamped a retention date on {n} candidate records, measured from when "
          f"each was created.")
    return redirect(url_for("settings_compliance"))


@app.route("/settings/compliance/purge", methods=["POST"])
@auth.role_required("owner")
def compliance_purge():
    """Owner-only: this destroys data and cannot be undone."""
    conn = get_connection()
    org_id = auth.current_org_id()
    dry = request.form.get("dry_run") == "1"
    rep = compliance.purge_expired(conn, org_id, dry_run=dry)
    if not dry:
        auth.audit(conn, "compliance.purge", detail=rep)
        conn.commit()
    conn.close()
    if dry:
        flash(f"Dry run — {rep['examined']} candidate records are past their "
              f"retention date and would be deleted. Nothing was changed.")
    else:
        flash(f"Purged {rep['purged']} candidate records past their retention date.")
    return redirect(url_for("settings_compliance"))


@app.route("/settings/compliance/adverse-impact.csv")
@auth.role_required("admin")
def compliance_adverse_impact():
    """
    The dataset an independent bias auditor needs, minus demographics.

    QuotaOS holds no race or sex data by design — see compliance.py. The auditor
    joins their separately-collected EEO self-identification on `candidate_key`.
    """
    conn = get_connection()
    org_id = auth.current_org_id()
    csv_text = compliance.adverse_impact_csv(conn, org_id, request.args.get("since"))
    auth.audit(conn, "compliance.export", detail={"kind": "adverse_impact"})
    conn.commit()
    conn.close()
    return Response(csv_text, mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=quotaos-adverse-impact-"
                               f"{date.today().isoformat()}.csv"})


@app.route("/candidate/<int:candidate_id>/export.json")
@auth.role_required("admin")
def candidate_export(candidate_id):
    """GDPR Art. 15 — everything held about one person, including the AI reasoning."""
    conn = get_connection()
    org_id = auth.current_org_id()
    data = compliance.export_candidate(conn, org_id, candidate_id)
    if data is None:
        conn.close()
        abort(404)
    auth.audit(conn, "compliance.export", entity=f"candidate:{candidate_id}",
               detail={"kind": "subject_access"})
    conn.commit()
    conn.close()
    return Response(json.dumps(data, indent=2, default=str), mimetype="application/json",
                    headers={"Content-Disposition":
                             f"attachment; filename=candidate-{candidate_id}.json"})


@app.route("/candidate/<int:candidate_id>/erase", methods=["POST"])
@auth.role_required("owner")
def candidate_erase(candidate_id):
    """
    GDPR Art. 17 — destroy the person, keep the decision.

    Owner-only and irreversible. The audit entries survive with identifiers
    stripped, so the process stays auditable without retaining the human.
    """
    conn = get_connection()
    org_id = auth.current_org_id()
    result = compliance.erase_candidate(conn, org_id, candidate_id)
    if result is None:
        conn.close()
        abort(404)
    auth.audit(conn, "compliance.erase", entity=f"candidate:{candidate_id}",
               detail={"applications_removed": result["applications"]})
    conn.commit()
    conn.close()
    flash(f"Erased all data for {result['name']} — {result['applications']} "
          f"application(s) removed. Redacted audit entries were kept.")
    return redirect(url_for("candidates"))


# ----------------------------------------------------------------- import --

@app.route("/import", methods=["GET", "POST"])
@auth.role_required("admin")
def data_import():
    """
    Upload a Salesforce or HubSpot export.

    Admin-only: an import writes deals and can create reps across the whole org,
    which is not a per-manager action.
    """
    conn = get_connection()
    org_id = auth.current_org_id()

    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            conn.close()
            flash("Choose a CSV export first.")
            return redirect(url_for("data_import"))
        try:
            text = upload.read().decode("utf-8-sig", errors="replace")
        except Exception:
            conn.close()
            flash("That file could not be read as text. Export as CSV, not XLSX.")
            return redirect(url_for("data_import"))

        rows, source, headers = importer.parse_csv(text)
        if not rows:
            conn.close()
            flash("No data rows found in that file.")
            return redirect(url_for("data_import"))

        import_id = conn.execute(
            """INSERT INTO imports (org_id, user_id, source, filename, raw_csv, status,
                                    created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (org_id, g.user["id"], source, upload.filename[:200], text, "preview",
             datetime.now().isoformat(timespec="seconds")),
        ).lastrowid
        conn.commit()
        conn.close()
        return redirect(url_for("import_preview", import_id=import_id))

    history = conn.execute(
        """SELECT i.*, u.name user_name FROM imports i
             LEFT JOIN users u ON u.id = i.user_id AND u.org_id = i.org_id
            WHERE i.org_id = ? ORDER BY i.created_at DESC LIMIT 20""",
        (org_id,),
    ).fetchall()
    parsed = []
    for h in history:
        parsed.append({"row": h,
                       "report": json.loads(h["report_json"]) if h["report_json"] else None})
    imported = conn.execute(
        "SELECT COUNT(*) n FROM deals WHERE org_id = ? AND external_id IS NOT NULL",
        (org_id,),
    ).fetchone()["n"]
    conn.close()
    return render_template("import.html", history=parsed, imported=imported)


def _load_import(conn, import_id):
    row = conn.execute("SELECT * FROM imports WHERE id = ? AND org_id = ?",
                       (import_id, auth.current_org_id())).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/import/<int:import_id>")
@auth.role_required("admin")
def import_preview(import_id):
    """Show exactly what applying this file would do. Writes nothing."""
    conn = get_connection()
    imp = _load_import(conn, import_id)
    create_reps = request.args.get("create_reps") == "1"
    rows, source, headers = importer.parse_csv(imp["raw_csv"])
    p = importer.plan(conn, auth.current_org_id(), rows, source,
                      create_missing_reps=create_reps)
    report = json.loads(imp["report_json"]) if imp["report_json"] else None
    conn.close()
    return render_template("import_preview.html", imp=imp, plan=p, headers=headers,
                           create_reps=create_reps, report=report,
                           our_stages=importer.OUR_STAGES)


@app.route("/import/<int:import_id>/apply", methods=["POST"])
@auth.role_required("admin")
def import_apply(import_id):
    conn = get_connection()
    org_id = auth.current_org_id()
    imp = _load_import(conn, import_id)
    if imp["status"] == "applied":
        conn.close()
        flash("That import has already been applied.")
        return redirect(url_for("import_preview", import_id=import_id))

    create_reps = request.form.get("create_reps") == "1"
    rows, source, _ = importer.parse_csv(imp["raw_csv"])
    report = importer.apply(conn, org_id, rows, source, g.user["id"],
                            create_missing_reps=create_reps)
    conn.execute(
        "UPDATE imports SET status = 'applied', report_json = ?, applied_at = ? "
        "WHERE id = ? AND org_id = ?",
        (json.dumps(report), datetime.now().isoformat(timespec="seconds"),
         import_id, org_id))
    auth.audit(conn, "data.import", entity=f"import:{import_id}", detail=report)
    conn.commit()
    conn.close()
    flash(f"Imported from {source.title()}: {report['created']} deals created, "
          f"{report['updated']} updated, {report['skipped']} skipped.")
    return redirect(url_for("import_preview", import_id=import_id))


@app.route("/import/<int:import_id>/discard", methods=["POST"])
@auth.role_required("admin")
def import_discard(import_id):
    """Drop an unapplied upload, including the raw file. It is customer PII."""
    conn = get_connection()
    org_id = auth.current_org_id()
    _load_import(conn, import_id)
    conn.execute("UPDATE imports SET status = 'discarded', raw_csv = '' "
                 "WHERE id = ? AND org_id = ? AND status != 'applied'",
                 (import_id, org_id))
    conn.commit()
    conn.close()
    flash("Upload discarded and the file contents removed.")
    return redirect(url_for("data_import"))


# ----------------------------------------------------------------- hiring --

@app.route("/hiring")
@auth.role_required("manager")
def hiring():
    conn = get_connection()
    org_id = auth.current_org_id()
    reqs = conn.execute(
        """SELECT q.*, r.name manager_name, COUNT(a.id) applicants,
                  SUM(CASE WHEN a.stage NOT IN ('rejected','hired') THEN 1 ELSE 0 END) active
             FROM reqs q
             LEFT JOIN reps r ON r.id = q.hiring_manager_id AND r.org_id = q.org_id
             LEFT JOIN applications a ON a.req_id = q.id AND a.org_id = q.org_id
            WHERE q.org_id = ? GROUP BY q.id ORDER BY q.status, q.created_at DESC""",
        (org_id,),
    ).fetchall()
    board = {s: [] for s in STAGES}
    board["rejected"] = []
    rows = conn.execute(
        """SELECT a.*, c.name cand_name, c.current_title, c.years_exp, q.title req_title
             FROM applications a
             JOIN candidates c ON c.id = a.candidate_id AND c.org_id = a.org_id
             JOIN reqs q ON q.id = a.req_id AND q.org_id = a.org_id
            WHERE a.org_id = ? ORDER BY a.match_score DESC""",
        (org_id,),
    ).fetchall()
    for r in rows:
        board.setdefault(r["stage"], []).append(r)
    conn.close()
    return render_template("hiring.html", reqs=reqs, board=board)


@app.route("/req/new", methods=["GET", "POST"])
@auth.role_required("manager")
def req_new():
    conn = get_connection()
    org_id = auth.current_org_id()
    managers = conn.execute(
        "SELECT * FROM reps WHERE org_id = ? AND role IN ('manager','vp') ORDER BY name",
        (org_id,),
    ).fetchall()
    if request.method == "POST":
        f = request.form
        hm = f.get("hiring_manager_id") or None
        if hm and not conn.execute("SELECT 1 FROM reps WHERE id = ? AND org_id = ?",
                                   (hm, org_id)).fetchone():
            conn.close()
            abort(403)
        try:
            quota = int(f.get("quota_annual") or 0)
            base = int(f.get("comp_base") or 0)
            ote = int(f.get("comp_ote") or 0)
        except ValueError:
            conn.close()
            flash("Quota and compensation must be whole numbers.")
            return redirect(url_for("req_new"))

        req_id = conn.execute(
            """INSERT INTO reqs (org_id, title, level, team, region, segment, quota_annual,
                                 comp_base, comp_ote, must_haves, nice_haves,
                                 hiring_manager_id, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, f["title"], f["level"], f["team"], f["region"], f["segment"],
             quota, base, ote, f["must_haves"], f.get("nice_haves", ""), hm,
             "draft", date.today().isoformat()),
        ).lastrowid
        conn.commit()
        conn.close()
        flash("Requisition created. Generate the posting when you're ready.")
        return redirect(url_for("req_detail", req_id=req_id))
    conn.close()
    return render_template("req_new.html", managers=managers)


@app.route("/req/<int:req_id>")
@auth.role_required("manager")
def req_detail(req_id):
    conn = get_connection()
    org_id = auth.current_org_id()
    req = auth.scoped_one(conn, "reqs", req_id)
    manager = None
    if req["hiring_manager_id"]:
        manager = conn.execute("SELECT * FROM reps WHERE id = ? AND org_id = ?",
                               (req["hiring_manager_id"], org_id)).fetchone()
    apps = conn.execute(
        """SELECT a.*, c.name cand_name, c.current_title, c.current_company, c.years_exp,
                  (SELECT COUNT(*) FROM interviews i
                    WHERE i.application_id = a.id AND i.org_id = a.org_id) interviews
             FROM applications a
             JOIN candidates c ON c.id = a.candidate_id AND c.org_id = a.org_id
            WHERE a.org_id = ? AND a.req_id = ? ORDER BY
              CASE a.stage WHEN 'hired' THEN 0 WHEN 'offer' THEN 1 WHEN 'panel' THEN 2
                           WHEN 'ai_interview' THEN 3 WHEN 'screened' THEN 4
                           WHEN 'sourced' THEN 5 ELSE 6 END, a.match_score DESC""",
        (org_id, req_id),
    ).fetchall()
    conn.close()
    return render_template("req.html", req=req, manager=manager, apps=apps)


@app.route("/req/<int:req_id>/posting", methods=["POST"])
@auth.role_required("manager")
def req_posting(req_id):
    """AI job posting generation."""
    conn = get_connection()
    org_id = auth.current_org_id()
    req = auth.scoped_one(conn, "reqs", req_id)
    ok, why = ai_budget_check(conn, org_id, g.user["ai_cents_cap"])
    if not ok:
        conn.close()
        flash(why)
        return redirect(url_for("req_detail", req_id=req_id))

    markdown_text, engine = ai_engine.write_posting(req)
    spent = ai_budget_record(conn, org_id)
    conn.execute(
        "UPDATE reqs SET posting_md = ?, posting_source = ?, status = ? "
        "WHERE id = ? AND org_id = ?",
        (markdown_text, engine, "open" if req["status"] == "draft" else req["status"],
         req_id, org_id),
    )
    auth.audit(conn, "ai.posting", entity=f"req:{req_id}", engine=engine,
               detail={"title": req["title"], "cents": round(spent, 4)})
    conn.commit()
    conn.close()
    flash(f"Posting written by {'Claude' if engine == 'claude' else 'the offline engine'}.")
    return redirect(url_for("req_detail", req_id=req_id))


@app.route("/search", methods=["GET", "POST"])
@auth.role_required("manager")
def search():
    """AI resume search over the candidate pool."""
    conn = get_connection()
    org_id = auth.current_org_id()
    reqs = conn.execute("SELECT * FROM reqs WHERE org_id = ? ORDER BY title",
                        (org_id,)).fetchall()
    results, query, engine, req = [], "", None, None

    if request.method == "POST":
        query = request.form.get("query", "").strip()[:2000]
        req_id = request.form.get("req_id")
        if req_id:
            req = auth.scoped_one(conn, "reqs", int(req_id))
        if not query:
            flash("Describe who you're looking for, in your own words.")
        elif not ai_budget_check(conn, org_id, g.user["ai_cents_cap"])[0]:
            flash(ai_budget_check(conn, org_id, g.user["ai_cents_cap"])[1])
        else:
            # Bounded so one enormous pool can't produce one enormous prompt.
            candidates = conn.execute(
                "SELECT * FROM candidates WHERE org_id = ? ORDER BY created_at DESC LIMIT ?",
                (org_id, Config.AI_SEARCH_MAX_CANDIDATES),
            ).fetchall()
            ranked, engine = ai_engine.search_resumes(query, candidates, req)
            spent = ai_budget_record(conn, org_id)
            auth.audit(conn, "ai.search", engine=engine,
                       detail={"query": query, "pool": len(candidates),
                               "req_id": req["id"] if req else None,
                               "cents": round(spent, 4)})
            by_id = {c["id"]: c for c in candidates}
            for r in ranked:
                cand = by_id.get(r["candidate_id"])
                if not cand:
                    continue
                existing = None
                if req:
                    existing = conn.execute(
                        "SELECT * FROM applications WHERE org_id = ? AND candidate_id = ? "
                        "AND req_id = ?", (org_id, cand["id"], req["id"]),
                    ).fetchone()
                results.append({"c": cand, "r": r, "app": existing})
    conn.close()
    return render_template("search.html", reqs=reqs, results=results, query=query,
                           engine=engine, req=req)


@app.route("/req/<int:req_id>/add/<int:candidate_id>", methods=["POST"])
@auth.role_required("manager")
def add_candidate(req_id, candidate_id):
    """Move a search hit into the funnel."""
    conn = get_connection()
    org_id = auth.current_org_id()
    auth.scoped_one(conn, "reqs", req_id)
    auth.scoped_one(conn, "candidates", candidate_id)
    now = date.today().isoformat()
    existing = conn.execute(
        "SELECT * FROM applications WHERE org_id = ? AND req_id = ? AND candidate_id = ?",
        (org_id, req_id, candidate_id),
    ).fetchone()
    if existing:
        conn.close()
        flash("That candidate is already in this funnel.")
        return redirect(url_for("req_detail", req_id=req_id))

    score = request.form.get("score")
    aid = conn.execute(
        """INSERT INTO applications (org_id, req_id, candidate_id, stage, match_score,
                                     match_reason, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (org_id, req_id, candidate_id, "sourced",
         int(score) if (score or "").isdigit() else None,
         request.form.get("reason") or None, now, now),
    ).lastrowid
    conn.execute(
        """INSERT INTO stage_events (org_id, application_id, from_stage, to_stage, note,
                                     actor_user_id, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (org_id, aid, None, "sourced", "Added from AI resume search", g.user["id"], now),
    )
    compliance.set_retention(conn, org_id, candidate_id, Config.CANDIDATE_RETENTION_DAYS)
    conn.commit()
    conn.close()
    flash("Candidate added to the funnel.")
    return redirect(url_for("application_detail", app_id=aid))


@app.route("/candidates")
@auth.role_required("manager")
def candidates():
    conn = get_connection()
    rows = conn.execute(
        """SELECT c.*, COUNT(a.id) apps FROM candidates c
             LEFT JOIN applications a ON a.candidate_id = c.id AND a.org_id = c.org_id
            WHERE c.org_id = ? GROUP BY c.id ORDER BY c.created_at DESC""",
        (auth.current_org_id(),),
    ).fetchall()
    conn.close()
    return render_template("candidates.html", candidates=rows,
                           retention_days=Config.CANDIDATE_RETENTION_DAYS)


@app.route("/application/<int:app_id>")
@auth.role_required("manager")
def application_detail(app_id):
    conn = get_connection()
    org_id = auth.current_org_id()
    auth.scoped_one(conn, "applications", app_id)
    row = conn.execute(
        """SELECT a.*, c.name cand_name, c.email, c.phone, c.location, c.years_exp,
                  c.current_title, c.current_company, c.resume_text, c.source,
                  q.title req_title, q.id req_id
             FROM applications a
             JOIN candidates c ON c.id = a.candidate_id AND c.org_id = a.org_id
             JOIN reqs q ON q.id = a.req_id AND q.org_id = a.org_id
            WHERE a.org_id = ? AND a.id = ?""",
        (org_id, app_id),
    ).fetchone()
    req = conn.execute("SELECT * FROM reqs WHERE id = ? AND org_id = ?",
                       (row["req_id"], org_id)).fetchone()
    interviews = conn.execute(
        "SELECT * FROM interviews WHERE org_id = ? AND application_id = ? "
        "ORDER BY created_at DESC", (org_id, app_id),
    ).fetchall()
    events = conn.execute(
        """SELECT s.*, u.name actor_name FROM stage_events s
             LEFT JOIN users u ON u.id = s.actor_user_id AND u.org_id = s.org_id
            WHERE s.org_id = ? AND s.application_id = ? ORDER BY s.created_at, s.id""",
        (org_id, app_id),
    ).fetchall()
    parsed = [{
        "row": i,
        "questions": json.loads(i["questions_json"]),
        "scores": json.loads(i["scores_json"]) if i["scores_json"] else None,
        "answers": json.loads(i["answers_json"]) if i["answers_json"] else {},
    } for i in interviews]
    conn.close()
    return render_template("application.html", a=row, req=req, interviews=parsed,
                           events=events, kinds=ai_engine.INTERVIEW_KINDS)


@app.route("/application/<int:app_id>/stage", methods=["POST"])
@auth.role_required("manager")
def move_stage(app_id):
    to_stage = request.form["to_stage"]
    if to_stage not in STAGE_LABELS:
        abort(400, description="Unknown stage.")
    conn = get_connection()
    org_id = auth.current_org_id()
    a = auth.scoped_one(conn, "applications", app_id)
    now = date.today().isoformat()
    conn.execute("UPDATE applications SET stage = ?, updated_at = ? WHERE id = ? AND org_id = ?",
                 (to_stage, now, app_id, org_id))
    conn.execute(
        """INSERT INTO stage_events (org_id, application_id, from_stage, to_stage, note,
                                     actor_user_id, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (org_id, app_id, a["stage"], to_stage, request.form.get("note", ""),
         g.user["id"], now),
    )
    auth.audit(conn, "stage.move", entity=f"application:{app_id}",
               detail={"from": a["stage"], "to": to_stage})
    conn.commit()
    conn.close()
    flash(f"Moved to {STAGE_LABELS.get(to_stage, to_stage)}.")
    if to_stage == "hired":
        return redirect(url_for("onboard", app_id=app_id))
    return redirect(url_for("application_detail", app_id=app_id))


@app.route("/application/<int:app_id>/interview/new", methods=["POST"])
@auth.role_required("manager")
def interview_new(app_id):
    """AI interview generation — questions plus the rubric they'll be scored on."""
    kind = request.form.get("kind", "screen")
    if kind not in ai_engine.INTERVIEW_KINDS:
        abort(400, description="Unknown interview type.")
    conn = get_connection()
    org_id = auth.current_org_id()
    a = auth.scoped_one(conn, "applications", app_id)
    ok, why = ai_budget_check(conn, org_id, g.user["ai_cents_cap"])
    if not ok:
        conn.close()
        flash(why)
        return redirect(url_for("application_detail", app_id=app_id))

    req = conn.execute("SELECT * FROM reqs WHERE id = ? AND org_id = ?",
                       (a["req_id"], org_id)).fetchone()
    questions, engine = ai_engine.build_interview(req, kind)
    spent = ai_budget_record(conn, org_id)
    iid = conn.execute(
        """INSERT INTO interviews (org_id, application_id, kind, questions_json, engine,
                                   model, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (org_id, app_id, kind, json.dumps(questions), engine,
         ai_engine.MODEL if engine == "claude" else None, date.today().isoformat()),
    ).lastrowid
    if a["stage"] in ("sourced", "screened"):
        conn.execute(
            "UPDATE applications SET stage = 'ai_interview', updated_at = ? "
            "WHERE id = ? AND org_id = ?", (date.today().isoformat(), app_id, org_id))
        conn.execute(
            """INSERT INTO stage_events (org_id, application_id, from_stage, to_stage, note,
                                         actor_user_id, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (org_id, app_id, a["stage"], "ai_interview", f"{kind} interview generated",
             g.user["id"], date.today().isoformat()))
    auth.audit(conn, "ai.interview", entity=f"application:{app_id}", engine=engine,
               detail={"kind": kind, "questions": len(questions), "cents": round(spent, 4)})
    conn.commit()
    conn.close()
    flash(f"Interview generated by {'Claude' if engine == 'claude' else 'the offline engine'}.")
    return redirect(url_for("interview_run", interview_id=iid))


@app.route("/interview/<int:interview_id>", methods=["GET", "POST"])
@auth.role_required("manager")
def interview_run(interview_id):
    """Conduct the interview, then score every answer against the rubric."""
    conn = get_connection()
    org_id = auth.current_org_id()
    iv = auth.scoped_one(conn, "interviews", interview_id)
    a = conn.execute(
        """SELECT a.*, c.name cand_name, q.title req_title, q.id req_id
             FROM applications a
             JOIN candidates c ON c.id = a.candidate_id AND c.org_id = a.org_id
             JOIN reqs q ON q.id = a.req_id AND q.org_id = a.org_id
            WHERE a.org_id = ? AND a.id = ?""",
        (org_id, iv["application_id"]),
    ).fetchone()
    req = conn.execute("SELECT * FROM reqs WHERE id = ? AND org_id = ?",
                       (a["req_id"], org_id)).fetchone()
    questions = json.loads(iv["questions_json"])

    if request.method == "POST":
        ok, why = ai_budget_check(conn, org_id, g.user["ai_cents_cap"])
        if not ok:
            conn.close()
            flash(why)
            return redirect(url_for("interview_run", interview_id=interview_id))
        answers = {str(q["id"]): request.form.get(f"answer_{q['id']}", "").strip()[:8000]
                   for q in questions}
        result, engine = ai_engine.score_interview(req, questions, answers)
        spent = ai_budget_record(conn, org_id)
        conn.execute(
            """UPDATE interviews SET answers_json = ?, scores_json = ?, overall_score = ?,
                      recommendation = ?, summary = ?, engine = ?, model = ?, completed_at = ?
                WHERE id = ? AND org_id = ?""",
            (json.dumps(answers), json.dumps(result), result["overall_score"],
             result["recommendation"], result["summary"], engine,
             ai_engine.MODEL if engine == "claude" else None,
             date.today().isoformat(), interview_id, org_id),
        )
        # The full input and output are recorded, because "the model said 62" is
        # not a defensible answer to a candidate or a regulator on its own.
        auth.audit(conn, "ai.score", entity=f"application:{a['id']}", engine=engine,
                   detail={"interview_id": interview_id,
                           "overall": result["overall_score"],
                           "recommendation": result["recommendation"],
                           "cents": round(spent, 4),
                           "per_question": result["scores"]})
        conn.commit()
        conn.close()
        flash(f"Scored by {'Claude' if engine == 'claude' else 'the offline engine'} — "
              f"{result['overall_score']}/100, recommendation: {result['recommendation']}.")
        return redirect(url_for("application_detail", app_id=a["id"]))

    answers = json.loads(iv["answers_json"]) if iv["answers_json"] else {}
    scores = json.loads(iv["scores_json"]) if iv["scores_json"] else None
    conn.close()
    return render_template("interview.html", iv=iv, a=a, req=req, questions=questions,
                           answers=answers, scores=scores)


@app.route("/application/<int:app_id>/onboard", methods=["GET", "POST"])
@auth.role_required("manager")
def onboard(app_id):
    """
    The join that makes this one product instead of three.

    A hired candidate becomes a row in `reps` with a ramp window, and every
    mandatory certification track is enrolled on day one — so the new seat shows
    up on the dashboard already graded on a ramp curve rather than as an
    unexplained zero.
    """
    conn = get_connection()
    org_id = auth.current_org_id()
    auth.scoped_one(conn, "applications", app_id)
    a = conn.execute(
        """SELECT a.*, c.name cand_name, c.email, q.title req_title, q.team, q.region,
                  q.segment, q.quota_annual, q.level, q.hiring_manager_id
             FROM applications a
             JOIN candidates c ON c.id = a.candidate_id AND c.org_id = a.org_id
             JOIN reqs q ON q.id = a.req_id AND q.org_id = a.org_id
            WHERE a.org_id = ? AND a.id = ?""",
        (org_id, app_id),
    ).fetchone()
    if a["hired_rep_id"]:
        rep_id = a["hired_rep_id"]
        conn.close()
        flash("This hire is already on the org chart.")
        return redirect(url_for("rep_detail", rep_id=rep_id))

    if request.method == "POST":
        start = request.form.get("start_date") or date.today().isoformat()
        try:
            start_d = datetime.strptime(start, "%Y-%m-%d").date()
            ramp_days = max(0, min(int(request.form.get("ramp_days") or 120), 365))
        except ValueError:
            conn.close()
            flash("Start date must be YYYY-MM-DD and ramp must be a number of days.")
            return redirect(url_for("onboard", app_id=app_id))

        rep_id = conn.execute(
            """INSERT INTO reps (org_id, name, email, title, role, manager_id, team, region,
                                 segment, hire_date, ramp_end_date, quota_annual, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, a["cand_name"], a["email"], f"Account Executive, {a['segment']}",
             "rep", a["hiring_manager_id"], a["team"], a["region"], a["segment"],
             start, (start_d + timedelta(days=ramp_days)).isoformat(), a["quota_annual"],
             f"Hired from requisition: {a['req_title']}."),
        ).lastrowid

        tracks = conn.execute(
            "SELECT * FROM cert_tracks WHERE org_id = ? AND is_mandatory = 1", (org_id,)
        ).fetchall()
        for t in tracks:
            conn.execute(
                "INSERT INTO certifications (org_id, rep_id, track_id, status) VALUES (?,?,?,?)",
                (org_id, rep_id, t["id"], "in_progress"))
            for c in conn.execute(
                "SELECT course_id FROM track_courses WHERE org_id = ? AND track_id = ? "
                "ORDER BY sequence", (org_id, t["id"]),
            ).fetchall():
                conn.execute(
                    """INSERT OR IGNORE INTO enrollments
                       (org_id, rep_id, course_id, status, assigned_on, assigned_reason)
                       VALUES (?,?,?,?,?,?)""",
                    (org_id, rep_id, c["course_id"], "not_started", start,
                     f"Onboarding — required for {t['name']}"))

        conn.execute("UPDATE applications SET hired_rep_id = ?, stage = 'hired', updated_at = ? "
                     "WHERE id = ? AND org_id = ?",
                     (rep_id, date.today().isoformat(), app_id, org_id))
        conn.execute(
            """INSERT INTO stage_events (org_id, application_id, from_stage, to_stage, note,
                                         actor_user_id, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (org_id, app_id, a["stage"], "hired", f"Onboarded with a {ramp_days}-day ramp",
             g.user["id"], date.today().isoformat()))
        auth.audit(conn, "hire", entity=f"application:{app_id}",
                   detail={"rep_id": rep_id, "ramp_days": ramp_days})
        conn.commit()
        conn.close()
        flash(f"{a['cand_name']} is on the org chart with a {ramp_days}-day ramp and "
              f"{len(tracks)} mandatory certification tracks enrolled.")
        return redirect(url_for("rep_detail", rep_id=rep_id))

    tracks = conn.execute(
        "SELECT * FROM cert_tracks WHERE org_id = ? AND is_mandatory = 1 ORDER BY name",
        (org_id,),
    ).fetchall()
    conn.close()
    return render_template("onboard.html", a=a, tracks=tracks,
                           default_start=date.today().isoformat())


if __name__ == "__main__":
    if not DB_FILE.exists():
        create_tables()
        seed_sample_data()
    app.run(debug=Config.DEBUG, port=5003)
