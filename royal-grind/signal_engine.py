"""
Royal Grind — opportunity scoring.

A prospecting database tells you a company exists. It does not tell you whether
to call them this morning. This engine does, by scoring every target 0-100 and
attaching the reasons, so the score is never the answer — it is the index into
the explanation.

Four questions, in the order a good salesperson asks them:

    1. Why now?           Something happened. Is it recent, real, and relevant?
    2. Are they a fit?    Can we actually haul what they ship, where they ship it?
    3. Can I reach them?  Is the number verified, or is it a coin flip with a
                          story attached?
    4. Is this the right person, and have I already burned them?

Three decisions worth calling out:

  * **Timing signals decay on a half-life, not a cliff.** A new-facility
    announcement is worth most in week one and something in week six. A hard
    30-day expiry makes the list lurch; exponential decay makes it drift, which
    is how the underlying reality actually behaves.

  * **Reachability is scored, not filtered.** The instinct is to hide every
    prospect without a verified direct dial. That empties the list. Instead a
    guessed number costs points and says so on the card, and the rep decides
    whether a 92-fit company is worth dialing a switchboard for.

  * **The weights learn, but slowly.** `learned_weights()` bends the base points
    toward whatever is actually converting for this org, shrunk hard toward the
    prior until real volume accumulates. A signal with four attempts and one
    meeting has a 25% meeting rate and means nothing, and an engine that chases
    it will reorder the entire sales floor's morning on noise.

Nothing in here calls a model. A rep who gets a different list each time they
refresh stops believing the list.
"""

import json
import math
from datetime import date, datetime, timedelta

# Only for the two normalizers. compliance does not import this module, so there
# is no cycle — and the suppression list must be compared the way it is stored,
# or "+1 (816) 555-0143" walks straight past an entry for "8165550143".
from compliance import normalize_email, normalize_phone

# Score bands. High is good — this is an opportunity score, not a risk score.
BANDS = [
    (80, "hot", "Call today"),
    (60, "warm", "This week"),
    (40, "cool", "Nurture"),
    (0, "cold", "Park it"),
]

# A target called within this many days is rested rather than re-dialed. Calling
# the same person three days running is how a brand gets a reputation.
COOLOFF_DAYS = 4

# Attempts without a single connect, after which the number itself is the more
# likely explanation than the person's schedule.
FATIGUE_ATTEMPTS = 6


# ---------------------------------------------------------------------------
# What counts as a buying signal, and what each is worth
# ---------------------------------------------------------------------------
#
# `points`     the ceiling, at the moment the event is observed
# `half_life`  days until the event is worth half that
# `need`       the freight need it implies — feeds the AI call plan and the
#              mode-fit check, so a flatbed signal is worth less to an LTL-only
#              broker than to one with flatbed capacity
#
# Adding a kind means adding it here. The `kind` column on `triggers` joins to
# this dict, and an unknown kind scores zero rather than guessing — silently
# inventing a weight for an event nobody defined is worse than ignoring it.
TRIGGER_TYPES = {
    "new_facility": {
        "label": "New facility announced",
        "points": 26, "half_life": 45,
        "need": "Outbound volume from a site with no incumbent lane coverage yet.",
        "modes": ["ftl", "flatbed", "ltl"],
    },
    "expansion": {
        "label": "Expansion or capacity increase",
        "points": 22, "half_life": 60,
        "need": "More volume on existing lanes than the current carrier base absorbs.",
        "modes": ["ftl", "ltl"],
    },
    "logistics_hire": {
        "label": "Hiring logistics or supply-chain staff",
        "points": 20, "half_life": 40,
        "need": "A department being built or rebuilt — vendor lists get rewritten.",
        "modes": ["ltl", "ftl"],
    },
    "leadership_change": {
        "label": "New supply-chain leadership",
        "points": 24, "half_life": 75,
        "need": "A new leader reviews incumbents in their first two quarters.",
        "modes": ["ltl", "ftl", "flatbed", "reefer"],
    },
    "carrier_trouble": {
        "label": "Public service complaint",
        "points": 28, "half_life": 30,
        "need": "An incumbent is failing them in a way they said out loud.",
        "modes": ["expedited", "ftl", "ltl"],
    },
    "contract_window": {
        "label": "Contract or bid window open",
        "points": 30, "half_life": 25,
        "need": "An RFP or annual bid is live. There is a door and it closes.",
        "modes": ["ftl", "ltl", "flatbed", "reefer", "intermodal"],
    },
    "seasonal_peak": {
        "label": "Approaching seasonal peak",
        "points": 16, "half_life": 35,
        "need": "Volume they cannot cover with contracted capacity alone.",
        "modes": ["ftl", "expedited"],
    },
    "new_product": {
        "label": "New product or SKU launch",
        "points": 14, "half_life": 50,
        "need": "New pack sizes and new destinations the current routing guide misses.",
        "modes": ["ltl", "ftl"],
    },
    "funding": {
        "label": "Funding or acquisition",
        "points": 18, "half_life": 70,
        "need": "Budget and a mandate to scale operations.",
        "modes": ["ltl", "ftl"],
    },
    "compliance_event": {
        "label": "Safety or compliance event at incumbent",
        "points": 21, "half_life": 40,
        "need": "Their carrier's authority or safety rating is now a board-level risk.",
        "modes": ["ftl", "flatbed"],
    },
    "facility_closure": {
        "label": "Facility closure or consolidation",
        "points": 17, "half_life": 45,
        "need": "Lanes are being redrawn — the routing guide is in play.",
        "modes": ["ftl", "intermodal"],
    },
    "tariff_shift": {
        "label": "Sourcing or tariff shift",
        "points": 15, "half_life": 55,
        "need": "Inbound origin changes force a new drayage and linehaul plan.",
        "modes": ["intermodal", "ftl"],
    },
}


