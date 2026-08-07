"""
CarrierGuard — carrier vetting & fraud firewall.

Run the carrier before you tender the load.

Local development:
    docker compose up -d db
    python3 seed.py --reset
    python3 app.py          -> http://localhost:5002

Production is served by gunicorn against the `app` object; see Dockerfile.
"""

import logging
from datetime import timedelta

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import security
from api import api as api_blueprint
from ingest import resolve_carrier
from auth import (consume_check, create_organization, email_problem,
                  generate_api_key, load_current, login_required, login_user,
                  logout_user, password_problem, quota_state, verify_password)
from config import load
from queries import carrier_staleness, network_stats
from risk_engine import evaluate, normalize_phone, run_check
from security import client_ip, csrf_token, csrf_valid, limited

cfg = load()

app = Flask(__name__)
app.secret_key = cfg.SECRET_KEY
app.config.update(
    SESSION_COOKIE_SECURE=cfg.SESSION_COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=cfg.SESSION_COOKIE_HTTPONLY,
    SESSION_COOKIE_SAMESITE=cfg.SESSION_COOKIE_SAMESITE,
    MAX_CONTENT_LENGTH=cfg.MAX_CONTENT_LENGTH,
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)

# Only unwrap forwarded headers when a proxy count is configured. Applied
# unconditionally, this would let any caller spoof their own IP.
if cfg.TRUSTED_PROXY_HOPS:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=cfg.TRUSTED_PROXY_HOPS,
                            x_proto=cfg.TRUSTED_PROXY_HOPS, x_host=0, x_prefix=0)

app.register_blueprint(api_blueprint)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("carrierguard")

# Bounds on free-text input. Anything longer is a mistake or an attack, and
# either way it should be rejected at the edge rather than stored.
MAX_FIELD = 200
MAX_NOTES = 4000
MAX_LOAD_VALUE = 100_000_000

# One-click demo scenarios. Each is a real vetting story with a different
# failure mode, so a pitch can walk the whole surface in about two minutes.
SCENARIOS = [
    {
        "key": "clean",
        "title": "The carrier you've hauled with for years",
        "blurb": "Great Lakes Freight — established authority, contact details match, "
                 "clean history on the network.",
        "mc": "MC-471902",
        "email": "dispatch@greatlakesfreightsys.com",
        "phone": "(313) 555-0188",
        "name": "Karen Vaughn",
        "value": "78000",
        "expect": "clear",
    },
    {
        "key": "impostor",
        "title": "The same MC number — but not the same company",
        "blurb": "Identical authority, one hyphen added to the email domain, callback "
                 "on a VOIP line. SAFER says this carrier is spotless.",
        "mc": "MC-471902",
        "email": "dispatch@greatlakesfreight-sys.com",
        "phone": "(872) 555-0417",
        "name": "Dave Kruse",
        "value": "78000",
        "expect": "block",
    },
    {
        "key": "network",
        "title": "Clean paperwork, four different faces",
        "blurb": "Bayou Express — nothing wrong on the government record. It has shown "
                 "four dispatch identities to four brokers in nineteen days.",
        "mc": "MC-775410",
        "email": "dispatch@bayouexpresscarriers.com",
        "phone": "(504) 555-0129",
        "name": "T. Boudreaux",
        "value": "51000",
        "expect": "block",
    },
    {
        "key": "risky",
        "title": "Not a fraudster — just genuinely risky",
        "blurb": "Summit Haul — 90-day-old authority, insurance lapsing in 18 days, "
                 "and the terminal address is a mailbox store.",
        "mc": "MC-901347",
        "email": "dispatch@summithaultransport.com",
        "phone": "(602) 555-0117",
        "name": "Alicia Ferrer",
        "value": "64000",
        "expect": "review",
    },
]

VERDICT_ACTION = {
    "clear": ("Tender the load.",
              "Nothing in the government record, the contact details, or the network "
              "contradicts this carrier. Attach this check ID to the load file — it is "
              "your due-diligence record if anything goes wrong later."),
    "review": ("Do not tender yet. Verify by callback.",
               "Call the number on the FMCSA record below — never the number you were "
               "given — and confirm this person works there. If the load is high value, "
               "ask for a certificate of insurance sent directly by the agent."),
    "block": ("Do not tender this load.",
              "Walk away and report it. If you have already dispatched, contact the "
              "carrier of record directly at their number on file: they may not know "
              "their identity is being used."),
}


