"""
CarrierGuard — carrier vetting & fraud firewall (demo prototype).

Run the carrier before you tender the load.

Run:
    python3 app.py
Then open http://localhost:5002
"""

from flask import (Flask, flash, redirect, render_template, request, session,
                   url_for)

from guard_db import (DEMO_BROKER_ID, create_tables, get_connection,
                      network_stats, seed_sample_data)
from risk_engine import evaluate, normalize_phone, run_check

app = Flask(__name__)
app.secret_key = "carrierguard-demo"

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


def db():
    return get_connection()


def current_broker_id():
    return session.get("broker_id", DEMO_BROKER_ID)


@app.context_processor
def inject_broker():
    """Every page shows which subscriber you're acting as."""
    conn = db()
    brokers = conn.execute("SELECT * FROM brokers ORDER BY company_name").fetchall()
    me = conn.execute("SELECT * FROM brokers WHERE id=?", (current_broker_id(),)).fetchone()
    conn.close()
    return {"all_brokers": brokers, "me": me}


@app.route("/act-as", methods=["POST"])
def act_as():
    session["broker_id"] = int(request.form["broker_id"])
    return redirect(request.form.get("next") or url_for("index"))


def as_dicts(signals):
    """Normalize Signal namedtuples and DB rows into one shape for templates."""
    out = []
    for s in signals:
        out.append(s._asdict() if hasattr(s, "_asdict") else dict(s))
    return out


@app.template_filter("fmt_phone")
def fmt_phone(raw):
    """Display phone numbers one way everywhere, so a match looks like a match."""
    digits = normalize_phone(raw)
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return digits or "—"


def submitted_contact(mc_number, email, phone, name, load_value):
    """
    What the carrier gave us, normalized. The result page compares these against
    the record field by field, so both sides have to be in the same format —
    otherwise every check would look like a mismatch.
    """
    return {
        "mc_number": (mc_number or "").strip().upper(),
        "contact_email": (email or "").strip().lower(),
        "contact_phone": normalize_phone(phone),
        "contact_name": (name or "").strip(),
        "load_value": load_value or "",
    }


# ---------- Landing ----------

@app.route("/")
def index():
    conn = db()
    stats = network_stats(conn)
    conn.close()
    return render_template("index.html", stats=stats, scenarios=SCENARIOS)


# ---------- The check ----------

@app.route("/check", methods=["GET", "POST"])
def check():
    if request.method == "GET":
        # Prefilled from a scenario link, or blank.
        return render_template("check.html", form=request.args, scenarios=SCENARIOS)

    f = request.form
    conn = db()
    value = f.get("load_value", "").replace(",", "").replace("$", "").strip()
    check_id, result = run_check(
        conn, current_broker_id(),
        f["mc_number"], f.get("contact_email", ""), f.get("contact_phone", ""),
        f.get("contact_name", ""), int(value) if value.isdigit() else None)
    conn.close()

    return render_template(
        "result.html", check_id=check_id, carrier=result["carrier"],
        signals=as_dicts(result["signals"]), score=result["score"],
        verdict=result["verdict"], action=VERDICT_ACTION[result["verdict"]],
        mc_number=f["mc_number"].strip().upper(),
        submitted=submitted_contact(f["mc_number"], f.get("contact_email"),
                                    f.get("contact_phone"), f.get("contact_name"), value))


@app.route("/check/<int:check_id>")
def check_detail(check_id):
    conn = db()
    row = conn.execute("SELECT * FROM checks WHERE id=?", (check_id,)).fetchone()
    if row is None:
        conn.close()
        return "No such check", 404

    signals = conn.execute(
        "SELECT * FROM check_signals WHERE check_id=? ORDER BY id", (check_id,)).fetchall()
    carrier = conn.execute(
        "SELECT * FROM carriers WHERE mc_number=?", (row["mc_number"],)).fetchone()
    conn.close()

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
    conn = db()
    carriers = conn.execute("SELECT * FROM carriers ORDER BY legal_name").fetchall()
    rows = []
    for c in carriers:
        # Score each carrier against its OWN contact of record, so the table shows
        # the honest baseline before anyone impersonates anybody.
        r = evaluate(conn, current_broker_id(), c["mc_number"],
                     c["registered_email"], c["registered_phone"], load_value=75000)
        rows.append({"carrier": c, "verdict": r["verdict"], "score": r["score"],
                     "top": r["signals"][0].label if r["signals"] else "No findings"})
    conn.close()
    return render_template("registry.html", rows=rows)


# ---------- Fraud reporting: the part that makes the network worth joining ----------

@app.route("/report", methods=["GET", "POST"])
def report():
    if request.method == "GET":
        return render_template("report.html", form=request.args)

    f = request.form
    amount = f.get("amount_lost", "").replace(",", "").replace("$", "").strip()
    conn = db()
    conn.execute(
        """INSERT INTO fraud_reports (broker_id, mc_number, reported_email,
             reported_phone, incident_type, amount_lost_usd, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (current_broker_id(), f["mc_number"].strip().upper(),
         f.get("reported_email", "").strip().lower() or None,
         normalize_phone(f.get("reported_phone", "")) or None,
         f["incident_type"], int(amount) if amount.isdigit() else 0,
         f.get("notes", "").strip() or None))
    conn.commit()
    conn.close()

    flash(f"Report filed against {f['mc_number'].strip().upper()}. Every broker on the "
          f"network is protected from it as of this second — re-run the check to see it.")
    return redirect(url_for("check", mc_number=f["mc_number"].strip().upper(),
                            contact_email=f.get("reported_email", ""),
                            contact_phone=f.get("reported_phone", "")))


# ---------- The network ----------

@app.route("/network")
def network():
    conn = db()
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
    conn.close()
    return render_template("network.html", stats=stats, checks=checks, reports=reports)


if __name__ == "__main__":
    conn = get_connection()
    create_tables(conn)
    seed_sample_data(conn)
    conn.close()
    app.run(debug=True, port=5002)