def trigger_label(kind):
    return TRIGGER_TYPES.get(kind, {}).get("label", kind.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _d(value):
    """Parse an ISO date out of either database. Returns None for NULL/blank."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        d = _d(value)
        return datetime.combine(d, datetime.min.time()) if d else None


def _days_since(value, today):
    d = _d(value)
    return None if d is None else (today - d).days


def band_for(score):
    for floor, key, label in BANDS:
        if score >= floor:
            return key, label
    return "cold", "Park it"


def _sig(key, label, detail, points, severity, category):
    """
    One line of the explanation.

    `points` is signed: positive adds to the score, negative subtracts. The UI
    renders the sign, so a rep can see what is helping and what is hurting
    without reading the code that produced it.
    """
    return {"key": key, "label": label, "detail": detail,
            "points": round(points, 1), "severity": severity, "category": category}


# Above this, extra points buy progressively less score. Below it, the raw total
# and the score are the same number.
SOFT_CAP_KNEE = 70
SOFT_CAP_SCALE = 40


def soft_cap(raw):
    """
    Compress the top of the range instead of clamping it.

    A hard `min(raw, 100)` throws away ordering precisely where a rep needs it:
    a record with two strong signals and a verified direct dial and one with
    four of each both read "100", and the list stops being ranked at the top.
    This curve approaches 100 without reaching it, so two very strong records
    still sort against each other, and the gap between 70 and 85 still means
    what it meant before.
    """
    if raw <= SOFT_CAP_KNEE:
        return max(0.0, raw)
    over = raw - SOFT_CAP_KNEE
    headroom = 100 - SOFT_CAP_KNEE
    return 100 - headroom * math.exp(-over / SOFT_CAP_SCALE)


def decay(points, days_old, half_life):
    """Exponential decay. A 45-day half-life event is worth half at day 45."""
    if days_old is None or days_old < 0:
        return points
    return points * math.pow(0.5, days_old / max(half_life, 1))


def _csv(value):
    return [p.strip().lower() for p in (value or "").split(",") if p.strip()]


# ---------------------------------------------------------------------------
# The learning loop
# ---------------------------------------------------------------------------
#
# Everything above is a prior — an experienced broker's opinion about what a
# new-facility announcement is worth. What it is actually worth is an empirical
# question, and every call this platform places is one observation of it.

# Below this many attempts a signal's observed rate is noise, and the weight
# stays essentially at its prior. The number is the shrinkage constant in the
# Bayesian sense: at exactly this many attempts the evidence and the prior get
# equal say.
SHRINK_ATTEMPTS = 40

# The most the evidence may move a weight, in either direction. A signal that
# converts three times better than average is capped at 1.6x rather than
# allowed to swallow the list, because the sample it came from was drawn by a
# scoring function that already favored it. Left uncapped, a lucky signal
# becomes a self-fulfilling prophecy: it gets scored higher, so it gets called
# more, so it accumulates more outcomes, so it gets scored higher again.
WEIGHT_FLOOR, WEIGHT_CEIL = 0.55, 1.6

# What the loop optimizes for. A connect is not success — a conversation that
# produces a meeting is. Weighting meetings well above connects stops the engine
# learning to prefer the signals that merely correlate with someone picking up.
OUTCOME_VALUE = {"connects": 1.0, "conversations": 2.0, "meetings": 8.0,
                 "opportunities": 14.0, "won": 30.0}


def outcome_value(row):
    """Blended value of everything one signal's calls produced."""
    return sum(OUTCOME_VALUE[k] * (row[k] or 0) for k in OUTCOME_VALUE)


def learned_weights(conn, org_id):
    """
    Per-signal multipliers derived from what actually happened.

    Returns {signal_key: multiplier}, defaulting to 1.0 for anything unseen.
    The multiplier is the signal's value-per-attempt against the org-wide
    average, shrunk toward 1.0 by sample size and then clamped.

    An org on its first day gets every weight at 1.0 and the engine behaves
    exactly as the priors say. That is the correct behavior — not a limitation
    to apologize for. The alternative is a cold-start system that pretends to
    know something.
    """
    rows = conn.execute(
        "SELECT * FROM signal_outcomes WHERE org_id = ? AND attempts > 0",
        (org_id,),
    ).fetchall()
    if not rows:
        return {}

    total_attempts = sum(r["attempts"] for r in rows)
    total_value = sum(outcome_value(r) for r in rows)
    if total_attempts == 0 or total_value <= 0:
        return {}
    baseline = total_value / total_attempts

    weights = {}
    for r in rows:
        attempts = r["attempts"]
        observed = outcome_value(r) / attempts
        raw = observed / baseline if baseline else 1.0
        # Shrinkage: with `attempts` observations against a SHRINK_ATTEMPTS
        # prior, trust the evidence in proportion to how much of it there is.
        trust = attempts / (attempts + SHRINK_ATTEMPTS)
        blended = 1.0 + (raw - 1.0) * trust
        weights[r["signal_key"]] = max(WEIGHT_FLOOR, min(WEIGHT_CEIL, blended))
    return weights


def weight_evidence(conn, org_id):
    """
    The learning loop, rendered for a human.

    Returns one row per signal with its attempts, its funnel, its current
    multiplier, and — the column that matters — whether there is yet enough
    data to believe it. A dashboard that shows a 300% lift off nine calls
    without saying so is lying with arithmetic.
    """
    rows = conn.execute(
        """SELECT * FROM signal_outcomes WHERE org_id = ?
            ORDER BY attempts DESC""", (org_id,)).fetchall()
    weights = learned_weights(conn, org_id)
    total_attempts = sum(r["attempts"] for r in rows) or 1
    total_value = sum(outcome_value(r) for r in rows)
    baseline = total_value / total_attempts if total_attempts else 0

    out = []
    for r in rows:
        attempts = r["attempts"] or 0
        value = outcome_value(r)
        spec = TRIGGER_TYPES.get(r["signal_key"], {})
        out.append({
            "key": r["signal_key"],
            "label": spec.get("label") or trigger_label(r["signal_key"]),
            "attempts": attempts,
            "connects": r["connects"] or 0,
            "conversations": r["conversations"] or 0,
            "meetings": r["meetings"] or 0,
            "opportunities": r["opportunities"] or 0,
            "won": r["won"] or 0,
            "dollars": r["dollars"] or 0,
            "connect_rate": (r["connects"] or 0) / attempts if attempts else 0,
            "meeting_rate": (r["meetings"] or 0) / attempts if attempts else 0,
            "lift": (value / attempts / baseline) if attempts and baseline else 1.0,
            "weight": weights.get(r["signal_key"], 1.0),
            # The honesty column. SHRINK_ATTEMPTS is where evidence and prior
            # get equal say, so half of it is a fair line for "worth reading".
            "confident": attempts >= SHRINK_ATTEMPTS // 2,
            "base_points": spec.get("points", 0),
        })
    return out


# Which dispositions count as which outcome. One call updates several counters:
# a meeting_set is also a conversation and a connect, because the funnel is
# nested and rates computed off a non-nested funnel are nonsense.
_CONNECTED = {"connected", "callback", "meeting_set", "not_interested"}
_CONVERSATION = {"connected", "callback", "meeting_set", "not_interested"}
_MEETING = {"meeting_set"}


def record_outcome(conn, org_id, signal_keys, disposition, commit=True):
    """
    Attribute one call's outcome to every signal that was live when it was placed.

    Multi-touch attribution with no decay: if three signals put a target on the
    list, all three get credit for what happened. Splitting the credit would be
    more defensible statistically and would take an order of magnitude more
    volume to say anything, which for a customer in their first quarter means
    saying nothing at all.
    """
    if not signal_keys:
        return
    now = datetime.now().isoformat(timespec="seconds")
    connected = 1 if disposition in _CONNECTED else 0
    conversation = 1 if disposition in _CONVERSATION else 0
    meeting = 1 if disposition in _MEETING else 0

    for key in set(signal_keys):
        conn.execute(
            """INSERT OR IGNORE INTO signal_outcomes (org_id, signal_key, updated_at)
               VALUES (?,?,?)""", (org_id, key, now))
        conn.execute(
            """UPDATE signal_outcomes
                  SET attempts = attempts + 1,
                      connects = connects + ?,
                      conversations = conversations + ?,
                      meetings = meetings + ?,
                      updated_at = ?
                WHERE org_id = ? AND signal_key = ?""",
            (connected, conversation, meeting, now, org_id, key))
    if commit:
        conn.commit()


def record_deal_outcome(conn, org_id, signal_key, stage, dollars=0, commit=True):
    """Credit a signal when the opportunity it started advances or closes."""
    if not signal_key:
        return
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT OR IGNORE INTO signal_outcomes (org_id, signal_key, updated_at) "
                 "VALUES (?,?,?)", (org_id, signal_key, now))
    if stage == "won":
        conn.execute(
            """UPDATE signal_outcomes SET won = won + 1, dollars = dollars + ?,
                      updated_at = ? WHERE org_id = ? AND signal_key = ?""",
            (dollars, now, org_id, signal_key))
    else:
        conn.execute(
            """UPDATE signal_outcomes SET opportunities = opportunities + 1,
                      updated_at = ? WHERE org_id = ? AND signal_key = ?""",
            (now, org_id, signal_key))
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# Scoring one target
# ---------------------------------------------------------------------------

