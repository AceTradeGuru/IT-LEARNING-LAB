"""
QuotaOS — the AI layer behind hiring.

Four capabilities, each with two implementations:

    write_posting()    req knobs            -> a finished job posting
    search_resumes()   plain-English query  -> ranked candidates with reasons
    build_interview()  req + interview kind -> structured questions and a rubric
    score_interview()  questions + answers  -> per-answer scores and a recommendation

Every function returns `(result, engine)` where engine is "claude" or "offline".

Why two implementations. The Claude path (`claude-opus-5`) is the real product:
it reads a resume the way a recruiter does and scores an answer against a rubric
rather than counting keywords. The offline path is a deterministic heuristic that
runs with no API key, no network, and no cost — so the repo clones and demos on a
laptop, and so a rate limit or an outage degrades the feature instead of breaking
the page. The UI labels which one produced any given artifact; a scorecard whose
provenance is ambiguous is a scorecard nobody should act on.

Credentials resolve the normal way — ANTHROPIC_API_KEY, or an `ant auth login`
profile. Nothing is hardcoded and nothing is prompted for.
"""

import json
import os
import re

MODEL = "claude-opus-5"

# Recruiting judgment is exactly the kind of task where a cheap answer is worse
# than no answer, so this runs at high effort rather than the cost-optimized tier.
EFFORT = "high"

# Claude Opus 5 list price, US cents per million tokens. Used to convert the usage
# the API reports into a number that can be checked against an org's monthly cap.
# Update these if pricing changes — they are not fetched at runtime on purpose,
# because a billing guard that depends on a network call fails open.
CENTS_PER_MTOK_IN = 500
CENTS_PER_MTOK_OUT = 2500

# Every _ask() appends the cost of that call here; the caller drains it. A module
# global rather than a return value so the four public functions keep their
# (result, engine) signature and callers that ignore cost stay correct.
_last_call_cents = []


def drain_cost_cents():
    """Return and clear the cost of model calls made since the last drain."""
    total = sum(_last_call_cents)
    _last_call_cents.clear()
    return total


def estimate_cents(usage):
    if usage is None:
        return 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    plain = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    # Cache reads bill at ~0.1x and writes at ~1.25x of the input rate.
    billable_in = plain + write * 1.25 + read * 0.1
    return (billable_in * CENTS_PER_MTOK_IN + out * CENTS_PER_MTOK_OUT) / 1_000_000

_client = None
_client_checked = False


def get_client():
    """Lazily construct the Anthropic client. Returns None if unavailable."""
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    try:
        import anthropic
        _client = anthropic.Anthropic()
        # A client with no resolvable credentials constructs fine and fails at
        # request time, so probe for one of the credential sources up front.
        if not (os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or _has_cli_profile()):
            _client = None
    except Exception:
        _client = None
    return _client


def _has_cli_profile():
    base = os.environ.get("ANTHROPIC_CONFIG_DIR") or os.path.expanduser("~/.config/anthropic")
    return os.path.isdir(os.path.join(base, "credentials"))


def ai_available():
    return get_client() is not None


def engine_status():
    if ai_available():
        return {"live": True, "label": f"Claude ({MODEL})",
                "detail": "Postings, resume ranking, and interview scoring run on Claude."}
    return {"live": False, "label": "Offline heuristics",
            "detail": "No Anthropic credentials found — running the deterministic "
                      "fallback engine. Set ANTHROPIC_API_KEY to switch on Claude."}


def _ask(system, prompt, schema=None, max_tokens=16000):
    """
    One call to Claude. Returns parsed JSON when a schema is given, else text.
    Raises on any failure so the caller can fall back.
    """
    client = get_client()
    if client is None:
        raise RuntimeError("no anthropic credentials")

    kwargs = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"effort": EFFORT},
    }
    if schema:
        kwargs["output_config"]["format"] = {"type": "json_schema", "schema": schema}

    response = client.messages.create(**kwargs)

    # Safety classifiers can decline with a 200 and an empty content list, so
    # check the stop reason before indexing into content.
    if response.stop_reason == "refusal":
        raise RuntimeError("request declined by safety classifiers")

    _last_call_cents.append(estimate_cents(getattr(response, "usage", None)))

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise RuntimeError("empty response")
    return json.loads(text) if schema else text