# ---------- input handling ----------

def clean_text(raw, limit=MAX_FIELD):
    """Trim and bound a free-text field. Never trust a form for length."""
    return (raw or "").strip()[:limit]


def parse_money(raw):
    """'$78,000' -> 78000. Returns None for anything that isn't a sane amount."""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if not digits:
        return None
    value = int(digits[:12])
    return value if 0 <= value <= MAX_LOAD_VALUE else None


def safe_next(raw, fallback_endpoint="index"):
    """Only ever redirect to a path on this app, never a caller-supplied host."""
    return (raw if raw.startswith("/") and not raw.startswith("//")
            else url_for(fallback_endpoint))


def as_dicts(signals):
    """Normalize Signal namedtuples and DB rows into one shape for templates."""
    return [s._asdict() if hasattr(s, "_asdict") else dict(s) for s in signals]


def submitted_contact(mc_number, email, phone, name, load_value):
    """
    What the carrier gave us, normalized. The result page compares these against
    the record field by field, so both sides have to be in the same format —
    otherwise every check would look like a mismatch.
    """
    return {
        "mc_number": clean_text(mc_number).upper(),
        "contact_email": clean_text(email).lower(),
        "contact_phone": normalize_phone(phone),
        "contact_name": clean_text(name),
        "load_value": load_value if load_value is not None else "",
    }


@app.template_filter("fmt_phone")
def fmt_phone(raw):
    """Display phone numbers one way everywhere, so a match looks like a match."""
    digits = normalize_phone(raw)
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return digits or "—"


@app.before_request
def attach_identity():
    """Resolve the signed-in user once per request. API routes authenticate separately."""
    g.user = None
    g.org = None
    if request.blueprint == "api" or not session.get("user_id"):
        return
    with db.connection() as conn:
        load_current(conn)


@app.before_request
def enforce_csrf():
    """
    Reject unsafe browser requests without the session's CSRF token.

    Blanket-enforced here rather than per route: an opt-in decorator protects
    only the endpoints someone remembered to decorate, and the one that gets
    forgotten is the one that matters. The API is exempt because bearer-token
    endpoints are not CSRF-able — browsers never attach Authorization headers
    on their own.
    """
    if request.blueprint == "api" or request.method not in security.UNSAFE_METHODS:
        return
    if not csrf_valid():
        log.warning("csrf rejected path=%s ip=%s", request.path, client_ip())
        return render_template(
            "error.html", code=400,
            message="That form expired or came from somewhere unexpected. "
                    "Go back, reload the page, and try again."), 400


@app.after_request
def secure_headers(response):
    return security.apply_headers(response, cfg.IS_PRODUCTION)


@app.context_processor
def inject_identity():
    quota = quota_state(g.org) if g.org else (0, None, False)
    return {"current_user": g.user, "current_org": g.org,
            "quota_used": quota[0], "quota_limit": quota[1],
            "csrf_token": csrf_token}


# ---------- operational ----------

@app.route("/healthz")
def healthz():
    """
    Liveness plus a real database round trip, because an app that can boot but
    can't reach Postgres is not healthy and should not receive traffic.
    """
    try:
        with db.connection() as conn:
            conn.execute("SELECT 1")
            stale = carrier_staleness(conn, cfg.CARRIER_STALE_DAYS)
    except Exception:
        log.exception("health check failed")
        return {"status": "unhealthy"}, 503

    return {
        "status": "ok",
        "carriers": stale["total"],
        "stale_carriers": stale["stale"],
        "last_refresh": stale["last_refresh"].isoformat() if stale["last_refresh"] else None,
    }