def score_target(org, company, contact, points, triggers, target,
                 weights=None, today=None, suppressed=False):
    """
    Score one target 0-100 and explain every point of it.

    Pure: takes rows, returns a dict. No database, no clock beyond `today`, no
    model. That is what makes it testable, and what makes the same list appear
    on a manager's screen and the rep's.

    Returns:
        score       0-100
        band/label  which bucket, for the badge
        signals     every contributing line, signed, sorted by magnitude
        why_now     the single strongest timing signal, for the card headline
        blockers    reasons this must not be dialed at all (see compliance.py)
        keys        the trigger kinds that were live — frozen onto the call row
                    so the learning loop can attribute the outcome later
    """
    today = today or date.today()
    weights = weights or {}
    signals, blockers, keys = [], [], []

    # --------------------------------------------------------- 1. Why now? --
    org_modes = set(_csv(org["modes"] if "modes" in org.keys() else "")) or {"ltl", "ftl"}
    timing_total = 0.0
    for t in triggers or []:
        spec = TRIGGER_TYPES.get(t["kind"])
        if not spec:
            continue
        age = _days_since(t["observed_at"], today)
        expiry = _d(t["expires_at"])
        if expiry and today > expiry:
            continue

        # Three multipliers on the base points, each of which can independently
        # make an event worthless: how old it is, how sure we are it happened,
        # and whether this org can serve the freight it implies.
        pts = decay(spec["points"], age, spec["half_life"])
        pts *= (t["strength"] or 60) / 100.0
        mode_overlap = org_modes & set(spec["modes"])
        if not mode_overlap:
            # Real news about freight this org cannot haul. Worth a fraction,
            # not zero — it still says the account is in motion.
            pts *= 0.35
        pts *= weights.get(t["kind"], 1.0)

        if pts < 1:
            continue
        timing_total += pts
        keys.append(t["kind"])
        age_txt = "today" if age == 0 else f"{age}d ago" if age is not None else "undated"
        note = "" if mode_overlap else " · outside your modes"
        signals.append(_sig(
            t["kind"], spec["label"],
            f"{t['headline']} — {t['source_name'] or 'source unrecorded'}, {age_txt}"
            f"{note}",
            pts, "good", "timing"))

    if not triggers:
        signals.append(_sig(
            "no_trigger", "No live buying signal",
            "Nothing has happened at this company that we know about. A cold call "
            "with no reason attached converts, but it converts worst.",
            -6, "medium", "timing"))

    # ----------------------------------------------------------- 2. Fit ----
    fit = 0.0
    co_modes = set(_csv(company["modes"]))
    overlap = org_modes & co_modes
    if co_modes:
        if overlap:
            fit += 9
            signals.append(_sig(
                "mode_fit", "Mode match",
                f"They move {', '.join(sorted(co_modes)).upper()}; you cover "
                f"{', '.join(sorted(overlap)).upper()}.",
                9, "good", "fit"))
        else:
            fit -= 12
            signals.append(_sig(
                "mode_miss", "No mode overlap",
                f"They move {', '.join(sorted(co_modes)).upper()} and you do not "
                f"cover any of it. Price will not fix this.",
                -12, "high", "fit"))

    loads = company["loads_per_month"] or 0
    if loads >= 120:
        fit += 12
        signals.append(_sig("volume_high", "High freight volume",
                            f"~{loads} loads/month. Large enough to matter, large "
                            f"enough to already have incumbents.", 12, "good", "fit"))
    elif loads >= 30:
        fit += 8
        signals.append(_sig("volume_good", "Meaningful freight volume",
                            f"~{loads} loads/month.", 8, "good", "fit"))
    elif loads and loads < 8:
        fit -= 6
        signals.append(_sig("volume_thin", "Low freight volume",
                            f"~{loads} loads/month — the account may not repay the "
                            f"cost of winning it.", -6, "medium", "fit"))

    # Revenue band. The floor is about buying power; the ceiling is about who
    # you would actually be competing with for the freight.
    rev = company["revenue_est"] or 0
    if 25_000_000 <= rev <= 500_000_000:
        fit += 7
        signals.append(_sig("size_band", "In the sweet spot",
                            f"~${rev/1e6:.0f}M revenue — big enough to ship real "
                            f"volume, small enough not to be locked into a national "
                            f"contract.", 7, "good", "fit"))
    elif rev > 2_000_000_000:
        fit -= 5
        signals.append(_sig("size_enterprise", "Enterprise procurement",
                            f"~${rev/1e9:.1f}B revenue. Freight this size moves "
                            f"through an annual RFP, not a phone call.",
                            -5, "medium", "fit"))

    home = set(s.strip().upper() for s in (org["home_markets"] or "").split(",") if s.strip())
    if home and (company["state"] or "").upper() in home:
        fit += 5
        signals.append(_sig("home_market", "In a home market",
                            f"{company['city']}, {company['state']} — you have "
                            f"density here, which is where margin comes from.",
                            5, "good", "fit"))

    # ------------------------------------------------- 3. Can I reach them? --
    reach = 0.0
    by_kind = {}
    for p in points or []:
        by_kind.setdefault(p["kind"], []).append(p)

    def best(kind):
        rows = by_kind.get(kind) or []
        return max(rows, key=lambda r: r["confidence"]) if rows else None

    direct = best("direct_dial") or best("mobile")
    email = best("email")

    if direct and direct["status"] == "verified":
        stale = _days_since(direct["last_verified"], today)
        if stale is not None and stale > 90:
            reach += 6
            signals.append(_sig(
                "dial_stale", "Direct dial verified, but not recently",
                f"Last checked {stale} days ago. People change desks; treat the "
                f"number as probably-good rather than good.", 6, "low", "reach"))
        else:
            reach += 16
            signals.append(_sig(
                "dial_verified", "Verified direct dial",
                f"{direct['value']} — {direct['method'] or 'verified'}, "
                f"{stale if stale is not None else '?'}d ago.", 16, "good", "reach"))
    elif direct and direct["status"] == "probable":
        reach += 6
        signals.append(_sig("dial_probable", "Unverified direct number",
                            f"{direct['value']} — from {direct['source']}, never "
                            f"independently confirmed.", 6, "low", "reach"))
    elif direct and direct["status"] == "guessed":
        reach += 1
        signals.append(_sig("dial_guessed", "Number is a guess",
                            "Constructed from a dialing pattern. Treat a connect as "
                            "luck, not as data.", 1, "medium", "reach"))
    elif by_kind.get("switchboard"):
        reach -= 2
        signals.append(_sig("switchboard_only", "Switchboard only",
                            "No direct line. You are calling the front desk and "
                            "asking for a name.", -2, "medium", "reach"))
    else:
        reach -= 14
        signals.append(_sig("no_phone", "No phone number at all",
                            "Nothing to dial. This is an email or a LinkedIn touch "
                            "until the number is sourced.", -14, "critical", "reach"))

    if email and email["status"] == "verified":
        reach += 7
        signals.append(_sig("email_verified", "Verified email",
                            f"{email['value']} — accepts mail.", 7, "good", "reach"))
    elif email and email["status"] == "catch_all":
        reach += 1
        signals.append(_sig("email_catchall", "Catch-all domain",
                            "The domain accepts every address, so acceptance proves "
                            "nothing about this one.", 1, "low", "reach"))
    elif email and email["status"] == "guessed":
        signals.append(_sig("email_guessed", "Email is a pattern guess",
                            f"{email['value']} — built from a naming convention. "
                            f"Expect a bounce about a third of the time.",
                            0, "low", "reach"))

    dead = [p for p in (points or []) if p["fail_count"] >= 3 or p["status"] == "invalid"]
    if dead:
        reach -= 10
        signals.append(_sig("bad_contact_data", "Contact data has failed before",
                            f"{len(dead)} number(s) or address(es) on this record "
                            f"have hard-failed. Somebody should re-source this "
                            f"contact before it costs another dial.",
                            -10, "high", "reach"))

    # --------------------------------------- 4. Right person, burned yet? ---
    person = 0.0
    if contact:
        if contact["status"] == "left_company":
            person -= 30
            signals.append(_sig("contact_gone", "This person has left",
                                "The record is stale. Whatever you knew about them "
                                "is now about somebody else's job.",
                                -30, "critical", "person"))
            blockers.append("Contact has left the company — re-source before dialing.")
        if contact["status"] == "do_not_contact":
            blockers.append("This contact asked not to be contacted.")

        role = contact["role_type"]
        if role == "decision_maker":
            person += 12
            signals.append(_sig("decision_maker", "Decision maker",
                                f"{contact['title']} — can say yes without running it "
                                f"upstairs.", 12, "good", "person"))
        elif role == "influencer":
            person += 5
            signals.append(_sig("influencer", "Influencer",
                                f"{contact['title']} — cannot sign, can kill it or "
                                f"carry it.", 5, "low", "person"))
        elif role == "gatekeeper":
            person -= 4
            signals.append(_sig("gatekeeper", "Gatekeeper",
                                f"{contact['title']} — a route to the buyer, not the "
                                f"buyer.", -4, "medium", "person"))

        # The most under-used field in B2B sales. Somebody 30-120 days into a
        # logistics role is rewriting the vendor list, has budget authority they
        # have not spent, and has no loyalty to the incumbent.
        tenure = _days_since(contact["role_started"], today)
        if tenure is not None and 21 <= tenure <= 180:
            person += 14
            signals.append(_sig("new_in_role", "New in the role",
                                f"Started {tenure} days ago. New logistics leaders "
                                f"review incumbents early and owe them nothing.",
                                14, "good", "person"))
        elif tenure is not None and tenure > 2500:
            person -= 3
            signals.append(_sig("entrenched", "Long tenure",
                                f"{tenure // 365} years in seat. Relationships with "
                                f"incumbents will be personal.", -3, "low", "person"))

    # ------------------------------------------------ engagement history ----
    if target:
        attempts = target["attempts"] or 0
        connects = target["connects"] or 0
        since_touch = _days_since(target["last_touch_at"], today)

        if connects and target["last_connect_at"]:
            person += 10
            signals.append(_sig("prior_connect", "You have spoken before",
                                f"{connects} prior conversation(s). A warm re-dial "
                                f"outperforms any cold one on this list.",
                                10, "good", "history"))

        if since_touch is not None and since_touch < COOLOFF_DAYS:
            pen = -18 + since_touch * 4
            person += pen
            signals.append(_sig("too_recent", "Called very recently",
                                f"Last touched {since_touch} day(s) ago. Dialing again "
                                f"now reads as pressure, not persistence.",
                                pen, "high", "history"))

        if attempts >= FATIGUE_ATTEMPTS and not connects:
            person -= 14
            signals.append(_sig("no_answer_fatigue", "Never connected",
                                f"{attempts} attempts, zero conversations. At this "
                                f"point the number is the more likely problem than "
                                f"their calendar.", -14, "high", "history"))

        snooze = _d(target["snoozed_until"])
        if snooze and snooze > today:
            blockers.append(f"Snoozed until {snooze.isoformat()}.")
        if target["status"] in ("disqualified", "lost"):
            blockers.append(f"Marked {target['status']}"
                            + (f": {target['dq_reason']}" if target["dq_reason"] else "."))

    if suppressed:
        blockers.append("On the suppression list — do not contact.")

    # ----------------------------------------------------------- totals ----
    # 30 is the neutral starting point: a company nobody knows anything about,
    # with a working number and a real title, lands in the low 40s and reads as
    # "nurture". Points are what lift it out of that, in either direction.
    raw = 30 + timing_total + fit + reach + person
    score = int(round(soft_cap(raw)))
    if blockers:
        # A blocked target keeps its score for the manager's view — the question
        # "how much is this suppression costing us" is worth being able to
        # answer — but the daily list filters it out before a rep sees it.
        pass

    signals.sort(key=lambda s: -abs(s["points"]))
    timing = [s for s in signals if s["category"] == "timing" and s["points"] > 0]
    band, label = band_for(score)

    return {
        "score": score,
        "band": band,
        "band_label": label,
        "signals": signals,
        "why_now": timing[0] if timing else None,
        "timing_points": round(timing_total, 1),
        "fit_points": round(fit, 1),
        "reach_points": round(reach, 1),
        "person_points": round(person, 1),
        "blockers": blockers,
        "keys": sorted(set(keys)),
    }


