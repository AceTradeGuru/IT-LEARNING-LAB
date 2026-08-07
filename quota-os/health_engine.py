"""
QuotaOS — rep and org health scoring.

A CRM dashboard tells you a rep is at 41% of quota. It does not tell you whether
that is a problem. This engine does, by asking the six questions a good sales
manager asks in a 1:1 and turning each answer into a signal:

    1. Are they behind the calendar, or just behind the number?
    2. Is there enough pipeline left to catch up, or is the quarter already gone?
    3. Are the deals in that pipeline actually being worked, or are they parked?
    4. Do the deals have a next step, or is "following up" the plan?
    5. Are close dates holding, or has this deal moved three times?
    6. Are they allowed to sell what they're selling? (certifications expire)

Every signal carries points, a severity, and a plain-English detail line, so the
score is never the answer — it is the index into the explanation.

Two design decisions worth calling out:

  * Ramping reps are graded on a curve, not excused. A rep 40 days into a 120-day
    ramp is expected to hit a third of pace. Flagging every new hire as at-risk is
    how a health score trains managers to ignore it.

  * Signals carry a `fixes` key that matches `courses.fixes_signal`. That is the
    join between "this rep is in trouble" and "here is the course that addresses
    it" — the reason training and performance live in the same product.
"""

from datetime import date, datetime, timedelta

# Health bands. High is good here — this is a health score, not a risk score.
BANDS = [
    (80, "strong", "Strong"),
    (60, "on_track", "On track"),
    (40, "watch", "Watch"),
    (0, "at_risk", "At risk"),
]

# A deal untouched for this long is not a deal, it's a hope.
STALE_DAYS = 14

# Standard pipeline coverage target: 3x the remaining number.
COVERAGE_TARGET = 3.0


def _d(value):
    """Parse an ISO date string out of SQLite. Returns None for NULL/blank."""
    if not value:
        return None
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def quarter_bounds(today=None):
    """Fiscal quarter == calendar quarter here. Returns (start, end, elapsed_pct)."""
    today = today or date.today()
    q = (today.month - 1) // 3
    start = date(today.year, q * 3 + 1, 1)
    end = date(today.year + (1 if q == 3 else 0), (q * 3 + 4) if q < 3 else 1, 1) - timedelta(days=1)
    span = (end - start).days + 1
    elapsed = min(max((today - start).days + 1, 0), span)
    return start, end, round(elapsed / span * 100)


def quarter_label(today=None):
    today = today or date.today()
    return f"Q{(today.month - 1) // 3 + 1} {today.year}"


def band_for(score):
    for floor, key, label in BANDS:
        if score >= floor:
            return key, label
    return "at_risk", "At risk"


def _sig(key, label, detail, points, severity, fixes=None):
    return {
        "key": key,
        "label": label,
        "detail": detail,
        "points": points,
        "severity": severity,
        "fixes": fixes,
    }


