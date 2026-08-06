"""
CarrierGuard — the risk engine.

This is the product. Everything else is plumbing around it.

A broker is about to hand $80,000 of someone else's freight to a stranger who
called them twenty minutes ago. The stranger gave an MC number. The MC number
looks fine in SAFER, because the MC number belongs to a real, clean carrier —
it just doesn't belong to the person on the phone.

So we never ask "is this MC number good?" We ask three questions at once:

    1. FMCSA record  — what does the government say about this authority?
    2. Identity      — do the contact details match the carrier of record?
    3. Network       — what has this MC number done to other brokers lately?

Fraud shows up in the DISAGREEMENT between those three, which is why a
single-source lookup tool cannot catch it and why this gets harder to beat
the more brokers are on the network.

Every signal carries an explicit weight so the verdict is defensible:
a broker can show their boss exactly which points came from where.
"""

from collections import namedtuple
from datetime import UTC, date, datetime

# A fired risk signal. `weight` is added to the risk score; negative weights
# are mitigating evidence (a carrier can earn its way down).
Signal = namedtuple("Signal", "code label detail weight severity source")

# Verdict thresholds. Deliberately conservative: in freight, a false "review"
# costs a broker five minutes, a false "clear" costs them a truckload.
BLOCK_AT = 60
REVIEW_AT = 30

# Consumer mailbox providers. A real carrier with its own domain that suddenly
# dispatches from a free inbox is worth a second look.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "protonmail.com", "mail.com", "gmx.com", "yandex.com",
}

NEW_AUTHORITY_DAYS = 180      # under 6 months = the most-abused vector
DORMANT_DAYS = 365            # active authority, no inspections = revived shell
INSURANCE_WARN_DAYS = 30
HIGH_OOS_RATE = 0.20          # roughly 1.5x the national vehicle OOS average
CONTACT_CHURN_WINDOW = 30     # days
CONTACT_CHURN_EMAILS = 3      # distinct emails inside the window
CONTACT_CHURN_BROKERS = 2     # ...seen by at least this many brokers


# ---------------------------------------------------------------- helpers

def normalize_phone(raw):
    """'(313) 555-0188' -> '3135550188'. Strips a leading US country code."""
    if not raw:
        return ""
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def email_domain(email):
    """'Dispatch@WWW.Acme.com ' -> 'acme.com'."""
    if not email or "@" not in email:
        return ""
    domain = email.strip().lower().rsplit("@", 1)[1]
    return domain[4:] if domain.startswith("www.") else domain


def levenshtein(a, b):
    """Edit distance. Small enough to keep in-repo; no dependency needed."""
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,          # deletion
                           cur[j - 1] + 1,       # insertion
                           prev[j - 1] + (ca != cb)))  # substitution
        prev = cur
    return prev[-1]


def _days_since(value):
    """
    Days between a stored date and today. None if the date is missing.

    Postgres hands back real `date` objects, so there is no parsing here — but
    `datetime` is accepted too, since timestamp columns pass through the same
    helpers.
    """
    if not value:
        return None
    return (date.today() - _as_date(value)).days


def _days_until(value):
    if not value:
        return None
    return (_as_date(value) - date.today()).days


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


# ---------------------------------------------------------------- the rules

def _authority_signals(carrier):
    """What the government record says about the right to haul freight."""
    out = []
    status = carrier["authority_status"]

    if status != "active":
        out.append(Signal(
            "AUTHORITY_NOT_ACTIVE",
            f"Operating authority is {status.upper()}",
            f"FMCSA lists this authority as {status}. This carrier cannot legally "
            f"haul interstate freight today.",
            45, "critical", "fmcsa"))

    age = _days_since(carrier["authority_granted"])
    if age is not None and age < NEW_AUTHORITY_DAYS:
        out.append(Signal(
            "AUTHORITY_NEW",
            "Authority is less than 6 months old",
            f"Granted {carrier['authority_granted']} ({age} days ago). Newly issued "
            f"authorities are the most commonly abused entry point for freight fraud.",
            20, "high", "fmcsa"))
    elif age is not None and age > 1825 and carrier["reinstatement_count"] == 0:
        out.append(Signal(
            "LONG_TENURE",
            f"Authority active and unbroken for {age // 365} years",
            f"Continuously authorized since {carrier['authority_granted']} with no "
            f"revocations.",
            -10, "good", "fmcsa"))

    if carrier["reinstatement_count"] >= 2:
        out.append(Signal(
            "AUTHORITY_CHURN",
            "Authority revoked and reinstated repeatedly",
            f"{carrier['reinstatement_count']} reinstatements on record — a pattern of "
            f"lapsed insurance or unpaid penalties.",
            18, "high", "fmcsa"))

    idle = _days_since(carrier["last_inspection"])
    if status == "active" and (idle is None or idle > DORMANT_DAYS):
        seen = f"{idle} days ago" if idle is not None else "never"
        out.append(Signal(
            "DORMANT_REVIVED",
            "Active authority with no recent roadside activity",
            f"Last inspection: {seen}. A dormant authority that suddenly starts booking "
            f"freight is a classic sign the MC number changed hands.",
            16, "high", "safety"))

    return out