# ---------------------------------------------------------------------------
# Scoring the list
# ---------------------------------------------------------------------------

def load_and_score(conn, org, target_ids=None, rep_id=None, statuses=None,
                   today=None, limit=None):
    """
    Score many targets in a fixed number of queries.

    The naive version of this issues four queries per target, which at 4,000
    targets is 16,000 round trips and a daily list that takes nine seconds to
    build. Everything is fetched in bulk and joined in Python instead.
    """
    today = today or date.today()
    org_id = org["id"]
    weights = learned_weights(conn, org_id)

    where = ["t.org_id = ?"]
    params = [org_id]
    if rep_id is not None:
        where.append("t.rep_id = ?")
        params.append(rep_id)
    if statuses:
        where.append("t.status IN (%s)" % ",".join("?" * len(statuses)))
        params.extend(statuses)
    if target_ids:
        where.append("t.id IN (%s)" % ",".join("?" * len(target_ids)))
        params.extend(target_ids)

    targets = conn.execute(
        f"""SELECT t.* FROM targets t WHERE {' AND '.join(where)}""", tuple(params)
    ).fetchall()
    if not targets:
        return []

    company_ids = {t["company_id"] for t in targets}
    contact_ids = {t["contact_id"] for t in targets if t["contact_id"]}

    companies = {c["id"]: c for c in _in_chunks(
        conn, "SELECT * FROM companies WHERE org_id = ? AND id IN (%s)", org_id, company_ids)}
    contacts = {c["id"]: c for c in _in_chunks(
        conn, "SELECT * FROM contacts WHERE org_id = ? AND id IN (%s)", org_id, contact_ids)}

    points = {}
    for p in _in_chunks(conn,
                        "SELECT * FROM contact_points WHERE org_id = ? AND contact_id IN (%s)",
                        org_id, contact_ids):
        points.setdefault(p["contact_id"], []).append(p)

    trigs = {}
    for t in _in_chunks(conn,
                        "SELECT * FROM triggers WHERE org_id = ? AND company_id IN (%s) "
                        "ORDER BY observed_at DESC", org_id, company_ids):
        trigs.setdefault(t["company_id"], []).append(t)

    # One pass over the suppression list rather than a lookup per number. Values
    # are stored normalized, so the comparison has to normalize too.
    supp = {r["value"] for r in conn.execute(
        "SELECT value FROM suppression WHERE org_id = ?", (org_id,)).fetchall()}

    def _blocked(points):
        for p in points:
            key = (normalize_email(p["value"]) if p["kind"] == "email"
                   else normalize_phone(p["value"]))
            if key and key in supp:
                return True
        return False

    out = []
    for t in targets:
        company = companies.get(t["company_id"])
        if company is None:
            continue
        contact = contacts.get(t["contact_id"])
        pts = points.get(t["contact_id"], [])
        blocked = _blocked(pts)
        result = score_target(org, company, contact, pts, trigs.get(t["company_id"], []),
                              t, weights=weights, today=today, suppressed=blocked)
        out.append({"target": t, "company": company, "contact": contact,
                    "points": pts, "triggers": trigs.get(t["company_id"], []),
                    **result})

    out.sort(key=lambda r: -r["score"])
    return out[:limit] if limit else out