# ---------------------------------------------------------------------------
# 1. Job postings
# ---------------------------------------------------------------------------

_POSTING_SYSTEM = """You write job postings for a B2B SaaS sales organization.

Write the posting a strong candidate would actually finish reading. Concrete over
aspirational: name the territory, the number, the segment, and what the first
ninety days look like. No "rockstar", no "wear many hats", no filler adjectives.

Return GitHub-flavored Markdown with these sections and nothing else:
a one-line hook, ## The role, ## What you'll own (bullets), ## The first 90 days,
## What we're looking for (bullets, must-haves first), ## Nice to have,
## Compensation, ## How we interview.

Under "How we interview", describe a four-step loop: recruiter screen, structured
AI-assisted interview scored against a published rubric, panel with the hiring
manager and two peers, and a final territory plan review. Be explicit that the
rubric is shared with candidates in advance."""


def write_posting(req):
    """Generate a job posting from req knobs. Returns (markdown, engine)."""
    prompt = f"""Write the posting for this requisition.

Title: {req['title']}
Level: {req['level']}
Team: {req['team']}
Region: {req['region']}
Segment: {req['segment']}
Annual quota: ${req['quota_annual']:,}
Base salary: ${req['comp_base']:,}
On-target earnings: ${req['comp_ote']:,}

Must have:
{req['must_haves']}

Nice to have:
{req['nice_haves'] or '(none specified)'}"""
    try:
        return _ask(_POSTING_SYSTEM, prompt), "claude"
    except Exception:
        return _offline_posting(req), "offline"


def _offline_posting(req):
    musts = [m.strip(" -•") for m in (req["must_haves"] or "").splitlines() if m.strip()]
    nices = [m.strip(" -•") for m in (req["nice_haves"] or "").splitlines() if m.strip()]
    quota = req["quota_annual"]
    return f"""Own a {req['segment']} territory in {req['region']} carrying a ${quota:,} number.

## The role

We're hiring a **{req['title']}** onto the {req['team']} team. You'll carry a
${quota:,} annual quota against {req['segment']} accounts in {req['region']},
reporting to the {req['team']} sales manager.

This is a full-cycle seat: you source, you run the cycle, you close, and you own
the renewal conversation with your accounts.

## What you'll own

- A named {req['segment']} territory in {req['region']} and the ${quota:,} attached to it
- Full-cycle execution from first touch through signature
- Pipeline generation — roughly half of your pipeline is expected to be self-sourced
- Accurate forecasting: close dates that hold and next steps on every open deal

## The first 90 days

- **Days 1-30** — product and methodology certification, shadow four live cycles,
  build your territory map.
- **Days 31-60** — run your own discovery calls with manager support, first
  qualified opportunities in pipeline.
- **Days 61-90** — carry a full pipeline independently, first close expected in
  the quarter following ramp.

## What we're looking for

{chr(10).join('- ' + m for m in musts) if musts else '- Full-cycle B2B sales experience'}

## Nice to have

{chr(10).join('- ' + n for n in nices) if nices else '- Experience selling into this segment'}

## Compensation

${req['comp_base']:,} base, ${req['comp_ote']:,} on-target earnings, uncapped
commission. Equity and full benefits from day one.

## How we interview

1. **Recruiter screen** (30 min) — role, territory, and comp alignment.
2. **Structured interview** (45 min) — scored against a published rubric. You get
   the rubric before the conversation; there are no trick questions.
3. **Panel** (60 min) — hiring manager plus two peers you'd sell alongside.
4. **Territory plan review** (45 min) — you present how you'd attack the patch.

_Posting generated by the QuotaOS offline template engine._"""