def _insurance_signals(carrier, load_value):
    """Insurance is the broker's only recovery path when a load disappears."""
    out = []
    for kind, column in (("Cargo", "cargo_expires"), ("Liability", "liability_expires")):
        remaining = _days_until(carrier[column])
        if remaining is None:
            continue
        if remaining < 0:
            out.append(Signal(
                f"{kind.upper()}_EXPIRED",
                f"{kind} insurance has EXPIRED",
                f"Expired {carrier[column]} ({abs(remaining)} days ago). A claim on this "
                f"load would not be covered.",
                40, "critical", "insurance"))
        elif remaining <= INSURANCE_WARN_DAYS:
            out.append(Signal(
                f"{kind.upper()}_EXPIRING",
                f"{kind} insurance expires in {remaining} days",
                f"Policy lapses {carrier[column]} — before or near delivery.",
                12, "medium", "insurance"))

    coverage = carrier["cargo_coverage_usd"] or 0
    if load_value and coverage and coverage < load_value:
        out.append(Signal(
            "CARGO_UNDERINSURED",
            "Cargo coverage is below the value of this load",
            f"Coverage ${coverage:,} vs. load value ${load_value:,}. You are exposed for "
            f"${load_value - coverage:,}.",
            14, "high", "insurance"))

    return out


def _safety_signals(carrier):
    out = []
    # NUMERIC comes back as Decimal; float() keeps the comparison and the
    # formatting below from depending on mixed-type arithmetic.
    oos = float(carrier["oos_rate"] or 0)
    if oos > HIGH_OOS_RATE:
        out.append(Signal(
            "HIGH_OOS_RATE",
            "Out-of-service rate well above national average",
            f"{oos * 100:.0f}% of inspections resulted in an out-of-service "
            f"order (national average is roughly 13%).",
            14, "high", "safety"))
    return out


def _identity_signals(carrier, contact_email, contact_phone, phone_type):
    """
    The impersonation check, and the reason this product exists.

    Carrier identity theft works because the MC number is real and clean. The
    only thing that is wrong is WHO is using it — so the only way to catch it
    is to compare the inbound contact details against the carrier of record.
    """
    out = []
    inbound_domain = email_domain(contact_email)
    registered_domain = (carrier["registered_domain"] or "").lower()

    if inbound_domain and registered_domain:
        if inbound_domain != registered_domain:
            distance = levenshtein(inbound_domain, registered_domain)
            if distance <= 2:
                out.append(Signal(
                    "LOOKALIKE_DOMAIN",
                    "Contact email uses a LOOKALIKE of the carrier's real domain",
                    f"They wrote from @{inbound_domain}. The carrier of record is "
                    f"@{registered_domain} — {distance} character(s) different. This is "
                    f"impersonation, not a typo on your end.",
                    35, "critical", "identity"))
            elif inbound_domain in FREE_EMAIL_DOMAINS:
                out.append(Signal(
                    "FREE_EMAIL_DOMAIN",
                    "Dispatch is using a free consumer inbox",
                    f"@{inbound_domain} instead of the carrier's own @{registered_domain}.",
                    12, "medium", "identity"))
            else:
                out.append(Signal(
                    "EMAIL_DOMAIN_MISMATCH",
                    "Contact email domain does not match the carrier of record",
                    f"They wrote from @{inbound_domain}; FMCSA and insurance filings list "
                    f"@{registered_domain}.",
                    18, "high", "identity"))

    inbound_phone = normalize_phone(contact_phone)
    registered_phone = normalize_phone(carrier["registered_phone"])
    if inbound_phone and registered_phone and inbound_phone != registered_phone:
        out.append(Signal(
            "PHONE_MISMATCH",
            "Callback number does not match the carrier of record",
            f"They called from {_fmt_phone(inbound_phone)}; the number on file is "
            f"{_fmt_phone(registered_phone)}. Verify by calling the number on file — "
            f"never the one you were given.",
            15, "high", "identity"))

    if phone_type == "voip":
        out.append(Signal(
            "VOIP_CONTACT_NUMBER",
            "Contact number is a VOIP line",
            f"{_fmt_phone(inbound_phone)} is a voice-over-IP number — provisioned in "
            f"minutes, discarded in minutes, and untraceable to a physical yard.",
            12, "medium", "identity"))

    if carrier["address_type"] == "cmra":
        out.append(Signal(
            "CMRA_ADDRESS",
            "Principal address is a commercial mailbox",
            f"{carrier['registered_address']} resolves to a mail-drop store, not a "
            f"terminal or yard.",
            14, "high", "identity"))
    elif carrier["address_type"] == "residential":
        out.append(Signal(
            "RESIDENTIAL_ADDRESS",
            "Principal address is residential",
            f"Normal for a genuine one-truck owner-operator; worth confirming against "
            f"the power unit count ({carrier['power_units']}).",
            8, "low", "identity"))

    return out


