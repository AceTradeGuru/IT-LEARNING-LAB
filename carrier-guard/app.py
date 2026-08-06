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

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)

import db
from config import load
from queries import carrier_staleness, network_stats
from risk_engine import evaluate, normalize_phone, run_check
from seed import DEMO_BROKER_ID

cfg = load()

app = Flask(__name__)
app.secret_key = cfg.SECRET_KEY
app.config.update(
    SESSION_COOKIE_SECURE=cfg.SESSION_COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=cfg.SESSION_COOKIE_HTTPONLY,
    SESSION_COOKIE_SAMESITE=cfg.SESSION_COOKIE_SAMESITE,
    MAX_CONTENT_LENGTH=cfg.MAX_CONTENT_LENGTH,
)

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


def current_broker_id():
    return session.get("broker_id", DEMO_BROKER_ID)


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


@app.context_processor
def inject_broker():
    """Every page shows which subscriber you're acting as."""
    with db.connection() as conn:
        brokers = conn.execute(
            "SELECT * FROM brokers ORDER BY company_name").fetchall()
        me = conn.execute(
            "SELECT * FROM brokers WHERE id = %s", (current_broker_id(),)).fetchone()
    return {"all_brokers": brokers, "me": me}


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


@app.route("/act-as", methods=["POST"])
def act_as():
    try:
        broker_id = int(request.form["broker_id"])
    except (KeyError, ValueError):
        abort(400)

    with db.connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM brokers WHERE id = %s", (broker_id,)).fetchone()
    if not exists:
        abort(400)

    session["broker_id"] = broker_id
    # Only ever redirect to a path on this app — never to a caller-supplied URL.
    nxt = request.form.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                    else url_for("index"))


# ---------- Landing ----------

@app.route("/")
def index():
    with db.connection() as conn:
        stats = network_stats(conn)
    return render_template("index.html", stats=stats, scenarios=SCENARIOS)


# ---------- The check ----------

@app.route("/check", methods=["GET", "POST"])
def check():
    if request.method == "GET":
        # Prefilled from a scenario link, or blank.
        return render_template("check.html", form=request.args, scenarios=SCENARIOS)

    f = request.form
    mc_number = clean_text(f.get("mc_number")).upper()
    if not mc_number:
        flash("Enter an MC number to run a check.")
        return redirect(url_for("check"))

    load_value = parse_money(f.get("load_value"))

    with db.connection() as conn:
        check_id, result = run_check(
            conn, current_broker_id(), mc_number,
            clean_text(f.get("contact_email")), clean_text(f.get("contact_phone")),
            clean_text(f.get("contact_name")), load_value)

    log.info("check id=%s broker=%s mc=%s verdict=%s score=%s",
             check_id, current_broker_id(), mc_number,
             result["verdict"], result["score"])

    return render_template(
        "result.html", check_id=check_id, carrier=result["carrier"],
        signals=as_dicts(result["signals"]), score=result["score"],
        verdict=result["verdict"], action=VERDICT_ACTION[result["verdict"]],
        mc_number=mc_number,
        submitted=submitted_contact(mc_number, f.get("contact_email"),
                                    f.get("contact_phone"), f.get("contact_name"),
                                    load_value))


@app.route("/check/<int:check_id>")
def check_detail(check_id):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM checks WHERE id = %s", (check_id,)).fetchone()
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
def registry():
    rows = []
    with db.connection() as conn:
        carriers = conn.execute(
            "SELECT * FROM carriers ORDER BY legal_name").fetchall()
        for c in carriers:
            # Score each carrier against its OWN contact of record, so the table
            # shows the honest baseline before anyone impersonates anybody.
            r = evaluate(conn, current_broker_id(), c["mc_number"],
                         c["registered_email"], c["registered_phone"], load_value=75000)
            rows.append({"carrier": c, "verdict": r["verdict"], "score": r["score"],
                         "top": r["signals"][0].label if r["signals"] else "No findings"})
    return render_template("registry.html", rows=rows)


# ---------- Fraud reporting: the part that makes the network worth joining ----------

VALID_INCIDENTS = {"double_brokered", "identity_theft", "payment_redirect",
                   "cargo_theft", "no_show", "fictitious_pickup"}


@app.route("/report", methods=["GET", "POST"])
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
        conn.execute(
            """INSERT INTO fraud_reports (broker_id, mc_number, reported_email,
                 reported_phone, incident_type, amount_lost_usd, notes)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (current_broker_id(), mc_number, reported_email or None,
             reported_phone or None, incident, parse_money(f.get("amount_lost")) or 0,
             clean_text(f.get("notes"), MAX_NOTES) or None))

    log.info("fraud_report broker=%s mc=%s type=%s",
             current_broker_id(), mc_number, incident)

    flash(f"Report filed against {mc_number}. Every broker on the network is "
          f"protected from it as of this second — re-run the check to see it.")
    return redirect(url_for("check", mc_number=mc_number,
                            contact_email=reported_email,
                            contact_phone=reported_phone))


# ---------- The network ----------

@app.route("/network")
def network():
    with db.connection() as conn:
        stats = network_stats(conn)
        checks = conn.execute(
            """SELECT c.*, b.company_name FROM checks c
               JOIN brokers b ON b.id = c.broker_id
               ORDER BY c.created_at DESC LIMIT 25""").fetchall()
        reports = conn.execute(
            """SELECT r.*, b.company_name, ca.legal_name FROM fraud_reports r
               JOIN brokers b ON b.id = r.broker_id
               LEFT JOIN carriers ca ON ca.mc_number = r.mc_number
               ORDER BY r.created_at DESC""").fetchall()
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