# ---------------------------------------------------------------------------
# 2. Resume search
# ---------------------------------------------------------------------------

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "integer"},
                    "score": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["strong", "possible", "weak"]},
                    "reason": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["candidate_id", "score", "verdict", "reason", "evidence", "gaps"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

_SEARCH_SYSTEM = """You are a sales recruiting sourcer reading resumes against a
plain-English brief from a hiring manager.

Score each candidate 0-100 on fit for the brief specifically — not on how good the
resume is in general. A brilliant enterprise closer is a weak match for an SMB
volume seat, and you should say so.

Rules:
- `evidence` quotes or closely paraphrases the resume line that earned the score.
  Never cite something the resume does not say.
- `gaps` names what the brief asked for that the resume does not show. An empty
  list means the resume genuinely covers everything asked.
- `reason` is one sentence a hiring manager can act on.
- Score every candidate you are given, including weak ones. Ranking is the caller's
  job; your job is an honest read on each."""


def search_resumes(query, candidates, req=None):
    """Rank candidates against a natural-language brief. Returns (results, engine)."""
    if not candidates:
        return [], "offline"

    context = ""
    if req:
        context = (f"\nThe open req: {req['title']} — {req['level']}, {req['segment']} "
                   f"segment, {req['region']}, ${req['quota_annual']:,} quota.\n"
                   f"Must have:\n{req['must_haves']}\n")

    blob = "\n\n".join(
        f"[candidate_id {c['id']}] {c['name']} — {c['current_title']} at "
        f"{c['current_company']}, {c['years_exp']} years experience, {c['location']}\n"
        f"{c['resume_text']}"
        for c in candidates
    )
    prompt = f"""Hiring manager's brief:
\"\"\"{query}\"\"\"
{context}
Candidates:

{blob}"""

    try:
        data = _ask(_SEARCH_SYSTEM, prompt, schema=_SEARCH_SCHEMA)
        by_id = {c["id"]: c for c in candidates}
        results = [r for r in data["results"] if r["candidate_id"] in by_id]
        results.sort(key=lambda r: -r["score"])
        return results, "claude"
    except Exception:
        return _offline_search(query, candidates), "offline"


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "for", "from",
    "has", "have", "i", "in", "is", "it", "me", "of", "on", "or", "someone", "that",
    "the", "them", "they", "this", "to", "want", "who", "with", "years", "year",
    "experience", "looking", "need", "find", "sales", "rep",
}


# The handful of places where sales resumes and sales briefs use different words
# for the same thing. Not semantics — just the vocabulary gap a keyword matcher
# would otherwise fall straight into ("healthcare" vs "hospital systems").
_SYNONYMS = {
    "healthcare": ["hospital", "clinical", "payer", "health system", "life sciences", "cmio"],
    "health": ["hospital", "clinical", "payer", "health system"],
    "fintech": ["bank", "financial services", "asset manager", "bancorp"],
    "financial": ["bank", "fintech", "asset manager", "capital"],
    "closer": ["closed", "attainment", "quota", "full-cycle"],
    "self-sourced": ["self-sourced", "outbound", "prospecting", "sourced"],
    "outbound": ["self-sourced", "prospecting", "sequence", "cold"],
    "regulated": ["security review", "procurement", "compliance", "review board"],
    "security-review": ["infosec", "security review", "security"],
    "enterprise": ["enterprise", "strategic"],
    "volume": ["velocity", "open opportunities", "high-velocity", "live opportunities"],
    "velocity": ["volume", "high-velocity", "short cycle"],
    "sdr": ["sdr", "bdr", "promoted"],
    "multi-threading": ["stakeholder", "multi-thread", "champion"],
}


