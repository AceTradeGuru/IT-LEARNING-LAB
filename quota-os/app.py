"""
QuotaOS — sales org operating system (demo prototype).

Monitor the floor, certify the bench, and hire the next seat — in one app.

Run:
    python3 app.py
Then open http://localhost:5003

The AI features (job postings, resume search, interview generation and scoring)
call Claude when ANTHROPIC_API_KEY or an `ant auth login` profile is present, and
fall back to a deterministic local engine otherwise. Every AI-produced artifact is
labeled with which engine wrote it.
"""

import json
import re
from datetime import date, datetime, timedelta

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup, escape

import ai_engine
from health_engine import (evaluate_org, evaluate_rep, pipeline_by_stage,
                           quarter_bounds, quarter_label, recommended_courses)
from quota_db import DB_FILE, create_tables, get_connection, seed_sample_data

app = Flask(__name__)
app.secret_key = "quotaos-demo"

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


@app.context_processor
def inject_globals():
    """Nav state and engine status, on every page."""
    conn = get_connection()
    me_id = session.get("viewer_id")
    viewers = conn.execute(
        "SELECT * FROM reps WHERE role IN ('vp','manager') ORDER BY role DESC, name"
    ).fetchall()
    me = None
    if viewers:
        me = next((v for v in viewers if v["id"] == me_id), viewers[0])
    conn.close()
    return {
        "all_viewers": viewers,
        "me": me,
        "engine": ai_engine.engine_status(),
        "stage_labels": STAGE_LABELS,
        "stages": STAGES,
        "today": date.today(),
    }


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
    — that isn't a trade worth making for one template.
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


def _scope(conn):
    """
    Whose numbers are we looking at?

    A VP sees the org; a manager sees their team. One `manager_id` filter carries
    that all the way through the dashboard queries.
    """
    me_id = session.get("viewer_id")
    me = conn.execute("SELECT * FROM reps WHERE id = ?", (me_id,)).fetchone() if me_id else None
    if me is None:
        me = conn.execute("SELECT * FROM reps WHERE role = 'vp' LIMIT 1").fetchone()
    return me, (me["id"] if me and me["role"] == "manager" else None)


@app.route("/viewer", methods=["POST"])
def set_viewer():
    session["viewer_id"] = int(request.form["viewer_id"])
    return redirect(request.form.get("next") or url_for("dashboard"))


# ------------------------------------------------------------------ floor ---

@app.route("/")
def dashboard():
    conn = get_connection()
    me, scope_id = _scope(conn)
    org = evaluate_org(conn, manager_id=scope_id)
    funnel = pipeline_by_stage(conn, manager_id=scope_id)
    peak = max((f["amount"] for f in funnel), default=1) or 1

    teams = []
    if not scope_id:
        for m in conn.execute(
            "SELECT * FROM reps WHERE role = 'manager' ORDER BY name"
        ).fetchall():
            t = evaluate_org(conn, manager_id=m["id"])
            teams.append({"manager": m, "org": t})

    # The compliance number a VP wants in one glance: who cannot legally sell.
    lapsed = conn.execute(
        """SELECT r.id, r.name, t.name track FROM certifications c
             JOIN reps r ON r.id = c.rep_id JOIN cert_tracks t ON t.id = c.track_id
            WHERE c.status = 'expired' AND t.is_mandatory = 1 AND r.status = 'active'"""
    ).fetchall()

    open_reqs = conn.execute(
        """SELECT q.*, COUNT(a.id) applicants FROM reqs q
             LEFT JOIN applications a ON a.req_id = q.id
            WHERE q.status = 'open' GROUP BY q.id ORDER BY q.created_at DESC"""
    ).fetchall()
    conn.close()
    return render_template("dashboard.html", org=org, funnel=funnel, peak=peak,
                           teams=teams, lapsed=lapsed, open_reqs=open_reqs, scoped=bool(scope_id))


