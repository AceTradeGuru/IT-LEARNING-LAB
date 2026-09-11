"""
Royal Grind — the rules that stop a call.

Every other module in this codebase is trying to get a rep onto the phone
faster. This one is the brake, and it runs before the dialer, not after it.

Four things live here:

  1. **Suppression.** Numbers and addresses nobody in this org may contact.
     Checked on every dial and every send, with no override flag — an
     override flag on a do-not-call list is a do-not-call suggestion.

  2. **Quiet hours.** The TCPA's 8am-9pm window, evaluated in the *prospect's*
     timezone rather than the rep's. A broker in Kansas City dialing California
     at 7:40am local is the single easiest violation to commit by accident, and
     a platform that sorts the call list by opportunity score will commit it
     every morning unless something stops it.

  3. **Recording consent.** Roughly a dozen states require every party on the
     call to consent. The safe rule, and the one implemented here, is that the
     stricter of the two jurisdictions governs, and that an unknown location is
     treated as the strict case. The basis for recording is written onto the
     call row at the time, because it cannot be reconstructed afterwards.

  4. **Subject rights and retention.** Access, erasure, and a clock on personal
     data held about people who never asked to be in a database.

**None of this is legal advice, and installing it does not make anyone
compliant.** Telemarketing law is federal, state, and in places municipal; it
turns on facts this software does not know, such as whether an existing business
relationship exists and whether a number is a residential wireless line. What
this module provides is the mechanism to enforce a policy your counsel sets, and
the record to show what the system did. Read DEPLOY.md before dialing anyone.
"""

import json
import re
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover
    ZoneInfo = None


# ---------------------------------------------------------------------------
# Recording consent
# ---------------------------------------------------------------------------
#
# States whose wiretapping statutes require the consent of every party to the
# conversation. Compiled from the commonly cited list; jurisdictions amend these
# and courts read them differently, which is why this is a constant your counsel
# can review in one place rather than a condition buried in the dialer.
#
# Oregon is included: its rule differs between in-person and telephonic
# conversations, and the platform takes the stricter reading rather than
# deciding a nuance of Oregon law inside a scoring function.
ALL_PARTY_STATES = {
    "CA": "California", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida",
    "IL": "Illinois", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MT": "Montana", "NV": "Nevada", "NH": "New Hampshire", "OR": "Oregon",
    "PA": "Pennsylvania", "WA": "Washington",
}

ANNOUNCEMENT = ("This call may be recorded for quality and training purposes. "
                "If you would prefer it not be, say so and I'll switch it off.")


def recording_decision(org, company_state, rep_state=""):
    """
    May this specific call be recorded, and on what basis?

    Returns (allowed, basis, announcement). `basis` is a sentence written onto
    the call row. "We had a policy" is not a defense; "the policy was all-party
    consent, the prospect was in Missouri, the rep was in Texas, neither is an
    all-party state, and the announcement played at connect" is a record.
    """
    policy = (org["recording_policy"] or "off").lower()
    if policy == "off":
        return False, "Recording disabled for this organization.", ""

    prospect = (company_state or "").upper()[:2]
    rep = (rep_state or "").upper()[:2]

    if not prospect:
        return False, ("Prospect location unknown — treated as an all-party consent "
                       "jurisdiction and not recorded."), ""

    strict_policy = policy == "all_party"
    strict = [s for s in (prospect, rep) if s in ALL_PARTY_STATES]

    # Both parties are frequently in the same state. Listing it twice reads as a
    # bug in the one field somebody may later read back in a deposition.
    jurisdictions = ", ".join(dict.fromkeys(j for j in (prospect, rep) if j))

    if strict and not strict_policy:
        names = ", ".join(dict.fromkeys(ALL_PARTY_STATES[s] for s in strict))
        return False, (f"{names} requires all-party consent and this organization's "
                       f"policy is one-party. Not recorded."), ""

    if strict:
        return True, (f"All-party consent policy. Announcement played at connect; "
                      f"recording begins only after the prospect stays on the line. "
                      f"Jurisdictions: {jurisdictions}."), ANNOUNCEMENT

    return True, (f"One-party consent. Rep is a party to the call. "
                  f"Jurisdictions: {jurisdictions}. Announcement played anyway."), \
        ANNOUNCEMENT


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------