def _offline_search(query, candidates):
    """
    Keyword scoring with a few sales-specific rules.

    Not pretending to be semantic. It tokenizes the brief, weights term hits by
    where they land (title beats resume body), and extracts two things a sales
    brief almost always contains: a years-of-experience floor and a quota floor.
    """
    q = query.lower()
    terms = [t for t in re.findall(r"[a-z][a-z\-\+/]{2,}", q) if t not in _STOPWORDS]

    min_years = 0
    ym = re.search(r"(\d+)\s*\+?\s*(?:years|yrs)", q)
    if ym:
        min_years = int(ym.group(1))

    min_quota = 0
    qm = re.search(r"\$\s*([\d.]+)\s*([mk])", q)
    if qm:
        min_quota = int(float(qm.group(1)) * (1_000_000 if qm.group(2) == "m" else 1_000))

    results = []
    for c in candidates:
        title = (c["current_title"] or "").lower()
        company = (c["current_company"] or "").lower()
        body = (c["resume_text"] or "").lower()

        # Term coverage is normalized against how much of the brief could have
        # matched, rather than summed raw. A flat sum saturates at the 100 cap and
        # every decent resume ties — which silently turns the ranking alphabetical.
        hits, evidence = [], []
        term_points = 0.0
        for t in terms:
            in_title = t in title or t in company
            in_body = t in body
            # A synonym hit counts, but for less than the term itself.
            alias = next((s for s in _SYNONYMS.get(t, []) if s in body or s in title), None)
            if in_title:
                term_points += 8
            elif in_body:
                term_points += 4
            elif alias:
                term_points += 3

            if in_title or in_body or alias:
                hits.append(t)
                needle = t if (in_title or in_body) else alias
                line = next((l.strip() for l in (c["resume_text"] or "").splitlines()
                             if needle in l.lower() and l.strip()), None)
                if line and line not in evidence and len(evidence) < 3:
                    evidence.append(line)

        ceiling = max(len(terms) * 5.0, 1.0)          # body-matching every term == full marks
        score = 20 + min(term_points / ceiling, 1.0) * 45

        gaps = []
        if min_years:
            if c["years_exp"] >= min_years:
                score += 15
            else:
                score -= 20
                gaps.append(f"{c['years_exp']} years experience, brief asked for {min_years}+")

        if min_quota:
            found = [int(float(m[0]) * (1_000_000 if m[1].lower() == "m" else 1_000))
                     for m in re.findall(r"\$\s*([\d.]+)\s*([mMkK])", c["resume_text"] or "")]
            if found and max(found) >= min_quota:
                score += 20
                evidence.append(f"Carried a quota of ${max(found):,}")
            else:
                score -= 12
                gaps.append(f"No evidence of a ${min_quota:,}+ quota on the resume")

        missed = [t for t in terms if t not in hits]
        if missed:
            gaps.append("Brief terms not found in resume: " + ", ".join(missed[:4]))

        score = max(0, min(100, round(score)))
        verdict = "strong" if score >= 65 else "possible" if score >= 35 else "weak"
        matched = ", ".join(hits[:4]) or "nothing in the brief"
        results.append({
            "candidate_id": c["id"],
            "score": score,
            "verdict": verdict,
            "reason": f"Matched on {matched}. {c['years_exp']} years, currently "
                      f"{c['current_title']} at {c['current_company']}.",
            "evidence": evidence[:3],
            "gaps": gaps[:3],
        })

    results.sort(key=lambda r: -r["score"])
    return results


# ---------------------------------------------------------------------------
# 3. Interview generation
# ---------------------------------------------------------------------------

_INTERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "competency": {"type": "string"},
                    "question": {"type": "string"},
                    "looking_for": {"type": "string"},
                    "red_flags": {"type": "string"},
                },
                "required": ["id", "competency", "question", "looking_for", "red_flags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

INTERVIEW_KINDS = {
    "screen": "a 30-minute recruiter screen covering motivation, track record, and territory fit",
    "role_play": "a role-play focused interview: discovery technique, objection handling, and "
                 "multi-threading, with at least two questions framed as live scenarios",
    "technical": "a deal-mechanics interview: forecasting discipline, pipeline math, "
                 "MEDDPICC-style qualification, and CRM hygiene",
}

_INTERVIEW_SYSTEM = """You design structured interviews for sales roles.

A structured interview means every candidate gets the same questions and every
answer is scored against criteria written before anyone answered — that is what
makes the scores comparable and the process defensible.

For each question, write:
- `competency`: the single thing this question measures (2-4 words)
- `question`: what the interviewer asks, verbatim
- `looking_for`: what a strong answer contains — be specific and observable
  ("names the economic buyer and how they got to them", not "shows good judgment")
- `red_flags`: what a weak answer looks like

Ask about what the candidate has actually done, not what they would hypothetically
do. Avoid questions with a single obvious right answer, and avoid anything touching
protected characteristics, personal circumstances, or current compensation."""


def build_interview(req, kind="screen", count=6):
    """Generate a structured interview. Returns (questions, engine)."""
    prompt = f"""Design {count} questions for {INTERVIEW_KINDS.get(kind, INTERVIEW_KINDS['screen'])}.

The role: {req['title']} ({req['level']}), {req['team']} team, {req['segment']}
segment, {req['region']}. Annual quota ${req['quota_annual']:,}.

Must have:
{req['must_haves']}

Number the questions 1 through {count}."""
    try:
        data = _ask(_INTERVIEW_SYSTEM, prompt, schema=_INTERVIEW_SCHEMA)
        return data["questions"][:count], "claude"
    except Exception:
        return _offline_interview(req, kind, count), "offline"


def _offline_interview(req, kind, count):
    seg = req["segment"]
    bank = {
        "screen": [
            ("Track record", f"Walk me through your last four quarters — quota, attainment, "
                             f"and what drove the variance.",
             "Specific numbers per quarter, honest about a miss, and a causal explanation "
             "rather than a market excuse.",
             "Round numbers with no detail, or attainment quoted only as a lifetime average."),
            ("Self-sourcing", "What percentage of your pipeline did you source yourself, and "
                              "what was the motion?",
             "A real percentage and a repeatable motion they can describe step by step.",
             "'Marketing gave me leads' with no self-sourced motion at all."),
            ("Segment fit", f"Tell me about your largest {seg} deal — cycle length, "
                            f"stakeholders, and how it closed.",
             f"Deal shape that matches {seg}: right cycle length, right number of stakeholders.",
             "A deal from a very different segment presented as equivalent experience."),
            ("Loss analysis", "Tell me about a deal you were confident in that you lost. "
                              "What did you miss?",
             "Names a specific qualification failure and what they changed after it.",
             "Blames procurement, price, or the product with no self-assessment."),
            ("Motivation", "Why this role, and why now?",
             "A reason connected to the territory, segment, or product — not just comp.",
             "Generic enthusiasm, or a reason that would apply to any job posting."),
            ("Territory thinking", f"You inherit a {seg} patch in {req['region']} on day one. "
                                   f"What are your first two weeks?",
             "Prioritizes account research and existing pipeline triage before outbound volume.",
             "Jumps straight to 'start dialing' with no segmentation."),
        ],
        "role_play": [
            ("Discovery", "I'm a VP of Operations who took your meeting as a favor. "
                          "Run the first five minutes.",
             "Earns the right to ask, then asks about a business outcome before the product.",
             "Opens with a product pitch or a scripted rapport question."),
            ("Objection handling", "'We already have a vendor for this and the contract "
                                   "renews in eleven months.' Go.",
             "Explores what's working and what isn't rather than attacking the incumbent.",
             "Discounts immediately, or argues that the incumbent is bad."),
            ("Multi-threading", "Your champion goes dark two weeks before close. "
                                "Walk me through the next 72 hours.",
             "Already has a second thread; escalates through business value, not guilt.",
             "Sends increasingly urgent emails to the same single contact."),
            ("Qualification", "How do you decide a deal does not belong in your forecast?",
             "A named, consistent bar — a metric, a champion test, or a paper-process check.",
             "'Gut feel' or a bar that moves with how the quarter is going."),
            ("Negotiation", "Procurement asks for 30% off in exchange for a two-year term. "
                            "Respond.",
             "Trades concession for something of value and involves the economic buyer.",
             "Accepts, or refuses flatly without exploring the trade."),
            ("Coachability", "Describe feedback from a manager that changed how you sell.",
             "Specific feedback, a specific behavior change, and a specific result.",
             "Feedback that is actually a humblebrag, or none they can recall."),
        ],
        "technical": [
            ("Forecast discipline", "How do you decide what goes in commit versus best case?",
             "Explicit criteria per category, applied the same way every week.",
             "Categories assigned by how confident they feel that day."),
            ("Pipeline math", f"You need ${req['quota_annual'] // 4:,} this quarter and you're "
                              f"at 40%. What coverage do you need and how do you get there?",
             "Works backward through win rate and cycle length to a real prospecting number.",
             "Answers with effort ('work harder') instead of arithmetic."),
            ("Qualification framework", "Walk me through how you qualify with MEDDPICC or "
                                        "whatever framework you use.",
             "Uses a framework as a gap-finding tool, not a form filled in after the fact.",
             "Recites the acronym without describing how a gap changes their behavior."),
            ("CRM hygiene", "What does your CRM look like on a Friday afternoon?",
             "Next step on every open deal and close dates they'd defend in a review.",
             "Treats CRM as admin overhead done before QBRs."),
            ("Slippage", "A deal has pushed twice. What do you do the third time?",
             "Re-qualifies the compelling event rather than moving the date again.",
             "Pushes it a third time and keeps it in the forecast."),
            ("Deal review", "Bring me a deal you'd want help on and tell me the gap.",
             "Names the actual gap — usually access to power or a missing compelling event.",
             "Presents a deal with no gaps, which means they haven't found them yet."),
        ],
    }
    picked = bank.get(kind, bank["screen"])[:count]
    return [
        {"id": i + 1, "competency": c, "question": q, "looking_for": lf, "red_flags": rf}
        for i, (c, q, lf, rf) in enumerate(picked)
    ]


# ---------------------------------------------------------------------------
# 4. Interview scoring
# ---------------------------------------------------------------------------

_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "integer"},
                    "rationale": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["id", "score", "rationale", "evidence"],
                "additionalProperties": False,
            },
        },
        "overall_score": {"type": "integer"},
        "recommendation": {"type": "string", "enum": ["advance", "hold", "reject"]},
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "follow_ups": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scores", "overall_score", "recommendation", "summary",
                 "strengths", "concerns", "follow_ups"],
    "additionalProperties": False,
}