def rep_metrics(conn, rep, today=None):
    """
    Everything measurable about one rep this quarter, in one pass.

    Kept separate from scoring so the rep detail page can show the raw numbers
    beside the interpretation — a manager who can't see the inputs won't trust
    the output.
    """
    today = today or date.today()
    q_start, q_end, pace_pct = quarter_bounds(today)
    rep_id = rep["id"]

    org_id = rep["org_id"]
    deals = conn.execute(
        "SELECT * FROM deals WHERE org_id = ? AND rep_id = ? ORDER BY close_date",
        (org_id, rep_id),
    ).fetchall()

    open_deals, won_deals, lost_deals = [], [], []
    for d in deals:
        if d["stage"] == "closed_won":
            won_deals.append(d)
        elif d["stage"] == "closed_lost":
            lost_deals.append(d)
        else:
            open_deals.append(d)

    quota_q = round(rep["quota_annual"] / 4)
    closed_q = sum(d["amount"] for d in won_deals if q_start <= _d(d["close_date"]) <= q_end)
    attainment_pct = round(closed_q / quota_q * 100) if quota_q else 0
    remaining = max(quota_q - closed_q, 0)

    # Only pipeline that can land this quarter counts toward coverage. A deal
    # slated for next March does not help you hit September.
    pipeline_q = sum(d["amount"] for d in open_deals if _d(d["close_date"]) <= q_end)
    pipeline_all = sum(d["amount"] for d in open_deals)
    # Coverage is meaningless once the number is essentially made — dividing by a
    # $2K gap produces a 100x ratio that says nothing. Report None instead.
    coverage = round(pipeline_q / remaining, 2) if remaining > quota_q * 0.05 else None

    stale = [
        d for d in open_deals
        if not d["last_activity_date"]
        or (today - _d(d["last_activity_date"])).days > STALE_DAYS
    ]
    no_next_step = [d for d in open_deals if not (d["next_step"] or "").strip()]

    # How many people we actually know at each open opportunity's account. A deal
    # with one contact is one job change away from dying, and no amount of pipeline
    # hygiene shows that — it only becomes visible once contacts are real records.
    single_threaded = []
    for d in open_deals:
        if not d["account_id"]:
            continue
        n = conn.execute(
            "SELECT COUNT(*) n FROM contacts WHERE org_id = ? AND account_id = ?",
            (org_id, d["account_id"]),
        ).fetchone()["n"]
        if n <= 1:
            single_threaded.append(d)
    slipped = [d for d in open_deals if _d(d["close_date"]) > _d(d["close_date_original"])]

    four_weeks_ago = (today - timedelta(days=28)).isoformat()
    act = conn.execute(
        """SELECT COALESCE(SUM(calls),0) c, COALESCE(SUM(emails),0) e,
                  COALESCE(SUM(meetings),0) m, COALESCE(SUM(demos),0) dm
             FROM activities WHERE org_id = ? AND rep_id = ? AND week_start >= ?""",
        (org_id, rep_id, four_weeks_ago),
    ).fetchone()
    # Meetings and demos are the activity that correlates with revenue. Calls and
    # emails are inputs to those, tracked but not scored.
    engagements = act["m"] + act["dm"]

    hired = _d(rep["hire_date"])
    ramp_end = _d(rep["ramp_end_date"])
    # A hire onboarded with a future start date has not begun. Clamping ramp
    # progress into [0, 1] keeps that case from producing a negative percentage.
    not_started = bool(hired and hired > today)
    ramping = bool(ramp_end and today <= ramp_end)
    ramp_pct = 100
    if ramping and hired:
        total = max((ramp_end - hired).days, 1)
        ramp_pct = round(min(max((today - hired).days / total, 0.0), 1.0) * 100)

    certs = conn.execute(
        """SELECT c.*, t.name track_name, t.is_mandatory, t.required_for
             FROM certifications c JOIN cert_tracks t ON t.id = c.track_id
            WHERE c.org_id = ? AND c.rep_id = ?""",
        (org_id, rep_id),
    ).fetchall()
    expired = [c for c in certs if c["status"] == "expired" and c["is_mandatory"]]
    expiring = [
        c for c in certs
        if c["status"] == "certified" and c["expires_on"]
        and 0 <= (_d(c["expires_on"]) - today).days <= 45
    ]
    missing = conn.execute(
        """SELECT t.* FROM cert_tracks t
            WHERE t.org_id = ? AND t.is_mandatory = 1
              AND t.id NOT IN (SELECT track_id FROM certifications
                                WHERE org_id = ? AND rep_id = ?)""",
        (org_id, org_id, rep_id),
    ).fetchall()

    return {
        "quarter": quarter_label(today),
        "q_start": q_start,
        "q_end": q_end,
        "pace_pct": pace_pct,
        "quota_q": quota_q,
        "closed_q": closed_q,
        "attainment_pct": attainment_pct,
        "remaining": remaining,
        "pipeline_q": pipeline_q,
        "pipeline_all": pipeline_all,
        "coverage": coverage,
        "open_deals": open_deals,
        "won_deals": won_deals,
        "lost_deals": lost_deals,
        "stale": stale,
        "no_next_step": no_next_step,
        "single_threaded": single_threaded,
        "slipped": slipped,
        "activity": dict(act),
        "engagements": engagements,
        "ramping": ramping,
        "ramp_pct": ramp_pct,
        "not_started": not_started,
        "hire_date": hired,
        "certs": certs,
        "expired_certs": expired,
        "expiring_certs": expiring,
        "missing_certs": missing,
    }