def _network_signals(conn, mc_number, contact_email, contact_phone, broker_id):
    """
    What the rest of the network already knows. None of this is purchasable —
    it exists only because other brokers ran their checks here first.
    """
    out = []

    reports = conn.execute(
        """SELECT * FROM fraud_reports WHERE mc_number = %s
           ORDER BY verified DESC, created_at DESC""",
        (mc_number,)).fetchall()

    verified = [r for r in reports if r["verified"]]
    unverified = [r for r in reports if not r["verified"]]

    if verified:
        lost = sum(r["amount_lost_usd"] or 0 for r in verified)
        kinds = sorted({r["incident_type"].replace("_", " ") for r in verified})
        out.append(Signal(
            "NETWORK_FRAUD_VERIFIED",
            f"CONFIRMED fraud on this MC number ({len(verified)} report(s))",
            f"Verified incidents: {', '.join(kinds)}. ${lost:,} lost by brokers on this "
            f"network. The FMCSA record for this carrier is clean — only the network "
            f"knows.",
            50, "critical", "network"))

    if unverified:
        out.append(Signal(
            "NETWORK_FRAUD_UNVERIFIED",
            f"{len(unverified)} unconfirmed fraud report(s) from other brokers",
            "Reported but not yet verified by CarrierGuard ops. Treat as smoke, not fire.",
            22, "high", "network"))

    # Contact churn: one MC number wearing several different faces at once.
    # The window is a bound parameter, not string-interpolated SQL.
    churn = conn.execute(
        """SELECT COUNT(DISTINCT contact_email) AS emails,
                  COUNT(DISTINCT broker_id)     AS brokers
           FROM contact_observations
           WHERE mc_number = %s
             AND contact_email <> ''
             AND last_seen >= now() - make_interval(days => %s)""",
        (mc_number, CONTACT_CHURN_WINDOW)).fetchone()

    if (churn["emails"] >= CONTACT_CHURN_EMAILS
            and churn["brokers"] >= CONTACT_CHURN_BROKERS):
        out.append(Signal(
            "CONTACT_CHURN",
            "This MC number is presenting different identities to different brokers",
            f"{churn['emails']} distinct dispatch emails shown to {churn['brokers']} "
            f"brokers in the last {CONTACT_CHURN_WINDOW} days. Legitimate dispatch "
            f"contacts do not rotate like this.",
            25, "critical", "network"))

    # Standing history for this exact contact, excluding the check in progress.
    seen = conn.execute(
        """SELECT SUM(times_seen) AS hits, COUNT(DISTINCT broker_id) AS brokers,
                  MIN(first_seen) AS since
           FROM contact_observations
           WHERE mc_number = %s AND contact_email = %s""",
        (mc_number, (contact_email or "").strip().lower())).fetchone()

    if seen["hits"] and seen["hits"] >= 3 and not reports:
        out.append(Signal(
            "CONTACT_ESTABLISHED",
            "This exact contact has a clean history on the network",
            f"Seen {seen['hits']} times by {seen['brokers']} broker(s) since "
            f"{_as_date(seen['since'])}, with no fraud reports.",
            -12, "good", "network"))

    return out


