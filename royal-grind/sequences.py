"""
Royal Grind — cadence automation.

The last word in "Find -> Understand -> Call -> Close -> Automate", and the one
that decides whether the other four compound. A broker who calls fifty companies
and follows up with none of them has run fifty experiments and recorded nothing.

A sequence is a list of steps, each with a day offset and a channel. Enrolling a
target schedules the first step; completing one schedules the next. Steps become
real rows — a task on the rep's list, a drafted email — because a cadence that
lives only as a rule nobody can see is a cadence nobody trusts.

Three rules hold this together:

  1. **A reply ends the sequence.** The moment a prospect connects, books, or
     asks to be left alone, the automation stops and a human owns the account.
     Automation that keeps firing after a human conversation started is how a
     company's domain gets blocked and its brand gets a reputation.

  2. **Steps generate drafts, never sends.** `materialize()` creates an email in
     `draft` status on the rep's queue. Nothing in this module sends anything.
     The gap between "the system wrote it" and "the customer received it" is a
     person, deliberately, and moving that gate is a product decision with legal
     consequences rather than a config flag.

  3. **Weekends do not count.** A three-day follow-up scheduled on a Thursday
     lands on Sunday, and a task due Sunday is a task done Tuesday. Offsets are
     business days.
"""

import json
from datetime import date, datetime, timedelta

# Dispositions that mean a human is now in the loop. Reaching any of these exits
# every sequence the target is enrolled in.
EXIT_DISPOSITIONS = {
    "meeting_set": "meeting booked",
    "connected": "spoke with the prospect",
    "callback": "callback agreed",
    "not_interested": "declined",
    "do_not_call": "asked not to be contacted",
    "bad_number": "number is dead",
    "wrong_number": "wrong person",
}

# Dispositions that should start a cadence if the target is not already in one.
# These are the "nothing happened" outcomes, which are the majority and the
# entire reason the feature exists.
ENTRY_DISPOSITIONS = {"no_answer", "voicemail", "gatekeeper"}


def business_days_from(start, days):
    """Add business days. A follow-up due Sunday is a follow-up done Tuesday."""
    current = start
    remaining = max(int(days), 0)
    if remaining == 0:
        while current.weekday() >= 5:
            current += timedelta(days=1)
        return current
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------

def enroll(conn, org_id, sequence_id, target_id, today=None, commit=True):
    """
    Put a target into a sequence and schedule its first step.

    Idempotent: re-enrolling an active target is a no-op rather than a second
    parallel cadence, which is how a prospect ends up receiving the same email
    twice from the same person.
    """
    today = today or date.today()
    existing = conn.execute(
        """SELECT * FROM enrollments
            WHERE org_id = ? AND sequence_id = ? AND target_id = ?""",
        (org_id, sequence_id, target_id)).fetchone()
    if existing and existing["status"] == "active":
        return existing["id"]

    steps = conn.execute(
        """SELECT * FROM sequence_steps WHERE org_id = ? AND sequence_id = ?
            ORDER BY step_no""", (org_id, sequence_id)).fetchall()
    if not steps:
        return None
    first_due = business_days_from(today, steps[0]["day_offset"])

    if existing:
        conn.execute(
            """UPDATE enrollments SET status = 'active', step_no = 0, exit_reason = '',
                      started_at = ?, next_due_at = ? WHERE id = ?""",
            (_now(), first_due.isoformat(), existing["id"]))
        enrollment_id = existing["id"]
    else:
        enrollment_id = conn.execute(
            """INSERT INTO enrollments (org_id, sequence_id, target_id, step_no,
                                        status, started_at, next_due_at)
               VALUES (?,?,?,?,?,?,?)""",
            (org_id, sequence_id, target_id, 0, "active", _now(),
             first_due.isoformat())).lastrowid

    conn.execute(
        """INSERT INTO activities (org_id, target_id, kind, summary, actor, created_at)
           VALUES (?,?,?,?,?,?)""",
        (org_id, target_id, "system",
         f"Enrolled in sequence: {_sequence_name(conn, org_id, sequence_id)}",
         "system", _now()))
    if commit:
        conn.commit()
    return enrollment_id