@app.route("/org")
def org_view():
    conn = get_connection()
    me, scope_id = _scope(conn)
    org = evaluate_org(conn, manager_id=scope_id)
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
def rep_detail(rep_id):
    conn = get_connection()
    rep = conn.execute("SELECT * FROM reps WHERE id = ?", (rep_id,)).fetchone()
    if rep is None:
        conn.close()
        abort(404)

    ev = evaluate_rep(conn, rep)
    courses = recommended_courses(conn, ev["signals"])
    manager = conn.execute("SELECT * FROM reps WHERE id = ?", (rep["manager_id"],)).fetchone() \
        if rep["manager_id"] else None
    certs = conn.execute(
        """SELECT c.*, t.name track_name, t.is_mandatory, t.valid_months
             FROM certifications c JOIN cert_tracks t ON t.id = c.track_id
            WHERE c.rep_id = ? ORDER BY t.is_mandatory DESC, t.name""",
        (rep_id,),
    ).fetchall()
    enrollments = conn.execute(
        """SELECT e.*, co.code, co.title, co.category, co.hours
             FROM enrollments e JOIN courses co ON co.id = e.course_id
            WHERE e.rep_id = ? ORDER BY e.status, co.code""",
        (rep_id,),
    ).fetchall()
    weeks = conn.execute(
        "SELECT * FROM activities WHERE rep_id = ? ORDER BY week_start DESC LIMIT 8",
        (rep_id,),
    ).fetchall()
    peak_week = max((w["meetings"] + w["demos"] for w in weeks), default=1) or 1
    conn.close()
    return render_template("rep.html", rep=rep, ev=ev, m=ev["metrics"], manager=manager,
                           courses=courses, certs=certs, enrollments=enrollments,
                           weeks=list(reversed(weeks)), peak_week=peak_week)


@app.route("/rep/<int:rep_id>/assign/<int:course_id>", methods=["POST"])
def assign_course(rep_id, course_id):
    """Assign remediation training straight off a health signal."""
    conn = get_connection()
    reason = request.form.get("reason", "Assigned from health signal")
    conn.execute(
        """INSERT OR IGNORE INTO enrollments
           (rep_id, course_id, status, assigned_on, assigned_reason)
           VALUES (?,?,?,?,?)""",
        (rep_id, course_id, "not_started", date.today().isoformat(), reason),
    )
    conn.commit()
    course = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    rep = conn.execute("SELECT * FROM reps WHERE id = ?", (rep_id,)).fetchone()
    conn.close()
    flash(f"Assigned {course['code']} — {course['title']} to {rep['name']}.")
    return redirect(url_for("rep_detail", rep_id=rep_id))


@app.route("/pipeline")
def pipeline():
    conn = get_connection()
    me, scope_id = _scope(conn)
    where, params = "WHERE d.stage NOT IN ('closed_won','closed_lost')", []
    if scope_id:
        where += " AND r.manager_id = ?"
        params.append(scope_id)
    if request.args.get("rep"):
        where += " AND r.id = ?"
        params.append(int(request.args["rep"]))
    deals = conn.execute(
        f"""SELECT d.*, r.name rep_name, r.id rep_id, r.segment
              FROM deals d JOIN reps r ON r.id = d.rep_id {where}
             ORDER BY d.close_date""",
        tuple(params),
    ).fetchall()
    reps = conn.execute(
        "SELECT * FROM reps WHERE role = 'rep' AND status='active'"
        + (" AND manager_id = ?" if scope_id else "") + " ORDER BY name",
        (scope_id,) if scope_id else (),
    ).fetchall()
    _, q_end, _ = quarter_bounds()
    conn.close()
    return render_template("pipeline.html", deals=deals, reps=reps, q_end=q_end,
                           selected=request.args.get("rep", ""),
                           quarter=quarter_label())


# --------------------------------------------------------------- training ---