def team_median_engagements(conn, org_id, manager_id, today=None):
    """Median meetings+demos over the trailing four weeks for a manager's team."""
    today = today or date.today()
    cutoff = (today - timedelta(days=28)).isoformat()
    rows = conn.execute(
        """SELECT COALESCE(SUM(a.meetings + a.demos), 0) n
             FROM reps r LEFT JOIN activities a
               ON a.rep_id = r.id AND a.org_id = r.org_id AND a.week_start >= ?
            WHERE r.org_id = ? AND r.manager_id = ? AND r.role = 'rep'
              AND r.status = 'active'
            GROUP BY r.id""",
        (cutoff, org_id, manager_id),
    ).fetchall()
    values = sorted(r["n"] for r in rows)
    if not values:
        return 0
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def evaluate_rep(conn, rep, today=None, team_median=None):
    """
    Score one rep 0-100 and explain every point of it.

    Returns {score, band, band_label, signals, metrics}. Signals are ordered
    worst-first, because that is the order a manager should read them in.
    """
    today = today or date.today()
    m = rep_metrics(conn, rep, today)
    signals = []
    score = 100

    if team_median is None and rep["manager_id"]:
        team_median = team_median_engagements(conn, rep["org_id"], rep["manager_id"], today)

    # A seat that hasn't started yet is not a performance problem. Score it neutral
    # and say why, rather than running pace and pipeline rules against a rep whose
    # first day is still in the future.
    if m["not_started"]:
        days = (m["hire_date"] - today).days
        sig = _sig("not_started", "Has not started yet",
                   f"Start date is {m['hire_date'].strftime('%b %-d, %Y')} — {days} days out. "
                   f"Ramp begins then; no performance signals apply until it does.",
                   0, "info")
        return {"score": 100, "band": "strong", "band_label": "Not started",
                "signals": [sig], "metrics": m}

    # --- 1. Attainment against the calendar --------------------------------
    # A ramping rep is held to a fraction of pace proportional to how far into
    # ramp they are. Judging a 30-day hire against a 60%-elapsed quarter is how
    # you teach managers to ignore the score.
    expected = m["pace_pct"] * (m["ramp_pct"] / 100 if m["ramping"] else 1)
    gap = expected - m["attainment_pct"]
    if m["ramping"]:
        signals.append(_sig(
            "ramping", "In ramp",
            f"{m['ramp_pct']}% through ramp — measured against {round(expected)}% "
            f"expected attainment, not the full {m['pace_pct']}% quarter pace.",
            0, "info",
        ))
    if gap >= 25:
        signals.append(_sig(
            "behind_pace", "Well behind pace",
            f"{m['attainment_pct']}% of quota with {m['pace_pct']}% of the quarter gone "
            f"— a {round(gap)}-point gap. ${m['remaining']:,} still to close.",
            -25, "critical", fixes="behind_pace",
        ))
    elif gap >= 15:
        signals.append(_sig(
            "behind_pace_mild", "Behind pace",
            f"{m['attainment_pct']}% attained against {m['pace_pct']}% elapsed. "
            f"Recoverable, but not on the current run rate.",
            -12, "high", fixes="behind_pace",
        ))
    elif m["attainment_pct"] >= m["pace_pct"] + 15:
        signals.append(_sig(
            "ahead_pace", "Ahead of pace",
            f"{m['attainment_pct']}% attained at {m['pace_pct']}% elapsed.",
            8, "good",
        ))

    # --- 2. Is there enough left to close? ---------------------------------
    # An empty pipeline is reported once, by the hygiene block below. Letting the
    # coverage rules also fire on 0.0x would charge the same rep twice for it.
    n_open = len(m["open_deals"])
    # Within 5% of the number counts as met. Otherwise a rep sitting on a $2K gap
    # shows a meaningless 100x coverage ratio and trips the concentration rule.
    met = m["remaining"] <= m["quota_q"] * 0.05
    if not n_open:
        pass
    elif met:
        signals.append(_sig(
            "quota_met", "Quota met",
            f"${m['closed_q']:,} closed against a ${m['quota_q']:,} quarterly number.",
            10, "good",
        ))
    elif m["coverage"] is not None:
        if m["coverage"] < 1.5:
            signals.append(_sig(
                "thin_pipeline", "Pipeline cannot cover the gap",
                f"${m['pipeline_q']:,} of in-quarter pipeline against ${m['remaining']:,} "
                f"remaining — {m['coverage']}x coverage. Every open deal would have to close.",
                -22, "critical", fixes="thin_pipeline",
            ))
        elif m["coverage"] < COVERAGE_TARGET:
            signals.append(_sig(
                "low_coverage", "Coverage below target",
                f"{m['coverage']}x against a {COVERAGE_TARGET}x target. "
                f"Prospecting now is the only lever that moves this quarter.",
                -10, "high", fixes="thin_pipeline",
            ))
        elif m["coverage"] >= 4:
            signals.append(_sig(
                "healthy_coverage", "Healthy coverage",
                f"{m['coverage']}x in-quarter pipeline against the remaining number.",
                5, "good",
            ))

    # --- 3, 4, 5. Deal hygiene ---------------------------------------------
    if n_open:
        stale_ratio = len(m["stale"]) / n_open
        if stale_ratio > 0.4:
            signals.append(_sig(
                "stale_deals", "Pipeline is parked",
                f"{len(m['stale'])} of {n_open} open deals have had no activity in "
                f"{STALE_DAYS}+ days. The forecast counts them; nobody is working them.",
                -15, "high", fixes="stale_deals",
            ))
        elif stale_ratio > 0.2:
            signals.append(_sig(
                "some_stale", "Some deals going cold",
                f"{len(m['stale'])} of {n_open} open deals untouched for {STALE_DAYS}+ days.",
                -7, "medium", fixes="stale_deals",
            ))

        if len(m["no_next_step"]) / n_open > 0.3:
            signals.append(_sig(
                "no_next_step", "Deals without a next step",
                f"{len(m['no_next_step'])} open deals have no scheduled next step. "
                f"A deal without a next step is a deal without a close date.",
                -8, "medium", fixes="no_next_step",
            ))

        slip_ratio = len(m["slipped"]) / n_open
        if slip_ratio > 0.3:
            worst = max(
                (( _d(d["close_date"]) - _d(d["close_date_original"]) ).days for d in m["slipped"]),
                default=0,
            )
            signals.append(_sig(
                "slippage", "Close dates are moving",
                f"{len(m['slipped'])} of {n_open} deals have slipped, the worst by {worst} days. "
                f"Repeated pushes usually mean the buyer's timeline was never real.",
                -12, "high", fixes="slippage",
            ))

        # Single-threading. Weighted by dollars rather than deal count, because one
        # single-threaded $300K deal is a bigger problem than three $10K ones.
        st = m["single_threaded"]
        if st:
            st_value = sum(d["amount"] for d in st)
            share = st_value / m["pipeline_all"] if m["pipeline_all"] else 0
            if share >= 0.4:
                worst = max(st, key=lambda d: d["amount"])
                signals.append(_sig(
                    "single_threaded", "Single-threaded on most of the pipeline",
                    f"{len(st)} of {n_open} open deals have exactly one known contact, "
                    f"covering {round(share * 100)}% of pipeline value. The largest is "
                    f"{worst['account']} at ${worst['amount']:,} — one person leaving "
                    f"ends that deal.",
                    -14, "high", fixes="single_threaded",
                ))
            elif share >= 0.2:
                signals.append(_sig(
                    "some_single_threaded", "Some deals rest on one contact",
                    f"{len(st)} of {n_open} open deals have a single known contact, "
                    f"covering {round(share * 100)}% of pipeline value.",
                    -6, "medium", fixes="single_threaded",
                ))

        # One whale carrying the quarter is a forecast risk even when the number
        # looks fine today. Two conditions, not one: the deal has to be most of
        # what's left to close AND most of the rep's own pipeline. A single large
        # deal inside a deep pipeline is just an enterprise deal.
        biggest = max(m["open_deals"], key=lambda d: d["amount"])
        share_of_pipeline = biggest["amount"] / m["pipeline_all"] if m["pipeline_all"] else 0
        if (n_open >= 2 and not met
                and biggest["amount"] >= m["remaining"] * 0.6
                and share_of_pipeline >= 0.4):
            signals.append(_sig(
                "concentration", "Quarter rides on one deal",
                f"{biggest['account']} at ${biggest['amount']:,} is "
                f"{round(biggest['amount'] / m['remaining'] * 100)}% of what's left to close "
                f"and {round(share_of_pipeline * 100)}% of the entire pipeline.",
                -6, "medium", fixes="concentration",
            ))
    elif rep["role"] == "rep" and m["remaining"] > 0:
        signals.append(_sig(
            "empty_pipeline", "No open pipeline",
            f"Zero open opportunities with ${m['remaining']:,} left against quota. "
            f"There is no version of this quarter that works from here.",
            -38, "critical", fixes="thin_pipeline",
        ))

    # --- 6. Activity, relative to the people sitting next to them -----------
    if team_median:
        ratio = m["engagements"] / team_median if team_median else 1
        if ratio < 0.6:
            signals.append(_sig(
                "low_activity", "Activity below the team",
                f"{m['engagements']} meetings and demos in the last 4 weeks against a "
                f"team median of {round(team_median)}.",
                -10, "high", fixes="low_activity",
            ))
        elif ratio > 1.3:
            signals.append(_sig(
                "high_activity", "Activity above the team",
                f"{m['engagements']} meetings and demos in the last 4 weeks "
                f"(team median {round(team_median)}).",
                4, "good",
            ))

    # --- 7. Certification compliance ---------------------------------------
    for c in m["expired_certs"]:
        signals.append(_sig(
            "cert_expired", f"{c['track_name']} expired",
            f"Lapsed {c['expires_on']}. This certification is mandatory — "
            f"deals sourced without it are a compliance exposure, not just a gap.",
            -15, "critical", fixes="cert_expired",
        ))
    for t in m["missing_certs"]:
        signals.append(_sig(
            "cert_missing", f"{t['name']} never started",
            f"Mandatory for {t['required_for']}. No enrollment on record.",
            -10, "high", fixes="cert_missing",
        ))
    for c in m["expiring_certs"]:
        days = (_d(c["expires_on"]) - today).days
        signals.append(_sig(
            "cert_expiring", f"{c['track_name']} expires in {days} days",
            f"Renewal due {c['expires_on']}. Schedule the session before it lapses.",
            -3, "low", fixes="cert_expiring",
        ))

    # A rep with nothing wrong should say so, not render an empty panel.
    if not signals:
        signals.append(_sig(
            "clean", "Nothing flagged",
            "On pace, covered, and current on certifications. No signals fired.",
            0, "good",
        ))

    score = max(0, min(100, score + sum(s["points"] for s in signals)))
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "good": 5}
    signals.sort(key=lambda s: (order.get(s["severity"], 9), s["points"]))
    key, label = band_for(score)

    return {"score": score, "band": key, "band_label": label, "signals": signals, "metrics": m}