def _sequence_name(conn, org_id, sequence_id):
    row = conn.execute("SELECT name FROM sequences WHERE org_id = ? AND id = ?",
                       (org_id, sequence_id)).fetchone()
    return row["name"] if row else "sequence"


def exit_all(conn, org_id, target_id, reason, commit=True):
    """Pull a target out of every sequence. Called the moment a human connects."""
    rows = conn.execute(
        """SELECT * FROM enrollments
            WHERE org_id = ? AND target_id = ? AND status = 'active'""",
        (org_id, target_id)).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE enrollments SET status = 'exited', exit_reason = ?, next_due_at = NULL "
            "WHERE id = ?", (reason, r["id"]))
    if rows:
        # Cancel the work the cadence had already put on the rep's list. Leaving
        # it there means a rep calls someone who booked a meeting yesterday.
        conn.execute(
            """UPDATE tasks SET status = 'cancelled'
                WHERE org_id = ? AND target_id = ? AND status = 'open'
                  AND created_by = 'sequence'""", (org_id, target_id))
        conn.execute(
            """UPDATE emails SET status = 'cancelled'
                WHERE org_id = ? AND target_id = ? AND status = 'draft'
                  AND sequence_step IS NOT NULL""", (org_id, target_id))
        conn.execute(
            """INSERT INTO activities (org_id, target_id, kind, summary, actor, created_at)
               VALUES (?,?,?,?,?,?)""",
            (org_id, target_id, "system",
             f"Exited {len(rows)} sequence(s): {reason}", "system", _now()))
    if commit:
        conn.commit()
    return len(rows)


def on_disposition(conn, org, target_id, disposition, today=None, commit=True):
    """
    React to a call outcome.

    One function so the rule lives in one place: every caller that logs a
    disposition routes through here, and none of them has to remember whether
    `gatekeeper` should exit a sequence.
    """
    org_id = org["id"]
    if disposition in EXIT_DISPOSITIONS:
        return {"action": "exited",
                "count": exit_all(conn, org_id, target_id,
                                  EXIT_DISPOSITIONS[disposition], commit=commit)}

    if disposition in ENTRY_DISPOSITIONS:
        active = conn.execute(
            """SELECT COUNT(*) AS n FROM enrollments
                WHERE org_id = ? AND target_id = ? AND status = 'active'""",
            (org_id, target_id)).fetchone()["n"]
        if active:
            return {"action": "already_enrolled", "count": active}
        seq = conn.execute(
            """SELECT * FROM sequences
                WHERE org_id = ? AND active = 1 AND entry_trigger = ?
                ORDER BY id LIMIT 1""", (org_id, disposition)).fetchone()
        if seq is None:
            seq = conn.execute(
                """SELECT * FROM sequences WHERE org_id = ? AND active = 1
                    AND entry_trigger = 'default' ORDER BY id LIMIT 1""",
                (org_id,)).fetchone()
        if seq is not None:
            enroll(conn, org_id, seq["id"], target_id, today=today, commit=commit)
            return {"action": "enrolled", "sequence": seq["name"]}
    return {"action": "none"}


# ---------------------------------------------------------------------------
# Running the cadence
# ---------------------------------------------------------------------------

def due(conn, org_id, today=None):
    """Enrollments whose next step is due on or before today."""
    today = (today or date.today()).isoformat()
    return conn.execute(
        """SELECT e.*, s.name AS sequence_name, t.rep_id, t.contact_id, t.company_id
             FROM enrollments e
             JOIN sequences s ON s.id = e.sequence_id
             JOIN targets  t ON t.id = e.target_id
            WHERE e.org_id = ? AND e.status = 'active'
              AND e.next_due_at IS NOT NULL AND e.next_due_at <= ?
            ORDER BY e.next_due_at""", (org_id, today)).fetchall()