def _in_chunks(conn, sql_template, org_id, ids, chunk=400):
    """
    Run an `IN (...)` query in batches.

    SQLite's default variable limit is 999 and Postgres has its own ceiling;
    a 4,000-target org would blow straight through either one. Chunking here
    means callers never have to think about it.
    """
    ids = [i for i in ids if i is not None]
    rows = []
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        if not batch:
            continue
        sql = sql_template % ",".join("?" * len(batch))
        rows.extend(conn.execute(sql, (org_id, *batch)).fetchall())
    return rows


def persist_scores(conn, scored, commit=True):
    """
    Cache the score on the target row.

    The daily list is then one indexed query instead of a full rescore, which
    matters the moment an org has more than a few thousand targets. The cache is
    refreshed whenever anything that feeds it changes — a call, a new trigger,
    an enrichment run — and on a nightly rescore.
    """
    now = datetime.now().isoformat(timespec="seconds")
    for row in scored:
        conn.execute(
            "UPDATE targets SET score = ?, score_at = ?, signals_json = ? "
            "WHERE id = ? AND org_id = ?",
            (row["score"], now,
             json.dumps({"signals": row["signals"], "keys": row["keys"],
                         "why_now": row["why_now"], "blockers": row["blockers"]}),
             row["target"]["id"], row["target"]["org_id"]))
    if commit:
        conn.commit()


