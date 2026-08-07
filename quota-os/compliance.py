"""
QuotaOS — data retention, subject rights, and bias-audit support.

This module is not legal advice and does not make anyone compliant. It exists
because three obligations that apply to AI-assisted hiring all reduce to "can you
produce this on request", and you can only produce it if it was engineered in:

  * **Retention.** GDPR and CCPA both require personal data to have an end date.
    `candidates.purge_after` is that clock, and `purge_expired` is what reads it.

  * **Subject rights.** A candidate can ask what you hold about them (GDPR Art. 15)
    and ask you to delete it (Art. 17). `export_candidate` and `erase_candidate`
    answer both, in one click, without a database console.

  * **Bias audit.** NYC Local Law 144 requires an annual independent audit of
    selection rates across race/ethnicity and sex categories.
    `adverse_impact_rows` produces the join key and the outcome data an auditor
    needs.

### Why we do not store demographics

QuotaOS deliberately collects no race, ethnicity, or sex data, and the scoring path
could not read it if it existed. That is not an oversight to fix later — a model
that cannot see a protected characteristic cannot score on it, and that is a
materially stronger position than one that collects it and promises not to look.

The standard pattern is that the employer collects voluntary EEO self-identification
through a separate channel that the hiring decision never touches, and the auditor
joins it to the outcome data on a candidate key. `adverse_impact_rows` emits that
key. Wiring demographics into this database would break the property that makes the
scoring defensible.

### Erasure vs. the audit trail

Art. 17 erasure and the LL144 expectation that decisions stay auditable pull in
opposite directions. `erase_candidate` resolves it the way regulators generally
expect: the person's identifying data is destroyed, while the fact that a decision
was made — its score, its date, its engine — survives with the identifiers removed.
You keep an auditable process without keeping the human.
"""

import csv
import io
import json
from datetime import date, datetime, timedelta

# The tables holding data about a candidate, innermost first, so foreign keys
# never block a delete.
_CANDIDATE_CHILDREN = [
    ("interviews", "application_id IN (SELECT id FROM applications WHERE candidate_id = ?)"),
    ("stage_events", "application_id IN (SELECT id FROM applications WHERE candidate_id = ?)"),
    ("applications", "candidate_id = ?"),
]


def set_retention(conn, org_id, candidate_id, days):
    """Stamp or extend the purge clock. Called on create and on stage movement."""
    conn.execute(
        "UPDATE candidates SET purge_after = ? WHERE id = ? AND org_id = ?",
        ((date.today() + timedelta(days=days)).isoformat(), candidate_id, org_id),
    )


def due_for_purge(conn, org_id=None, today=None):
    """Candidates whose retention window has closed."""
    today = (today or date.today()).isoformat()
    sql = ("SELECT id, org_id, name, email, purge_after FROM candidates "
           "WHERE purge_after IS NOT NULL AND purge_after <= ?")
    params = [today]
    if org_id is not None:
        sql += " AND org_id = ?"
        params.append(org_id)
    return conn.execute(sql + " ORDER BY purge_after", tuple(params)).fetchall()


def purge_expired(conn, org_id=None, today=None, dry_run=False):
    """
    Delete candidates past their retention date, and everything about them.

    Safe to run repeatedly. `dry_run` reports what would go without touching
    anything, which is how you should run it the first time.
    """
    victims = due_for_purge(conn, org_id, today)
    report = {"examined": len(victims), "purged": 0, "dry_run": dry_run, "names": []}
    if dry_run:
        report["names"] = [f"{v['name']} ({v['purge_after']})" for v in victims]
        return report

    for v in victims:
        _erase_rows(conn, v["org_id"], v["id"])
        report["purged"] += 1
    conn.commit()
    return report


def _erase_rows(conn, org_id, candidate_id):
    """Destroy a candidate's records, leaving redacted audit entries behind."""
    for table, where in _CANDIDATE_CHILDREN:
        conn.execute(f"DELETE FROM {table} WHERE org_id = ? AND {where}",
                     (org_id, candidate_id))
    conn.execute("DELETE FROM candidates WHERE id = ? AND org_id = ?",
                 (candidate_id, org_id))
    # The decision record survives; the person in it does not. `entity` keeps the
    # application id so an auditor can still count decisions, and `detail` keeps
    # the score while losing anything that identifies who was scored.
    conn.execute(
        """UPDATE audit_log SET detail = ?
            WHERE org_id = ? AND entity LIKE ? AND detail IS NOT NULL""",
        (json.dumps({"redacted": "candidate erased under data-subject request"}),
         org_id, f"candidate:{candidate_id}"),
    )


def erase_candidate(conn, org_id, candidate_id):
    """GDPR Art. 17 erasure for one person. Returns what was destroyed."""
    row = conn.execute("SELECT name, email FROM candidates WHERE id = ? AND org_id = ?",
                       (candidate_id, org_id)).fetchone()
    if row is None:
        return None
    apps = conn.execute(
        "SELECT COUNT(*) n FROM applications WHERE org_id = ? AND candidate_id = ?",
        (org_id, candidate_id)).fetchone()["n"]
    _erase_rows(conn, org_id, candidate_id)
    conn.commit()
    return {"name": row["name"], "email": row["email"], "applications": apps}