def _fmt_phone(digits):
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return digits or "—"


# ---------------------------------------------------------------- public API

def evaluate(conn, broker_id, mc_number, contact_email="", contact_phone="",
             load_value=None):
    """
    Score one carrier against one inbound contact. Returns a dict:
        carrier, signals (list[Signal]), score (0-100), verdict, band
    Does not write anything — call run_check() to score and persist.
    """
    mc_number = (mc_number or "").strip().upper()
    contact_email = (contact_email or "").strip().lower()
    phone_digits = normalize_phone(contact_phone)

    carrier = conn.execute(
        "SELECT * FROM carriers WHERE mc_number = %s", (mc_number,)).fetchone()

    signals = []

    if carrier is None:
        # No matching authority at all. Either a typo or an invented MC number,
        # and both mean: do not tender this load.
        signals.append(Signal(
            "AUTHORITY_NOT_FOUND",
            "No operating authority found for this MC number",
            f"{mc_number} does not appear in the FMCSA registry snapshot. Confirm the "
            f"number, and treat any pressure to skip this step as the tell it is.",
            55, "critical", "fmcsa"))
    else:
        phone_type = "unknown"
        if phone_digits:
            row = conn.execute(
                "SELECT line_type FROM phone_intel WHERE phone = %s",
                (phone_digits,)).fetchone()
            if row:
                phone_type = row["line_type"]

        signals += _authority_signals(carrier)
        signals += _insurance_signals(carrier, load_value)
        signals += _safety_signals(carrier)
        signals += _identity_signals(carrier, contact_email, phone_digits, phone_type)

    signals += _network_signals(conn, mc_number, contact_email, phone_digits, broker_id)

    # Mitigating evidence can pull a score down, but never below zero, and a
    # single critical finding is never argued away by good history.
    score = max(0, min(100, sum(s.weight for s in signals)))
    if any(s.severity == "critical" and s.weight > 0 for s in signals):
        score = max(score, BLOCK_AT)

    verdict = "block" if score >= BLOCK_AT else "review" if score >= REVIEW_AT else "clear"

    # Worst-first, so the reason for the verdict is the first thing read.
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "good": 4}
    signals.sort(key=lambda s: (order[s.severity], -abs(s.weight)))

    return {"carrier": carrier, "signals": signals, "score": score, "verdict": verdict}


def run_check(conn, broker_id, mc_number, contact_email="", contact_phone="",
              contact_name="", load_value=None, at=None):
    """
    Score a carrier, persist the check and its signals, and update the
    network's contact memory. Returns (check_id, result).

    `at` backdates the record (used to seed a believable history).

    Does not commit — the caller's `db.connection()` block owns the transaction,
    so a failure partway through leaves no check row with missing signals behind.
    """
    mc_number = (mc_number or "").strip().upper()
    contact_email = (contact_email or "").strip().lower()
    phone_digits = normalize_phone(contact_phone)
    stamp = at or datetime.now(UTC)

    result = evaluate(conn, broker_id, mc_number, contact_email, phone_digits, load_value)

    cur = conn.cursor()
    cur.execute(
        """INSERT INTO checks (broker_id, mc_number, contact_name, contact_email,
                               contact_phone, load_value_usd, risk_score, verdict, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (broker_id, mc_number, contact_name.strip() or None, contact_email or None,
         phone_digits or None, load_value, result["score"], result["verdict"], stamp))
    check_id = cur.fetchone()["id"]

    if result["signals"]:
        cur.executemany(
            """INSERT INTO check_signals
                   (check_id, code, label, detail, weight, severity, source)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [(check_id, s.code, s.label, s.detail, s.weight, s.severity, s.source)
             for s in result["signals"]])

    # Remember this contact — it is what makes the next broker's check smarter.
    if contact_email or phone_digits:
        cur.execute(
            """INSERT INTO contact_observations
                   (mc_number, contact_email, contact_phone, broker_id, first_seen, last_seen)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (mc_number, contact_email, contact_phone, broker_id)
               DO UPDATE SET times_seen = contact_observations.times_seen + 1,
                             last_seen  = EXCLUDED.last_seen""",
            (mc_number, contact_email, phone_digits, broker_id, stamp, stamp))

    return check_id, result