def daily_list(conn, org, rep_id, size=25, today=None):
    """
    The product, in one function: who to call today and why.

    Blocked targets are filtered out here rather than scored to zero, because a
    suppression entry is a legal instruction and not a ranking input.
    """
    scored = load_and_score(conn, org, rep_id=rep_id,
                            statuses=("new", "working", "meeting_set"), today=today)
    live = [r for r in scored if not r["blockers"]]
    return live[:size], scored


def best_hours(conn, org_id, rep_id=None):
    """
    When this org's calls actually connect, by hour of the local business day.

    Computed from the org's own call history rather than an industry study,
    because "Tuesday at 4pm" is an average over industries that do not include
    this one. Returns [] until there is enough history to say anything, and the
    UI says so rather than drawing an empty chart.
    """
    where = ["org_id = ?", "started_at IS NOT NULL", "started_at != ''"]
    params = [org_id]
    if rep_id:
        where.append("rep_id = ?")
        params.append(rep_id)
    rows = conn.execute(
        f"SELECT started_at, disposition FROM calls WHERE {' AND '.join(where)}",
        tuple(params)).fetchall()

    buckets = {}
    for r in rows:
        dt = _dt(r["started_at"])
        if not dt or not (6 <= dt.hour <= 19):
            continue
        b = buckets.setdefault(dt.hour, {"attempts": 0, "connects": 0})
        b["attempts"] += 1
        if r["disposition"] in _CONNECTED:
            b["connects"] += 1

    out = [{"hour": h, "attempts": v["attempts"], "connects": v["connects"],
            "rate": v["connects"] / v["attempts"] if v["attempts"] else 0}
           for h, v in sorted(buckets.items()) if v["attempts"] >= 5]
    return out