@app.route("/training")
def training():
    conn = get_connection()
    courses = conn.execute("SELECT * FROM courses ORDER BY category, code").fetchall()
    tracks = conn.execute(
        "SELECT * FROM cert_tracks ORDER BY is_mandatory DESC, name"
    ).fetchall()

    # Per-track compliance: how many active reps hold it, how many have lapsed.
    headcount = conn.execute(
        "SELECT COUNT(*) n FROM reps WHERE role = 'rep' AND status = 'active'"
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
                "SELECT COUNT(*) n FROM reps WHERE role = 'rep' AND status = 'active' "
                "AND segment = ?", (t["required_for"],),
            ).fetchone()["n"]

        stats = conn.execute(
            """SELECT
                 SUM(CASE WHEN c.status='certified'   THEN 1 ELSE 0 END) certified,
                 SUM(CASE WHEN c.status='expired'     THEN 1 ELSE 0 END) expired,
                 SUM(CASE WHEN c.status='in_progress' THEN 1 ELSE 0 END) in_progress
               FROM certifications c JOIN reps r ON r.id = c.rep_id
              WHERE c.track_id = ? AND r.role = 'rep' AND r.status = 'active'""",
            (t["id"],),
        ).fetchone()
        certified = stats["certified"] or 0
        courses_in = conn.execute(
            """SELECT co.* FROM track_courses tc JOIN courses co ON co.id = tc.course_id
                WHERE tc.track_id = ? ORDER BY tc.sequence""",
            (t["id"],),
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
                  (SELECT COUNT(*) FROM session_signups g WHERE g.session_id = s.id) taken
             FROM class_sessions s JOIN courses co ON co.id = s.course_id
            WHERE s.starts_at >= ? ORDER BY s.starts_at LIMIT 12""",
        (date.today().isoformat(),),
    ).fetchall()

    # Everything expiring or already lapsed in the next 60 days, org-wide.
    horizon = (date.today() + timedelta(days=60)).isoformat()
    renewals = conn.execute(
        """SELECT c.*, r.name rep_name, r.id rep_id, r.team, t.name track_name, t.is_mandatory
             FROM certifications c
             JOIN reps r ON r.id = c.rep_id JOIN cert_tracks t ON t.id = c.track_id
            WHERE r.status = 'active'
              AND (c.status = 'expired' OR (c.expires_on IS NOT NULL AND c.expires_on <= ?))
            ORDER BY c.expires_on""",
        (horizon,),
    ).fetchall()
    conn.close()
    return render_template("training.html", courses=courses, track_rows=track_rows,
                           sessions=sessions, renewals=renewals, headcount=headcount)


@app.route("/session/<int:session_id>/signup", methods=["POST"])
def session_signup(session_id):
    rep_id = int(request.form["rep_id"])
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO session_signups (session_id, rep_id, signed_up_on) VALUES (?,?,?)",
        (session_id, rep_id, date.today().isoformat()),
    )
    conn.commit()
    rep = conn.execute("SELECT name FROM reps WHERE id = ?", (rep_id,)).fetchone()
    conn.close()
    flash(f"{rep['name']} is booked into the session.")
    return redirect(url_for("training"))


# ----------------------------------------------------------------- hiring ---

@app.route("/hiring")
def hiring():
    conn = get_connection()
    reqs = conn.execute(
        """SELECT q.*, r.name manager_name, COUNT(a.id) applicants,
                  SUM(CASE WHEN a.stage NOT IN ('rejected','hired') THEN 1 ELSE 0 END) active
             FROM reqs q LEFT JOIN reps r ON r.id = q.hiring_manager_id
             LEFT JOIN applications a ON a.req_id = q.id
            GROUP BY q.id ORDER BY q.status, q.created_at DESC"""
    ).fetchall()
    board = {s: [] for s in STAGES}
    board["rejected"] = []
    rows = conn.execute(
        """SELECT a.*, c.name cand_name, c.current_title, c.years_exp, q.title req_title
             FROM applications a JOIN candidates c ON c.id = a.candidate_id
             JOIN reqs q ON q.id = a.req_id ORDER BY a.match_score DESC"""
    ).fetchall()
    for r in rows:
        board.setdefault(r["stage"], []).append(r)
    conn.close()
    return render_template("hiring.html", reqs=reqs, board=board)


@app.route("/req/new", methods=["GET", "POST"])
def req_new():
    conn = get_connection()
    managers = conn.execute(
        "SELECT * FROM reps WHERE role IN ('manager','vp') ORDER BY name"
    ).fetchall()
    if request.method == "POST":
        f = request.form
        req_id = conn.execute(
            """INSERT INTO reqs (title, level, team, region, segment, quota_annual, comp_base,
                                 comp_ote, must_haves, nice_haves, hiring_manager_id, status,
                                 created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f["title"], f["level"], f["team"], f["region"], f["segment"],
             int(f["quota_annual"] or 0), int(f["comp_base"] or 0), int(f["comp_ote"] or 0),
             f["must_haves"], f["nice_haves"], int(f["hiring_manager_id"]),
             "draft", date.today().isoformat()),
        ).lastrowid
        conn.commit()
        conn.close()
        flash("Requisition created. Generate the posting when you're ready.")
        return redirect(url_for("req_detail", req_id=req_id))
    conn.close()
    return render_template("req_new.html", managers=managers)