def recommended_courses(conn, org_id, signals):
    """
    Map fired signals to the courses that address them.

    This is deliberately a lookup, not a model call: a manager assigning
    remediation training wants the same answer every time, and the catalog is
    small enough that a join beats a generation.
    """
    keys = {s["fixes"] for s in signals if s["fixes"] and s["points"] < 0}
    if not keys:
        return []
    marks = ",".join("?" * len(keys))
    return conn.execute(
        f"""SELECT * FROM courses WHERE org_id = ? AND fixes_signal IN ({marks})
             ORDER BY category, title""",
        tuple([org_id] + list(keys)),
    ).fetchall()


def evaluate_org(conn, org_id, today=None, manager_id=None, rep_ids=None):
    """
    Roll every rep up into one view of the org (or one manager's team).

    Returns the per-rep evaluations plus the aggregates a VP actually opens the
    dashboard for: attainment, coverage, band distribution, and who is at risk.
    """
    today = today or date.today()
    where = "WHERE org_id = ? AND role = 'rep' AND status = 'active'"
    params = [org_id]
    if manager_id:
        where += " AND manager_id = ?"
        params.append(manager_id)
    # `rep_ids` is the row-level ACL from auth.visible_rep_ids(). An empty set is a
    # real answer meaning "this user sees no reps" — it must not be read as "no
    # filter", which is why it is checked against None rather than falsiness.
    if rep_ids is not None:
        if not rep_ids:
            rows = []
            params = None
        else:
            marks = ",".join("?" * len(rep_ids))
            where += f" AND id IN ({marks})"
            params.extend(rep_ids)
    if params is not None:
        rows = conn.execute(
            f"SELECT * FROM reps {where} ORDER BY name", tuple(params)
        ).fetchall()

    medians = {}
    evals = []
    for rep in rows:
        mgr = rep["manager_id"]
        if mgr not in medians:
            medians[mgr] = team_median_engagements(conn, org_id, mgr, today)
        ev = evaluate_rep(conn, rep, today, team_median=medians[mgr])
        ev["rep"] = rep
        evals.append(ev)

    # Seats that start next month shouldn't dilute this quarter's attainment —
    # they carry a quota on paper and zero opportunity to have hit it.
    starting_soon = [e for e in evals if e["metrics"]["not_started"]]
    evals = [e for e in evals if not e["metrics"]["not_started"]]

    quota = sum(e["metrics"]["quota_q"] for e in evals)
    closed = sum(e["metrics"]["closed_q"] for e in evals)
    pipeline = sum(e["metrics"]["pipeline_q"] for e in evals)
    remaining = max(quota - closed, 0)
    _, _, pace = quarter_bounds(today)

    bands = {"strong": 0, "on_track": 0, "watch": 0, "at_risk": 0}
    for e in evals:
        bands[e["band"]] += 1

    return {
        "evals": sorted(evals, key=lambda e: e["score"]),
        "quarter": quarter_label(today),
        "pace_pct": pace,
        "quota": quota,
        "closed": closed,
        "pipeline": pipeline,
        "remaining": remaining,
        "attainment_pct": round(closed / quota * 100) if quota else 0,
        "coverage": round(pipeline / remaining, 2) if remaining else None,
        "bands": bands,
        "headcount": len(evals),
        "starting_soon": starting_soon,
        "avg_score": round(sum(e["score"] for e in evals) / len(evals)) if evals else 0,
        "at_risk": [e for e in evals if e["band"] in ("at_risk", "watch")],
    }


def pipeline_by_stage(conn, org_id, manager_id=None, rep_ids=None):
    """Funnel view — open dollars per stage, for the dashboard's stage bars."""
    stages = ["discovery", "demo", "proposal", "negotiation"]
    where = "WHERE d.org_id = ? AND d.stage = ?"
    params = [org_id]
    if manager_id:
        where += " AND r.manager_id = ?"
        params.append(manager_id)
    if rep_ids is not None:
        if not rep_ids:
            return [{"stage": s, "label": s.title(), "count": 0, "amount": 0} for s in stages]
        where += f" AND d.rep_id IN ({','.join('?' * len(rep_ids))})"
        params.extend(rep_ids)
    out = []
    for s in stages:
        row = conn.execute(
            f"""SELECT COUNT(*) n, COALESCE(SUM(d.amount),0) amt
                  FROM deals d JOIN reps r ON r.id = d.rep_id AND r.org_id = d.org_id
                  {where}""",
            tuple([params[0], s] + params[1:]),
        ).fetchone()
        out.append({"stage": s, "label": s.title(), "count": row["n"], "amount": row["amt"]})
    return out