def materialize(conn, org_id, enrollment, today=None, commit=True):
    """
    Turn the due step into a real row, then schedule the next one.

    Returns a description of what was created, or None if the sequence finished.
    """
    today = today or date.today()
    steps = conn.execute(
        """SELECT * FROM sequence_steps WHERE org_id = ? AND sequence_id = ?
            ORDER BY step_no""", (org_id, enrollment["sequence_id"])).fetchall()
    next_index = enrollment["step_no"]
    if next_index >= len(steps):
        conn.execute("UPDATE enrollments SET status = 'completed', next_due_at = NULL "
                     "WHERE id = ?", (enrollment["id"],))
        if commit:
            conn.commit()
        return None

    step = steps[next_index]
    target_id = enrollment["target_id"]
    created = None

    if step["channel"] == "email":
        # A draft, on the rep's queue. See rule 2 in the module docstring.
        conn.execute(
            """INSERT INTO emails (org_id, target_id, rep_id, subject, body, status,
                                   engine, sequence_step, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (org_id, target_id, enrollment["rep_id"], step["title"],
             step["guidance"] or "", "draft", "sequence", step["step_no"], _now()))
        created = f"Email draft queued: {step['title']}"
    else:
        conn.execute(
            """INSERT INTO tasks (org_id, target_id, rep_id, kind, title, detail,
                                  due_at, status, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (org_id, target_id, enrollment["rep_id"], step["channel"], step["title"],
             step["guidance"] or "", today.isoformat(), "open", "sequence", _now()))
        created = f"Task created: {step['title']}"

    # Schedule the next step from today rather than from the original start, so a
    # cadence that ran late does not fire three steps at once to catch up.
    step_no = next_index + 1
    if step_no < len(steps):
        gap = steps[step_no]["day_offset"] - step["day_offset"]
        next_due = business_days_from(today, max(gap, 1)).isoformat()
        status = "active"
    else:
        next_due, status = None, "completed"

    conn.execute(
        "UPDATE enrollments SET step_no = ?, next_due_at = ?, status = ? WHERE id = ?",
        (step_no, next_due, status, enrollment["id"]))
    conn.execute(
        """INSERT INTO activities (org_id, target_id, rep_id, kind, summary, actor, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (org_id, target_id, enrollment["rep_id"], "system",
         f"{enrollment['sequence_name']} step {step['step_no']}: {created}",
         "system", _now()))
    if commit:
        conn.commit()
    return created


def run(conn, org_id, today=None, limit=500):
    """Materialize every due step for one org. The cron entry point."""
    done = []
    for enrollment in due(conn, org_id, today)[:limit]:
        created = materialize(conn, org_id, enrollment, today, commit=False)
        if created:
            done.append({"target_id": enrollment["target_id"],
                         "sequence": enrollment["sequence_name"], "created": created})
    conn.commit()
    return done


def summary(conn, org_id):
    """Per-sequence counts for the sequences page."""
    # Every computed column is suffixed. `sequences` already has a column called
    # `active` (the on/off switch), and `SELECT s.*, (...) AS active` returns two
    # columns of that name — the row object keeps the first, so the enrollment
    # count silently reads back as the on/off flag.
    rows = conn.execute(
        """SELECT s.*,
                  (SELECT COUNT(*) FROM enrollments e
                    WHERE e.sequence_id = s.id AND e.status = 'active') AS active_count,
                  (SELECT COUNT(*) FROM enrollments e
                    WHERE e.sequence_id = s.id AND e.status = 'completed') AS completed_count,
                  (SELECT COUNT(*) FROM enrollments e
                    WHERE e.sequence_id = s.id AND e.status = 'exited') AS exited_count,
                  (SELECT COUNT(*) FROM sequence_steps st
                    WHERE st.sequence_id = s.id) AS step_count
             FROM sequences s WHERE s.org_id = ? ORDER BY s.id""", (org_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        total = d["completed_count"] + d["exited_count"]
        # An exit is a success — it means somebody answered. A cadence where
        # everyone completes every step is a cadence nobody replied to.
        d["reply_rate"] = d["exited_count"] / total if total else 0
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import grind_db

    conn = grind_db.get_connection()
    if "--run" in sys.argv:
        for org in conn.execute("SELECT id, name FROM orgs WHERE status = 'active'").fetchall():
            done = run(conn, org["id"])
            print(f"{org['name']}: {len(done)} step(s) materialized")
            for d in done[:20]:
                print(f"    {d['sequence']} — {d['created']}")
    else:
        print(__doc__)
        print("\nUsage:\n  python3 sequences.py --run     # materialize due steps (cron)")
    conn.close()