_SCORE_SYSTEM = """You score structured interview answers against a rubric that was
written before the candidate answered.

For each answer, score 0-100 against that question's `looking_for` and `red_flags`
only. Do not reward polish, confidence, or length — reward specificity and evidence.
A short answer with two real numbers beats three paragraphs of narrative.

- `evidence` quotes the part of the answer that drove the score. If the answer
  contained nothing relevant, say so plainly rather than inventing a quote.
- `rationale` is one sentence tying the score to the rubric.
- `overall_score` weights the competencies most load-bearing for this role; it is
  not required to be the arithmetic mean.
- `recommendation`: advance, hold, or reject. Be willing to say reject — a scoring
  system that never rejects is not a filter.
- `follow_ups` are the questions the next round should ask to close what this
  interview could not resolve.

Score only what the candidate said. Ignore their name, background, and anything
that is not evidence of ability to do this job."""


def score_interview(req, questions, answers):
    """Score answers against the rubric. Returns (result, engine)."""
    blocks = []
    for q in questions:
        a = (answers.get(str(q["id"])) or answers.get(q["id"]) or "").strip()
        blocks.append(
            f"Question {q['id']} — {q['competency']}\n"
            f"Asked: {q['question']}\n"
            f"Rubric — strong answer: {q['looking_for']}\n"
            f"Rubric — red flags: {q['red_flags']}\n"
            f"Candidate answered: {a or '(no answer given)'}"
        )
    prompt = (f"Role: {req['title']} ({req['level']}), {req['segment']} segment, "
              f"${req['quota_annual']:,} quota.\n\n" + "\n\n".join(blocks))
    try:
        data = _ask(_SCORE_SYSTEM, prompt, schema=_SCORE_SCHEMA)
        return data, "claude"
    except Exception:
        return _offline_score(questions, answers), "offline"