# ---------- accounts ----------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("check"))
    if request.method == "GET":
        return render_template("signup.html", form={})

    f = request.form
    company = clean_text(f.get("company_name"))
    email = clean_text(f.get("email")).lower()
    password = f.get("password") or ""

    problem = (("Enter your company name." if not company else None)
               or email_problem(email) or password_problem(password))
    if problem:
        flash(problem)
        return render_template("signup.html", form=f), 400

    with db.connection() as conn:
        allowed, retry = limited(conn, "signup_ip", client_ip())
        if not allowed:
            security.record(conn, "signup", target=email, outcome="denied",
                            detail={"reason": "rate_limited"})
            return render_template(
                "error.html", code=429,
                message="Too many accounts created from this network. "
                        f"Try again in {retry // 60 + 1} minutes."), 429

        user, error = create_organization(
            conn, company, email, password, clean_text(f.get("full_name")))
        if error:
            flash(error)
            return render_template("signup.html", form=f), 400
        login_user(conn, user)
        security.record(conn, "signup", target=email,
                        org_id=user["org_id"], user_id=user["id"])

    log.info("signup org=%s user=%s", user["org_id"], user["id"])
    flash("Account created. You're on the Starter plan — 250 checks a month.")
    return redirect(url_for("check"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("check"))
    if request.method == "GET":
        return render_template("login.html", next=request.args.get("next", ""))

    f = request.form
    email = clean_text(f.get("email")).lower()

    with db.connection() as conn:
        # Two keys, because either alone is trivially evaded: a botnet rotates
        # IPs against one account, and one host sprays many accounts from one IP.
        ip_ok, retry = limited(conn, "login_ip", client_ip())
        email_ok, retry_email = limited(conn, "login_email", email or "-")
        if not (ip_ok and email_ok):
            security.record(conn, "login", target=email, outcome="denied",
                            detail={"reason": "rate_limited"})
            return render_template(
                "error.html", code=429,
                message="Too many sign-in attempts. Try again in "
                        f"{max(retry, retry_email) // 60 + 1} minutes."), 429

        user = conn.execute(
            "SELECT * FROM users WHERE lower(email) = %s", (email,)).fetchone()
        # One message for both cases: distinguishing them turns the login form
        # into an oracle for which email addresses have accounts here.
        if user is None or not verify_password(user["password_hash"], f.get("password") or ""):
            security.record(conn, "login", target=email, outcome="failed")
            flash("Email or password is incorrect.")
            return render_template("login.html", next=f.get("next", "")), 401

        login_user(conn, user)
        security.record(conn, "login", target=email,
                        org_id=user["org_id"], user_id=user["id"])

    return redirect(safe_next(f.get("next", ""), "check"))


@app.post("/logout")
def logout():
    logout_user()
    flash("Signed out.")
    return redirect(url_for("index"))


@app.route("/settings")
@login_required
def settings():
    with db.connection() as conn:
        keys = conn.execute(
            """SELECT k.*, u.email AS created_by_email
               FROM api_keys k LEFT JOIN users u ON u.id = k.created_by
               WHERE k.org_id = %s ORDER BY k.revoked_at NULLS FIRST, k.created_at DESC""",
            (g.org["id"],)).fetchall()
        members = conn.execute(
            "SELECT * FROM users WHERE org_id = %s ORDER BY created_at",
            (g.org["id"],)).fetchall()

    # Shown exactly once, immediately after creation, then dropped.
    new_key = session.pop("new_api_key", None)
    return render_template("settings.html", keys=keys, members=members, new_key=new_key)


@app.post("/settings/api-keys")
@login_required
def create_api_key():
    name = clean_text(request.form.get("name"), 80) or "Untitled key"
    full, prefix, key_hash = generate_api_key()

    with db.connection() as conn:
        conn.execute(
            """INSERT INTO api_keys (org_id, name, key_prefix, key_hash, created_by)
               VALUES (%s, %s, %s, %s, %s)""",
            (g.org["id"], name, prefix, key_hash, g.user["user_id"]))
        security.record(conn, "api_key.create", target=prefix)

    # Carried in the session, not the URL: a key in a query string ends up in
    # browser history, proxy logs, and the Referer header of the next request.
    session["new_api_key"] = full
    log.info("api_key created org=%s prefix=%s", g.org["id"], prefix)
    flash("API key created. Copy it now — it will not be shown again.")
    return redirect(url_for("settings"))


@app.post("/settings/api-keys/<int:key_id>/revoke")
@login_required
def revoke_api_key(key_id):
    with db.connection() as conn:
        updated = conn.execute(
            """UPDATE api_keys SET revoked_at = now()
               WHERE id = %s AND org_id = %s AND revoked_at IS NULL
               RETURNING id""", (key_id, g.org["id"])).fetchone()
        if updated:
            security.record(conn, "api_key.revoke", target=str(key_id))
    if updated:
        log.info("api_key revoked org=%s key=%s", g.org["id"], key_id)
        flash("API key revoked. Any integration using it will start failing immediately.")
    return redirect(url_for("settings"))


# ---------- Landing ----------

@app.route("/")
def index():
    with db.connection() as conn:
        stats = network_stats(conn)
    return render_template("index.html", stats=stats, scenarios=SCENARIOS)


# ---------- The check ----------

@app.route("/check", methods=["GET", "POST"])
@login_required
def check():
    if request.method == "GET":
        # Prefilled from a scenario link, or blank.
        return render_template("check.html", form=request.args, scenarios=SCENARIOS)

    f = request.form
    mc_number = clean_text(f.get("mc_number")).upper()
    if not mc_number:
        flash("Enter an MC number to run a check.")
        return redirect(url_for("check"))

    used, limit, exceeded = quota_state(g.org)
    if exceeded:
        flash(f"You've used all {limit} checks on the {g.org['plan'].title()} plan "
              f"this period. Upgrade to keep checking.")
        return redirect(url_for("settings"))

    load_value = parse_money(f.get("load_value"))

    with db.connection() as conn:
        # Separate from the plan quota: that caps the month, this caps the
        # minute. A scraper burning a Pro allowance in ten seconds is not a
        # customer using the product.
        allowed, retry = limited(conn, "check_org", g.org["id"])
        if not allowed:
            return render_template(
                "error.html", code=429,
                message=f"Too many checks at once. Try again in {retry} seconds."), 429

        check_id, result = run_check(
            conn, g.org["id"], mc_number,
            clean_text(f.get("contact_email")), clean_text(f.get("contact_phone")),
            clean_text(f.get("contact_name")), load_value, resolver=resolve_carrier)
        consume_check(conn, g.org["id"])

    log.info("check id=%s org=%s mc=%s verdict=%s score=%s",
             check_id, g.org["id"], mc_number, result["verdict"], result["score"])

    return render_template(
        "result.html", check_id=check_id, carrier=result["carrier"],
        signals=as_dicts(result["signals"]), score=result["score"],
        verdict=result["verdict"], action=VERDICT_ACTION[result["verdict"]],
        mc_number=mc_number,
        submitted=submitted_contact(mc_number, f.get("contact_email"),
                                    f.get("contact_phone"), f.get("contact_name"),
                                    load_value))


@app.route("/check/<int:check_id>")
@login_required
def check_detail(check_id):
    with db.connection() as conn:
        # Scoped by org: a check history is a list of exactly which carriers a
        # brokerage is about to do business with. Subscribers are competitors.
        row = conn.execute(
            "SELECT * FROM checks WHERE id = %s AND org_id = %s",
            (check_id, g.org["id"])).fetchone()
        if row is None:
            abort(404)

        signals = conn.execute(
            "SELECT * FROM check_signals WHERE check_id = %s ORDER BY id",
            (check_id,)).fetchall()
        carrier = conn.execute(
            "SELECT * FROM carriers WHERE mc_number = %s", (row["mc_number"],)).fetchone()

    return render_template(
        "result.html", check_id=check_id, carrier=carrier, signals=as_dicts(signals),
        score=row["risk_score"], verdict=row["verdict"],
        action=VERDICT_ACTION[row["verdict"]],
        submitted=submitted_contact(row["mc_number"], row["contact_email"],
                                    row["contact_phone"], row["contact_name"],
                                    row["load_value_usd"]),
        mc_number=row["mc_number"], historical=row["created_at"])


# ---------- Registry: what the government record alone would tell you ----------

@app.route("/registry")
@login_required
def registry():
    rows = []
    with db.connection() as conn:
        carriers = conn.execute(
            "SELECT * FROM carriers ORDER BY legal_name").fetchall()
        for c in carriers:
            # Score each carrier against its OWN contact of record, so the table
            # shows the honest baseline before anyone impersonates anybody.
            r = evaluate(conn, g.org["id"], c["mc_number"],
                         c["registered_email"], c["registered_phone"], load_value=75000)
            rows.append({"carrier": c, "verdict": r["verdict"], "score": r["score"],
                         "top": r["signals"][0].label if r["signals"] else "No findings"})
    return render_template("registry.html", rows=rows)


# ---------- Fraud reporting: the part that makes the network worth joining ----------

VALID_INCIDENTS = {"double_brokered", "identity_theft", "payment_redirect",
                   "cargo_theft", "no_show", "fictitious_pickup"}


@app.route("/report", methods=["GET", "POST"])
@login_required
def report():
    if request.method == "GET":
        return render_template("report.html", form=request.args)

    f = request.form
    mc_number = clean_text(f.get("mc_number")).upper()
    incident = clean_text(f.get("incident_type"), 40)
    if not mc_number or incident not in VALID_INCIDENTS:
        flash("A report needs an MC number and a valid incident type.")
        return redirect(url_for("report"))

    reported_email = clean_text(f.get("reported_email")).lower()
    reported_phone = normalize_phone(f.get("reported_phone"))

    with db.connection() as conn:
        allowed, retry = limited(conn, "report_org", g.org["id"])
        if not allowed:
            security.record(conn, "fraud_report.create", target=mc_number,
                            outcome="denied", detail={"reason": "rate_limited"})
            flash("That's a lot of reports at once. Give it an hour, or contact "
                  "us if you genuinely have this many.")
            return redirect(url_for("report"))

        report_id = conn.execute(
            """INSERT INTO fraud_reports (org_id, mc_number, reported_email,
                 reported_phone, incident_type, amount_lost_usd, notes)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (g.org["id"], mc_number, reported_email or None,
             reported_phone or None, incident, parse_money(f.get("amount_lost")) or 0,
             clean_text(f.get("notes"), MAX_NOTES) or None)).fetchone()["id"]

        # The audit entry that matters most. This is one company publishing a
        # fraud accusation against another by name; when a carrier disputes it,
        # who filed it and from where is the entire defense.
        security.record(conn, "fraud_report.create", target=mc_number,
                        detail={"report_id": report_id, "incident_type": incident})

    log.info("fraud_report org=%s mc=%s type=%s", g.org["id"], mc_number, incident)

    flash(f"Report filed against {mc_number}. Every broker on the network is "
          f"protected from it as of this second — re-run the check to see it.")
    return redirect(url_for("check", mc_number=mc_number,
                            contact_email=reported_email,
                            contact_phone=reported_phone))


# ---------- The network ----------

@app.route("/network")
@login_required
def network():
    with db.connection() as conn:
        stats = network_stats(conn)
        # Your own check history only. The aggregate counts above are network-wide
        # (that is the point of the network), but the per-check feed is not:
        # publishing which carriers a brokerage is vetting would tell competitors
        # exactly which lanes and customers they are working.
        checks = conn.execute(
            """SELECT * FROM checks WHERE org_id = %s
               ORDER BY created_at DESC LIMIT 25""", (g.org["id"],)).fetchall()
        # Fraud reports ARE shared — a warning nobody can see protects nobody.
        reports = conn.execute(
            """SELECT r.*, o.name AS company_name, ca.legal_name FROM fraud_reports r
               JOIN organizations o ON o.id = r.org_id
               LEFT JOIN carriers ca ON ca.mc_number = r.mc_number
               ORDER BY r.created_at DESC LIMIT 50""").fetchall()
    return render_template("network.html", stats=stats, checks=checks, reports=reports)


# ---------- errors ----------

@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404,
                           message="That page doesn't exist."), 404


@app.errorhandler(413)
def too_large(_):
    return render_template("error.html", code=413,
                           message="That request was too large."), 413


@app.errorhandler(500)
def server_error(e):
    # Log the detail; show the user nothing about internals.
    log.exception("unhandled error: %s", e)
    return render_template("error.html", code=500,
                           message="Something went wrong on our end."), 500


if __name__ == "__main__":
    db.migrate()
    app.run(debug=not cfg.IS_PRODUCTION, port=5002)