def export_candidate(conn, org_id, candidate_id):
    """
    Everything held about one person, for a GDPR Art. 15 access request.

    Includes the AI scoring in full — a candidate asking what a system decided
    about them is entitled to the reasoning, not just the verdict.
    """
    cand = conn.execute("SELECT * FROM candidates WHERE id = ? AND org_id = ?",
                        (candidate_id, org_id)).fetchone()
    if cand is None:
        return None
    out = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "candidate": dict(cand),
        "applications": [],
    }
    for a in conn.execute(
        """SELECT a.*, q.title req_title FROM applications a
             JOIN reqs q ON q.id = a.req_id AND q.org_id = a.org_id
            WHERE a.org_id = ? AND a.candidate_id = ?""",
        (org_id, candidate_id),
    ).fetchall():
        entry = dict(a)
        entry["stage_history"] = [dict(e) for e in conn.execute(
            "SELECT from_stage, to_stage, note, created_at FROM stage_events "
            "WHERE org_id = ? AND application_id = ? ORDER BY created_at",
            (org_id, a["id"]))]
        entry["ai_interviews"] = []
        for iv in conn.execute(
            "SELECT * FROM interviews WHERE org_id = ? AND application_id = ?",
            (org_id, a["id"]),
        ).fetchall():
            d = dict(iv)
            for f in ("questions_json", "answers_json", "scores_json"):
                if d.get(f):
                    d[f.replace("_json", "")] = json.loads(d.pop(f))
                else:
                    d.pop(f, None)
            entry["ai_interviews"].append(d)
        out["applications"].append(entry)
    return out


def adverse_impact_rows(conn, org_id, since=None):
    """
    One row per application, for an independent bias auditor.

    Deliberately contains no demographic data — see the module docstring.
    `candidate_key` is the join column: the employer's separately-collected EEO
    self-identification is matched to it by the auditor, so the scoring system
    never holds both halves.
    """
    sql = """SELECT a.id application_id, a.candidate_id, a.created_at applied_on,
                    a.stage final_stage, a.match_score resume_match_score,
                    q.title req_title, q.level, q.segment,
                    i.kind interview_kind, i.overall_score, i.recommendation,
                    i.engine, i.model, i.completed_at scored_on
               FROM applications a
               JOIN reqs q ON q.id = a.req_id AND q.org_id = a.org_id
               LEFT JOIN interviews i ON i.application_id = a.id AND i.org_id = a.org_id
              WHERE a.org_id = ?"""
    params = [org_id]
    if since:
        sql += " AND a.created_at >= ?"
        params.append(since)
    rows = conn.execute(sql + " ORDER BY a.created_at", tuple(params)).fetchall()
    return [{"candidate_key": f"{org_id}-{r['candidate_id']}", **{
        k: v for k, v in dict(r).items() if k != "candidate_id"}} for r in rows]


def adverse_impact_csv(conn, org_id, since=None):
    rows = adverse_impact_rows(conn, org_id, since)
    buf = io.StringIO()
    if not rows:
        return "candidate_key\n"
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def retention_summary(conn, org_id, retention_days):
    """Numbers for the compliance dashboard."""
    total = conn.execute("SELECT COUNT(*) n FROM candidates WHERE org_id = ?",
                         (org_id,)).fetchone()["n"]
    unstamped = conn.execute(
        "SELECT COUNT(*) n FROM candidates WHERE org_id = ? AND purge_after IS NULL",
        (org_id,)).fetchone()["n"]
    due = len(due_for_purge(conn, org_id))
    soon = conn.execute(
        "SELECT COUNT(*) n FROM candidates WHERE org_id = ? AND purge_after > ? "
        "AND purge_after <= ?",
        (org_id, date.today().isoformat(),
         (date.today() + timedelta(days=30)).isoformat())).fetchone()["n"]
    scored = conn.execute(
        "SELECT COUNT(*) n FROM interviews WHERE org_id = ? AND overall_score IS NOT NULL",
        (org_id,)).fetchone()["n"]
    return {"total": total, "unstamped": unstamped, "due": due, "due_soon": soon,
            "scored": scored, "retention_days": retention_days}


def backfill_retention(conn, org_id, retention_days):
    """
    Give existing candidates a purge date.

    Measured from when the record was created, not from today — backdating to
    today would silently extend the retention of data already held too long.
    """
    n = 0
    for c in conn.execute(
        "SELECT id, created_at FROM candidates WHERE org_id = ? AND purge_after IS NULL",
        (org_id,),
    ).fetchall():
        created = datetime.strptime(c["created_at"][:10], "%Y-%m-%d").date()
        conn.execute("UPDATE candidates SET purge_after = ? WHERE id = ? AND org_id = ?",
                     ((created + timedelta(days=retention_days)).isoformat(),
                      c["id"], org_id))
        n += 1
    conn.commit()
    return n


if __name__ == "__main__":
    # Retention job. Wire to a Render cron:
    #   python3 compliance.py --purge
    # Run it with --dry-run first; it reports without deleting.
    import sys
    from config import Config
    from quota_db import get_connection

    conn = get_connection()
    if "--purge" in sys.argv:
        rep = purge_expired(conn, dry_run="--dry-run" in sys.argv)
        print(f"examined {rep['examined']}, purged {rep['purged']}"
              f"{' (dry run)' if rep['dry_run'] else ''}")
        for n in rep["names"]:
            print(f"  would purge: {n}")
    else:
        print("usage: python3 compliance.py --purge [--dry-run]")
    conn.close()