def _offline_score(questions, answers):
    """
    Heuristic scoring: rewards specificity, penalizes vagueness.

    It looks for the things a strong sales answer contains — numbers, dollar
    figures, percentages, named roles, and concrete outcome language — and for
    the hedges that usually replace them. Crude next to the Claude path, and
    labeled as such in the UI, but it ranks a specific answer above a vague one,
    which is the point.
    """
    specifics = re.compile(r"(\$[\d,.]+\s*[mMkK]?|\b\d+%|\b\d{2,}\b)")
    strong_words = ("closed", "quota", "champion", "economic buyer", "pipeline",
                    "discovery", "next step", "cycle", "renewal", "forecast",
                    "sourced", "attainment", "stakeholder", "procurement", "problem",
                    "demo", "proposal", "deal", "meeting", "objection", "outbound",
                    "compelling event", "budget owner", "win rate", "referral")
    hedges = ("i would probably", "i guess", "kind of", "sort of", "i think maybe",
              "not really sure", "not sure", "hard to say", "i'd have to think")

    scores, total = [], 0
    for q in questions:
        a = (answers.get(str(q["id"])) or answers.get(q["id"]) or "").strip()
        low = a.lower()
        if not a:
            scores.append({"id": q["id"], "score": 0,
                           "rationale": "No answer submitted for this question.",
                           "evidence": "(no answer given)"})
            continue

        words = len(a.split())
        n_specifics = len(specifics.findall(a))
        n_strong = sum(1 for w in strong_words if w in low)
        n_hedge = sum(1 for h in hedges if h in low)

        # No bonus for length. The rubric says a short answer with two real numbers
        # beats three paragraphs of narrative, so only answers too short to contain
        # any evidence are penalized.
        s = 35
        s += min(n_specifics, 4) * 13      # numbers are the strongest signal
        s += min(n_strong, 4) * 6          # domain vocabulary used in context
        s -= n_hedge * 10
        s -= 20 if words < 15 else (8 if words < 25 else 0)
        s = max(0, min(100, s))

        if n_specifics:
            bit = specifics.search(a)
            evidence = a[max(0, bit.start() - 60):bit.end() + 60].strip()
        else:
            evidence = a[:120].strip()

        if s >= 68:
            rationale = (f"Concrete answer — {n_specifics} specific figure(s) and "
                         f"{n_strong} on-topic term(s) against the rubric.")
        elif s >= 45:
            rationale = ("Partially meets the rubric: on topic, but light on the "
                         "specifics a strong answer would contain.")
        else:
            rationale = ("Falls short of the rubric — vague or too brief to "
                         "demonstrate the competency.")

        scores.append({"id": q["id"], "score": s, "rationale": rationale, "evidence": evidence})
        total += s

    overall = round(total / len(questions)) if questions else 0
    if overall >= 68:
        rec, summary = "advance", ("Answers were specific and well-evidenced across "
                                   "most competencies. Worth a panel.")
    elif overall >= 45:
        rec, summary = "hold", ("Mixed. Enough signal to keep warm, not enough to "
                                "commit panel time until the gaps are closed.")
    else:
        rec, summary = "reject", ("Answers lacked the specificity the rubric calls for. "
                                  "No evidence of the required track record.")

    ranked = sorted(scores, key=lambda s: -s["score"])
    by_id = {q["id"]: q for q in questions}
    strengths = [f"{by_id[s['id']]['competency']} — scored {s['score']}"
                 for s in ranked[:2] if s["score"] >= 60]
    concerns = [f"{by_id[s['id']]['competency']} — scored {s['score']}"
                for s in ranked[::-1][:2] if s["score"] < 60]

    return {
        "scores": scores,
        "overall_score": overall,
        "recommendation": rec,
        "summary": summary + " (Scored by the offline heuristic engine.)",
        "strengths": strengths or ["No competency scored above 60."],
        "concerns": concerns or ["No competency scored below 60."],
        "follow_ups": [f"Re-ask {by_id[s['id']]['competency']} with a request for "
                       f"specific numbers." for s in ranked[::-1][:2]],
    }