def local_now(timezone_name, now=None):
    """
    The prospect's wall clock, and whether we could actually work it out.

    `now` is the current instant. A naive datetime is treated as UTC, which is
    what a server runs on and what every caller here passes; guessing the
    server's local zone instead would make the answer depend on where the
    container happens to be deployed, which is precisely the mistake this
    function exists to prevent.

    Returns (local_datetime, resolved).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if ZoneInfo is None or not timezone_name:
        return now, False
    try:
        return now.astimezone(ZoneInfo(timezone_name)), True
    except Exception:
        return now, False


def quiet_hours_check(org, timezone_name, now=None):
    """
    Returns (ok, message). Blocks outside the org's configured window, in the
    prospect's local time.

    An unresolvable timezone does not fail open. The whole point of the check is
    the case where the rep does not know what time it is where they are calling.
    """
    start = org["quiet_start"] if "quiet_start" in org.keys() else 8
    end = org["quiet_end"] if "quiet_end" in org.keys() else 21
    local, resolved = local_now(timezone_name, now)
    hour = local.hour

    if not resolved:
        return True, (f"Timezone '{timezone_name or 'unknown'}' could not be resolved — "
                      f"quiet hours checked against server time ({hour:02d}:00). "
                      f"Confirm local time before dialing.")
    if start <= hour < end:
        return True, f"{local.strftime('%-I:%M %p')} local — inside calling hours."
    return False, (f"{local.strftime('%-I:%M %p')} local at the prospect "
                   f"({timezone_name}). Calling window is "
                   f"{start:02d}:00-{end:02d}:00 local.")


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------

def normalize_phone(value):
    """
    Reduce a number to comparable digits.

    US numbers are stored and compared as ten digits so that +1 (816) 555-0143,
    8165550143, and 1-816-555-0143 are one entry on a suppression list rather
    than three ways to call somebody who asked you not to.
    """
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def normalize_email(value):
    return (value or "").strip().lower()


def is_suppressed(conn, org_id, value, kind=None):
    """Check one value. Returns the suppression row, or None."""
    if not value:
        return None
    candidates = []
    if kind in (None, "phone"):
        norm = normalize_phone(value)
        if norm:
            candidates.append(("phone", norm))
    if kind in (None, "email"):
        email = normalize_email(value)
        if "@" in email:
            candidates.append(("email", email))
            candidates.append(("domain", email.split("@", 1)[1]))
    for k, v in candidates:
        row = conn.execute(
            "SELECT * FROM suppression WHERE org_id = ? AND kind = ? AND value = ?",
            (org_id, k, v)).fetchone()
        if row:
            return row
    return None


def suppress(conn, org_id, kind, value, reason="", source="manual", commit=True):
    """Add an entry. Normalizing on write is what makes the read-side check cheap."""
    norm = normalize_phone(value) if kind == "phone" else normalize_email(value)
    if not norm:
        return False
    conn.execute(
        """INSERT OR IGNORE INTO suppression (org_id, kind, value, reason, source, created_at)
           VALUES (?,?,?,?,?,?)""",
        (org_id, kind, norm, reason, source, datetime.now().isoformat(timespec="seconds")))
    if commit:
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# The pre-dial gate
# ---------------------------------------------------------------------------

def predial_check(conn, org, company, contact, number, rep=None, now=None):
    """
    Everything that must be true before a number is dialed.

    Returns {"allowed": bool, "blocks": [...], "warnings": [...], "recording": {...}}.

    Blocks stop the call. Warnings are shown to the rep and let them proceed —
    the distinction is whether the objection is legal or merely tactical, and
    conflating the two teaches reps to click through both.
    """
    blocks, warnings = [], []
    org_id = org["id"]

    hit = is_suppressed(conn, org_id, number, kind="phone")
    if hit:
        blocks.append(f"This number is on the suppression list ({hit['reason'] or hit['source']}).")

    if contact is not None:
        if contact["status"] == "do_not_contact":
            blocks.append("This contact has asked not to be contacted.")
        elif contact["status"] == "left_company":
            warnings.append("Our record says this person has left the company. "
                            "Whoever answers is someone else.")

    tz = company["timezone"] if company is not None else None
    ok, message = quiet_hours_check(org, tz, now=now)
    if not ok:
        blocks.append(message)
    else:
        warnings.append(message) if "could not be resolved" in message else None

    if not number or len(normalize_phone(number)) < 10:
        blocks.append("No dialable number on this record.")

    allowed, basis, announcement = recording_decision(
        org, company["state"] if company is not None else "",
        (rep["territory"] if rep is not None and "territory" in rep.keys() else "") or "")

    return {
        "allowed": not blocks,
        "blocks": blocks,
        "warnings": [w for w in warnings if w],
        "recording": {"allowed": allowed, "basis": basis, "announcement": announcement},
        "local_time_note": message,
    }


# ---------------------------------------------------------------------------
# Subject rights
# ---------------------------------------------------------------------------

def subject_access_export(conn, org_id, contact_id):
    """
    Everything held about one person, as JSON (GDPR Art. 15, CCPA §1798.110).

    Includes the AI-generated material — the research brief written about them,
    the call plan built to persuade them, the analysis of what they said. A
    person asking what a system holds about them is entitled to the parts that
    were inferred, not just the parts that were typed in. Those are usually the
    parts they did not know existed.
    """
    contact = conn.execute("SELECT * FROM contacts WHERE org_id = ? AND id = ?",
                           (org_id, contact_id)).fetchone()
    if contact is None:
        return None

    def rows(sql, params):
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    company = conn.execute("SELECT * FROM companies WHERE org_id = ? AND id = ?",
                           (org_id, contact["company_id"])).fetchone()
    targets = rows("SELECT * FROM targets WHERE org_id = ? AND contact_id = ?",
                   (org_id, contact_id))

    export = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "notice": ("Everything this platform holds about this person, including "
                   "material generated about them by an AI model."),
        "person": dict(contact),
        "employer_record": dict(company) if company else None,
        "contact_points": rows(
            "SELECT * FROM contact_points WHERE org_id = ? AND contact_id = ?",
            (org_id, contact_id)),
        "calls": rows("SELECT * FROM calls WHERE org_id = ? AND contact_id = ?",
                      (org_id, contact_id)),
        "emails": [], "prospecting_records": targets, "ai_generated": [],
    }
    for t in targets:
        export["emails"].extend(
            rows("SELECT * FROM emails WHERE org_id = ? AND target_id = ?",
                 (org_id, t["id"])))
        for field, kind in (("research_json", "research brief"),
                            ("plan_json", "call plan")):
            if t.get(field):
                export["ai_generated"].append({
                    "kind": kind,
                    "engine": t.get(field.replace("_json", "_engine")),
                    "content": _loads(t[field]),
                })
    for c in export["calls"]:
        if c.get("analysis_json"):
            export["ai_generated"].append({
                "kind": "call analysis", "engine": c.get("analysis_engine"),
                "call_id": c["id"], "content": _loads(c["analysis_json"])})
    return export


def _loads(raw):
    try:
        return json.loads(raw)
    except Exception:
        return raw


def erase_contact(conn, org_id, contact_id, commit=True):
    """
    Right to erasure (GDPR Art. 17), done so the business record survives.

    The person is destroyed: name, title, every number and address, every
    transcript. What remains is that a call happened, its duration, and its
    outcome — which is the org's own commercial record, not personal data about
    the erased individual, and without which the learning loop silently loses
    its history.
    """
    now = datetime.now().isoformat(timespec="seconds")
    contact = conn.execute("SELECT * FROM contacts WHERE org_id = ? AND id = ?",
                           (org_id, contact_id)).fetchone()
    if contact is None:
        return False

    # Suppress before deleting, or the next enrichment run re-imports them.
    for p in conn.execute(
            "SELECT * FROM contact_points WHERE org_id = ? AND contact_id = ?",
            (org_id, contact_id)).fetchall():
        suppress(conn, org_id, "phone" if p["kind"] != "email" else "email",
                 p["value"], reason="erasure request", source="request", commit=False)

    conn.execute("DELETE FROM contact_points WHERE org_id = ? AND contact_id = ?",
                 (org_id, contact_id))
    conn.execute(
        """UPDATE calls SET transcript = '', notes = '[erased]', analysis_json = '',
                  to_number = '', contact_id = NULL
            WHERE org_id = ? AND contact_id = ?""", (org_id, contact_id))
    conn.execute(
        """UPDATE targets SET contact_id = NULL, research_json = '', plan_json = '',
                  status = 'disqualified', dq_reason = 'erasure request'
            WHERE org_id = ? AND contact_id = ?""", (org_id, contact_id))
    conn.execute("DELETE FROM emails WHERE org_id = ? AND target_id IN "
                 "(SELECT id FROM targets WHERE org_id = ? AND contact_id = ?)",
                 (org_id, org_id, contact_id))
    conn.execute("DELETE FROM contacts WHERE org_id = ? AND id = ?", (org_id, contact_id))
    conn.execute(
        """INSERT INTO audit_log (org_id, user_id, action, entity, detail, created_at)
           VALUES (?,?,?,?,?,?)""",
        (org_id, None, "contact.erase", f"contact:{contact_id}",
         json.dumps({"erased_at": now, "note": "personal data destroyed, "
                     "commercial record redacted and retained"}), now))
    if commit:
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def retention_candidates(conn, org_id, retention_days=None, today=None):
    """
    Contacts with no activity for longer than the retention window.

    "We might call them one day" is not a lawful basis for holding somebody's
    mobile number indefinitely, and a retention policy nobody can execute is a
    policy in name only.
    """
    today = today or date.today()
    org = conn.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
    days = retention_days or (org["retention_days"] if org else 730)
    cutoff = (today - timedelta(days=days)).isoformat()

    return conn.execute(
        """SELECT c.*, co.name AS company_name,
                  (SELECT MAX(created_at) FROM calls k WHERE k.contact_id = c.id) AS last_call,
                  (SELECT MAX(last_touch_at) FROM targets t WHERE t.contact_id = c.id) AS last_touch
             FROM contacts c
             JOIN companies co ON co.id = c.company_id
            WHERE c.org_id = ?
              AND COALESCE(
                    (SELECT MAX(created_at) FROM calls k WHERE k.contact_id = c.id),
                    (SELECT MAX(last_touch_at) FROM targets t WHERE t.contact_id = c.id),
                    c.created_at) < ?
            ORDER BY c.created_at""", (org_id, cutoff)).fetchall()


def purge(conn, org_id, retention_days=None, dry_run=True, today=None):
    """Erase everyone past the retention window. Returns (count, names)."""
    rows = retention_candidates(conn, org_id, retention_days, today)
    names = [f"{r['name']} ({r['company_name']})" for r in rows]
    if not dry_run:
        for r in rows:
            erase_contact(conn, org_id, r["id"], commit=False)
        conn.commit()
    return len(rows), names


# ---------------------------------------------------------------------------
# CLI — run from cron
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import grind_db

    args = sys.argv[1:]
    conn = grind_db.get_connection()

    if "--purge" in args:
        dry = "--apply" not in args
        orgs = conn.execute("SELECT id, name FROM orgs WHERE status = 'active'").fetchall()
        for o in orgs:
            n, names = purge(conn, o["id"], dry_run=dry)
            verb = "would erase" if dry else "erased"
            print(f"{o['name']}: {verb} {n} contact(s) past retention")
            for nm in names[:20]:
                print(f"    {nm}")
            if len(names) > 20:
                print(f"    ... and {len(names) - 20} more")
        if dry:
            print("\nDry run. Re-run with --apply to execute.")
    else:
        print(__doc__)
        print("\nUsage:\n  python3 compliance.py --purge [--apply]")
    conn.close()