@app.route("/req/<int:req_id>")
def req_detail(req_id):
    conn = get_connection()
    req = conn.execute("SELECT * FROM reqs WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        conn.close()
        abort(404)
    manager = conn.execute(
        "SELECT * FROM reps WHERE id = ?", (req["hiring_manager_id"],)
    ).fetchone() if req["hiring_manager_id"] else None
    apps = conn.execute(
        """SELECT a.*, c.name cand_name, c.current_title, c.current_company, c.years_exp,
                  (SELECT COUNT(*) FROM interviews i WHERE i.application_id = a.id) interviews
             FROM applications a JOIN candidates c ON c.id = a.candidate_id
            WHERE a.req_id = ? ORDER BY
              CASE a.stage WHEN 'hired' THEN 0 WHEN 'offer' THEN 1 WHEN 'panel' THEN 2
                           WHEN 'ai_interview' THEN 3 WHEN 'screened' THEN 4
                           WHEN 'sourced' THEN 5 ELSE 6 END, a.match_score DESC""",
        (req_id,),
    ).fetchall()
    conn.close()
    return render_template("req.html", req=req, manager=manager, apps=apps)


@app.route("/req/<int:req_id>/posting", methods=["POST"])
def req_posting(req_id):
    """AI job posting generation."""
    conn = get_connection()
    req = conn.execute("SELECT * FROM reqs WHERE id = ?", (req_id,)).fetchone()
    if req is None:
        conn.close()
        abort(404)
    markdown, engine = ai_engine.write_posting(req)
    conn.execute(
        "UPDATE reqs SET posting_md = ?, posting_source = ?, status = ? WHERE id = ?",
        (markdown, engine, "open" if req["status"] == "draft" else req["status"], req_id),
    )
    conn.commit()
    conn.close()
    flash(f"Posting written by {'Claude' if engine == 'claude' else 'the offline engine'}.")
    return redirect(url_for("req_detail", req_id=req_id))


@app.route("/search", methods=["GET", "POST"])
def search():
    """AI resume search over the candidate pool."""
    conn = get_connection()
    reqs = conn.execute("SELECT * FROM reqs ORDER BY title").fetchall()
    results, query, engine, req = [], "", None, None

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        req_id = request.form.get("req_id")
        req = conn.execute("SELECT * FROM reqs WHERE id = ?", (req_id,)).fetchone() \
            if req_id else None
        candidates = conn.execute("SELECT * FROM candidates ORDER BY name").fetchall()
        if query:
            ranked, engine = ai_engine.search_resumes(query, candidates, req)
            by_id = {c["id"]: c for c in candidates}
            for r in ranked:
                cand = by_id.get(r["candidate_id"])
                if not cand:
                    continue
                existing = conn.execute(
                    "SELECT * FROM applications WHERE candidate_id = ? AND req_id = ?",
                    (cand["id"], req["id"] if req else -1),
                ).fetchone() if req else None
                results.append({"c": cand, "r": r, "app": existing})
        else:
            flash("Describe who you're looking for, in your own words.")
    conn.close()
    return render_template("search.html", reqs=reqs, results=results, query=query,
                           engine=engine, req=req)


@app.route("/req/<int:req_id>/add/<int:candidate_id>", methods=["POST"])
def add_candidate(req_id, candidate_id):
    """Move a search hit into the funnel."""
    conn = get_connection()
    now = date.today().isoformat()
    existing = conn.execute(
        "SELECT * FROM applications WHERE req_id = ? AND candidate_id = ?",
        (req_id, candidate_id),
    ).fetchone()
    if existing:
        conn.close()
        flash("That candidate is already in this funnel.")
        return redirect(url_for("req_detail", req_id=req_id))
    aid = conn.execute(
        """INSERT INTO applications (req_id, candidate_id, stage, match_score, match_reason,
                                     created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (req_id, candidate_id, "sourced", request.form.get("score") or None,
         request.form.get("reason") or None, now, now),
    ).lastrowid
    conn.execute(
        "INSERT INTO stage_events (application_id, from_stage, to_stage, note, created_at) "
        "VALUES (?,?,?,?,?)",
        (aid, None, "sourced", "Added from AI resume search", now),
    )
    conn.commit()
    conn.close()
    flash("Candidate added to the funnel.")
    return redirect(url_for("application_detail", app_id=aid))


@app.route("/candidates")
def candidates():
    conn = get_connection()
    rows = conn.execute(
        """SELECT c.*, COUNT(a.id) apps FROM candidates c
             LEFT JOIN applications a ON a.candidate_id = c.id
            GROUP BY c.id ORDER BY c.created_at DESC"""
    ).fetchall()
    conn.close()
    return render_template("candidates.html", candidates=rows)


@app.route("/application/<int:app_id>")
def application_detail(app_id):
    conn = get_connection()
    row = conn.execute(
        """SELECT a.*, c.name cand_name, c.email, c.phone, c.location, c.years_exp,
                  c.current_title, c.current_company, c.resume_text, c.source,
                  q.title req_title, q.id req_id
             FROM applications a JOIN candidates c ON c.id = a.candidate_id
             JOIN reqs q ON q.id = a.req_id WHERE a.id = ?""",
        (app_id,),
    ).fetchone()
    if row is None:
        conn.close()
        abort(404)
    req = conn.execute("SELECT * FROM reqs WHERE id = ?", (row["req_id"],)).fetchone()
    interviews = conn.execute(
        "SELECT * FROM interviews WHERE application_id = ? ORDER BY created_at DESC", (app_id,)
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM stage_events WHERE application_id = ? ORDER BY created_at", (app_id,)
    ).fetchall()
    parsed = []
    for i in interviews:
        parsed.append({
            "row": i,
            "questions": json.loads(i["questions_json"]),
            "scores": json.loads(i["scores_json"]) if i["scores_json"] else None,
            "answers": json.loads(i["answers_json"]) if i["answers_json"] else {},
        })
    conn.close()
    return render_template("application.html", a=row, req=req, interviews=parsed, events=events,
                           kinds=ai_engine.INTERVIEW_KINDS)


@app.route("/application/<int:app_id>/stage", methods=["POST"])
def move_stage(app_id):
    to_stage = request.form["to_stage"]
    note = request.form.get("note", "")
    conn = get_connection()
    a = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    if a is None:
        conn.close()
        abort(404)
    now = date.today().isoformat()
    conn.execute("UPDATE applications SET stage = ?, updated_at = ? WHERE id = ?",
                 (to_stage, now, app_id))
    conn.execute(
        "INSERT INTO stage_events (application_id, from_stage, to_stage, note, created_at) "
        "VALUES (?,?,?,?,?)",
        (app_id, a["stage"], to_stage, note, now),
    )
    conn.commit()
    conn.close()
    flash(f"Moved to {STAGE_LABELS.get(to_stage, to_stage)}.")
    if to_stage == "hired":
        return redirect(url_for("onboard", app_id=app_id))
    return redirect(url_for("application_detail", app_id=app_id))


@app.route("/application/<int:app_id>/interview/new", methods=["POST"])
def interview_new(app_id):
    """AI interview generation — questions plus the rubric they'll be scored on."""
    kind = request.form.get("kind", "screen")
    conn = get_connection()
    a = conn.execute(
        "SELECT a.*, q.id req_id FROM applications a JOIN reqs q ON q.id = a.req_id "
        "WHERE a.id = ?", (app_id,)
    ).fetchone()
    if a is None:
        conn.close()
        abort(404)
    req = conn.execute("SELECT * FROM reqs WHERE id = ?", (a["req_id"],)).fetchone()
    questions, engine = ai_engine.build_interview(req, kind)
    iid = conn.execute(
        """INSERT INTO interviews (application_id, kind, questions_json, engine, created_at)
           VALUES (?,?,?,?,?)""",
        (app_id, kind, json.dumps(questions), engine, date.today().isoformat()),
    ).lastrowid
    if a["stage"] in ("sourced", "screened"):
        conn.execute("UPDATE applications SET stage = 'ai_interview', updated_at = ? WHERE id = ?",
                     (date.today().isoformat(), app_id))
        conn.execute(
            "INSERT INTO stage_events (application_id, from_stage, to_stage, note, created_at) "
            "VALUES (?,?,?,?,?)",
            (app_id, a["stage"], "ai_interview", f"{kind} interview generated",
             date.today().isoformat()),
        )
    conn.commit()
    conn.close()
    flash(f"Interview generated by {'Claude' if engine == 'claude' else 'the offline engine'}.")
    return redirect(url_for("interview_run", interview_id=iid))


@app.route("/interview/<int:interview_id>", methods=["GET", "POST"])
def interview_run(interview_id):
    """Conduct the interview, then score every answer against the rubric."""
    conn = get_connection()
    iv = conn.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
    if iv is None:
        conn.close()
        abort(404)
    a = conn.execute(
        """SELECT a.*, c.name cand_name, q.title req_title, q.id req_id
             FROM applications a JOIN candidates c ON c.id = a.candidate_id
             JOIN reqs q ON q.id = a.req_id WHERE a.id = ?""",
        (iv["application_id"],),
    ).fetchone()
    req = conn.execute("SELECT * FROM reqs WHERE id = ?", (a["req_id"],)).fetchone()
    questions = json.loads(iv["questions_json"])

    if request.method == "POST":
        answers = {str(q["id"]): request.form.get(f"answer_{q['id']}", "").strip()
                   for q in questions}
        result, engine = ai_engine.score_interview(req, questions, answers)
        conn.execute(
            """UPDATE interviews SET answers_json = ?, scores_json = ?, overall_score = ?,
                      recommendation = ?, summary = ?, engine = ?, completed_at = ?
                WHERE id = ?""",
            (json.dumps(answers), json.dumps(result), result["overall_score"],
             result["recommendation"], result["summary"], engine,
             date.today().isoformat(), interview_id),
        )
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
def onboard(app_id):
    """
    The join that makes this one product instead of three.

    A hired candidate becomes a row in `reps` with a ramp window, and every
    mandatory certification track is enrolled on day one — so the new seat shows
    up on the dashboard already graded on a ramp curve rather than as an
    unexplained zero.
    """
    conn = get_connection()
    a = conn.execute(
        """SELECT a.*, c.name cand_name, c.email, q.title req_title, q.team, q.region,
                  q.segment, q.quota_annual, q.level, q.hiring_manager_id
             FROM applications a JOIN candidates c ON c.id = a.candidate_id
             JOIN reqs q ON q.id = a.req_id WHERE a.id = ?""",
        (app_id,),
    ).fetchone()
    if a is None:
        conn.close()
        abort(404)
    if a["hired_rep_id"]:
        rep_id = a["hired_rep_id"]
        conn.close()
        flash("This hire is already on the org chart.")
        return redirect(url_for("rep_detail", rep_id=rep_id))

    if request.method == "POST":
        start = request.form.get("start_date") or date.today().isoformat()
        ramp_days = int(request.form.get("ramp_days") or 120)
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        rep_id = conn.execute(
            """INSERT INTO reps (name, email, title, role, manager_id, team, region, segment,
                                 hire_date, ramp_end_date, quota_annual, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (a["cand_name"], a["email"], f"Account Executive, {a['segment']}", "rep",
             a["hiring_manager_id"], a["team"], a["region"], a["segment"], start,
             (start_d + timedelta(days=ramp_days)).isoformat(), a["quota_annual"],
             f"Hired from requisition: {a['req_title']}."),
        ).lastrowid

        # Enroll into every mandatory track and its courses.
        tracks = conn.execute("SELECT * FROM cert_tracks WHERE is_mandatory = 1").fetchall()
        for t in tracks:
            conn.execute(
                "INSERT INTO certifications (rep_id, track_id, status) VALUES (?,?,?)",
                (rep_id, t["id"], "in_progress"))
            for c in conn.execute(
                "SELECT course_id FROM track_courses WHERE track_id = ? ORDER BY sequence",
                (t["id"],),
            ).fetchall():
                conn.execute(
                    """INSERT OR IGNORE INTO enrollments
                       (rep_id, course_id, status, assigned_on, assigned_reason)
                       VALUES (?,?,?,?,?)""",
                    (rep_id, c["course_id"], "not_started", start,
                     f"Onboarding — required for {t['name']}"))

        conn.execute("UPDATE applications SET hired_rep_id = ?, stage = 'hired', updated_at = ? "
                     "WHERE id = ?", (rep_id, date.today().isoformat(), app_id))
        conn.execute(
            "INSERT INTO stage_events (application_id, from_stage, to_stage, note, created_at) "
            "VALUES (?,?,?,?,?)",
            (app_id, a["stage"], "hired", f"Onboarded with a {ramp_days}-day ramp",
             date.today().isoformat()))
        conn.commit()
        conn.close()
        flash(f"{a['cand_name']} is on the org chart with a {ramp_days}-day ramp and "
              f"{len(tracks)} mandatory certification tracks enrolled.")
        return redirect(url_for("rep_detail", rep_id=rep_id))

    tracks = conn.execute(
        "SELECT * FROM cert_tracks WHERE is_mandatory = 1 ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template("onboard.html", a=a, tracks=tracks,
                           default_start=date.today().isoformat())


if __name__ == "__main__":
    if not DB_FILE.exists():
        create_tables()
        seed_sample_data()
    app.run(debug=True, port=5003)
