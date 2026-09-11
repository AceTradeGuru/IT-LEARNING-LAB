"""
Royal Grind — the intelligence layer.

Five capabilities, each with two implementations:

    parse_search()      plain-English brief   -> structured search filters
    research_company()  company + signals     -> why they are worth a call now
    build_call_plan()   research + contact    -> opening, discovery, objections
    analyze_call()      transcript            -> summary, commitments, next action
    draft_email()       call outcome          -> the follow-up that gets sent

Every function returns `(result, engine)` where engine is "claude" or "offline".

**Why two implementations.** The Claude path (`claude-opus-5`) is the product:
it reads a freight profile the way a good broker does and pulls a commitment out
of a messy transcript. The offline path is deterministic template logic that
runs with no API key, no network, and no cost — so the repo clones and demos on
a laptop, and so a rate limit or an outage degrades a feature instead of
breaking a page mid-call. It is crude next to the Claude path and it is labelled
as such everywhere it appears. A call script whose provenance is ambiguous is a
call script a rep should not read aloud to a customer.

**Structured outputs everywhere the result reaches the database.** A call plan
is rendered into six named sections and a call analysis writes a disposition and
a follow-up date onto real rows. Parsing that out of prose hopefully is how a
CRM ends up with a task due on the 31st of February.

**The prompts carry the org's own pitch.** `orgs.pitch` and `orgs.modes` are in
every system prompt, so the script offers the capacity this broker actually has.
Generic AI sales copy is worse than no script: it puts a rep on a call promising
reefer capacity to a shipper when the company runs dry van only.

Credentials resolve the normal way — ANTHROPIC_API_KEY, or an `ant auth login`
profile. Nothing is hardcoded and nothing is prompted for.
"""

import json
import os
import re
from datetime import date, timedelta

MODEL = "claude-opus-5"

# A sales script that is merely plausible costs a rep the call. This runs at high
# effort rather than the cost-optimized tier; the guard on the bill is the
# per-org cap in app.py, not a cheaper model.
EFFORT = "high"

# Claude Opus 5 list price, US cents per million tokens. Used to convert reported
# usage into a number checkable against an org's monthly cap. Not fetched at
# runtime on purpose — a billing guard that depends on a network call fails open.
CENTS_PER_MTOK_IN = 500
CENTS_PER_MTOK_OUT = 2500

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
                "detail": "Research, call plans, and call analysis run on Claude."}
    return {"live": False, "label": "Offline templates",
            "detail": ("No Anthropic credentials found — running the deterministic "
                       "fallback. Set ANTHROPIC_API_KEY to switch on Claude.")}


def _ask(system, prompt, schema=None, max_tokens=8000):
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
    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError("request declined by safety classifiers")

    _last_call_cents.append(estimate_cents(getattr(response, "usage", None)))

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise RuntimeError("empty response")
    return json.loads(text) if schema else text


def _obj(props, required):
    """JSON-schema object helper. additionalProperties off — see module docstring."""
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def _str(desc=""):
    return {"type": "string", "description": desc}


def _arr(items, desc=""):
    return {"type": "array", "items": items, "description": desc}


def _company_brief(company, contact=None, triggers=None, org=None):
    """The shared context block. One place, so every prompt sees the same facts."""
    lines = [
        f"COMPANY: {company['name']}",
        f"Industry: {company['industry'] or 'unknown'}"
        + (f" (NAICS {company['naics']})" if company['naics'] else ""),
        f"Location: {company['city']}, {company['state']}",
        f"Size: {company['employees'] or 'unknown'} employees, "
        f"{('$%.0fM revenue (est)' % (company['revenue_est'] / 1e6)) if company['revenue_est'] else 'revenue unknown'}",
        f"Shipper type: {company['shipper_type'] or 'unknown'}",
        f"Freight modes: {company['modes'] or 'unknown'}",
        f"Volume: {company['loads_per_month'] or 'unknown'} loads/month",
        f"Docks: {company['dock_count'] or 'unknown'}",
    ]
    if company["lanes"]:
        lines.append("Known lanes:\n" + "\n".join(
            f"  - {l.strip()}" for l in company["lanes"].splitlines() if l.strip()))
    if company["incumbents"]:
        lines.append(f"Incumbent providers: {company['incumbents']}")
    lines.append(f"Data confidence on this company record: {company['confidence']}/100 "
                 f"(source: {company['source']})")

    if contact is not None:
        lines += ["", f"CONTACT: {contact['name']} — {contact['title']}",
                  f"Role in the deal: {contact['role_type'].replace('_', ' ')}",
                  f"Seniority: {contact['seniority'] or 'unknown'}"]
        if contact["role_started"]:
            lines.append(f"In this role since: {contact['role_started']}")

    if triggers:
        lines += ["", "LIVE SIGNALS (why this company, why now):"]
        for t in triggers:
            lines.append(f"  - [{t['observed_at']}] {t['headline']}"
                         f" ({t['source_name'] or 'source unrecorded'}, "
                         f"confidence {t['strength']}/100)")
            if t["detail"]:
                lines.append(f"      {t['detail']}")

    if org is not None:
        lines += ["", f"WE ARE: {org['name']}",
                  f"What we sell: {org['pitch'] or 'freight brokerage services'}",
                  f"Modes we can cover: {org['modes']}",
                  f"Home markets: {org['home_markets'] or 'not specified'}"]
    return "\n".join(lines)


# ===========================================================================
# 1. Plain-English prospect search
# ===========================================================================

_SEARCH_SYSTEM = """You turn a salesperson's plain-English prospecting brief into
structured database filters for a freight brokerage's prospect database.

Extract only what the brief actually says or clearly implies. Do not invent a
state because an industry is concentrated somewhere, and do not narrow a revenue
range the user left open — an over-specified filter returns an empty list and the
user cannot tell whether that means "no matches" or "you guessed wrong".

`keywords` should hold terms to match against industry, shipper type and company
name. `reasoning` is one sentence a salesperson reads to check you understood
them, written in plain language, not as a restatement of the JSON."""

_SEARCH_SCHEMA = _obj({
    "states": _arr(_str(), "Two-letter US state codes, empty if unspecified"),
    "industries": _arr(_str(), "Industry terms mentioned or clearly implied"),
    "keywords": _arr(_str(), "Free-text terms to match on industry/type/name"),
    "modes": _arr({"type": "string",
                   "enum": ["ltl", "ftl", "flatbed", "reefer", "expedited", "intermodal"]},
                  "Freight modes implied by the brief"),
    "shipper_types": _arr(_str(), "manufacturer / distributor / retailer / 3pl"),
    "revenue_min": {"type": ["number", "null"], "description": "USD, null if unspecified"},
    "revenue_max": {"type": ["number", "null"]},
    "employees_min": {"type": ["integer", "null"]},
    "employees_max": {"type": ["integer", "null"]},
    "loads_min": {"type": ["integer", "null"], "description": "Minimum loads/month"},
    "trigger_kinds": _arr(_str(), "Signal kinds implied, e.g. new_facility"),
    "reasoning": _str("One sentence: how you read the brief"),
}, ["states", "industries", "keywords", "modes", "shipper_types", "revenue_min",
    "revenue_max", "employees_min", "employees_max", "loads_min", "trigger_kinds",
    "reasoning"])


def parse_search(query, org=None):
    """Plain English -> filters. Returns (filters, engine)."""
    prompt = (f"Prospecting brief:\n\n{query}\n\n"
              f"Available signal kinds: {', '.join(_TRIGGER_KEYS)}")
    if org is not None:
        prompt += (f"\n\nThe broker running this search covers these modes: "
                   f"{org['modes']}, home markets: {org['home_markets'] or 'none set'}.")
    try:
        return _ask(_SEARCH_SYSTEM, prompt, _SEARCH_SCHEMA, max_tokens=2000), "claude"
    except Exception:
        return _offline_search(query), "offline"


# Kept in sync with signal_engine.TRIGGER_TYPES by import rather than by hand.
def _trigger_keys():
    try:
        import signal_engine
        return list(signal_engine.TRIGGER_TYPES)
    except Exception:
        return []


_TRIGGER_KEYS = _trigger_keys()

_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

_MODE_WORDS = {
    "ltl": ["ltl", "less than truckload", "less-than-truckload", "partial"],
    "ftl": ["ftl", "truckload", "full truckload", "dry van", "van"],
    "flatbed": ["flatbed", "flat bed", "steel", "lumber", "oversize", "step deck"],
    "reefer": ["reefer", "refrigerated", "cold chain", "frozen", "produce", "temperature"],
    "expedited": ["expedited", "hot shot", "hotshot", "time-critical", "time critical",
                  "sprinter", "team"],
    "intermodal": ["intermodal", "rail", "drayage", "container", "port"],
}

_SHIPPER_WORDS = {
    "manufacturer": ["manufactur", "factory", "plant", "fabricat", "producer", "mill"],
    "distributor": ["distribut", "wholesale", "supply", "supplier"],
    "retailer": ["retail", "store", "ecommerce", "e-commerce"],
    "3pl": ["3pl", "third party logistics", "third-party logistics", "warehouse"],
}

_MONEY = re.compile(r"\$?\s*(\d+(?:\.\d+)?)\s*([mkb])?", re.I)


def _offline_search(query):
    """
    Keyword extraction with a unit-aware money parser.

    Genuinely crude and labelled as such in the UI. It handles the shapes that
    actually get typed — "$25M-$500M", "Missouri", "flatbed", "50+ loads" — and
    misses everything subtler, which is the honest ceiling for regular
    expressions over natural language.
    """
    q = (query or "").lower()
    states = sorted({code for name, code in _STATES.items() if name in q}
                    | {m.group(1) for m in re.finditer(r"\b([A-Z]{2})\b", query or "")
                       if m.group(1) in set(_STATES.values())})

    modes = [m for m, words in _MODE_WORDS.items() if any(w in q for w in words)]
    shippers = [s for s, words in _SHIPPER_WORDS.items() if any(w in q for w in words)]

    amounts = []
    for m in re.finditer(r"\$\s*(\d+(?:\.\d+)?)\s*([mkb])?", q):
        value = float(m.group(1))
        unit = (m.group(2) or "").lower()
        amounts.append(value * {"k": 1e3, "m": 1e6, "b": 1e9}.get(unit, 1))
    rev_min = min(amounts) if amounts else None
    rev_max = max(amounts) if len(amounts) > 1 else None

    loads = None
    lm = re.search(r"(\d+)\s*\+?\s*(?:loads|shipments|trucks)", q)
    if lm:
        loads = int(lm.group(1))

    trigger_kinds = []
    for key, words in (("new_facility", ["new facility", "new plant", "expanding",
                                         "expansion", "opening"]),
                       ("logistics_hire", ["hiring", "job post"]),
                       ("leadership_change", ["new vp", "new director", "leadership"]),
                       ("contract_window", ["rfp", "bid", "contract"]),
                       ("carrier_trouble", ["complain", "unhappy", "problem"])):
        if any(w in q for w in words):
            trigger_kinds.append(key)

    stop = {"with", "that", "ship", "shipping", "freight", "companies", "company",
            "in", "and", "the", "who", "revenue", "between", "more", "than",
            "often", "frequently", "a", "of", "for", "find", "me"}
    keywords = [w for w in re.findall(r"[a-z]{4,}", q)
                if w not in stop and w not in _STATES][:6]

    return {
        "states": states, "industries": [], "keywords": keywords, "modes": modes,
        "shipper_types": shippers, "revenue_min": rev_min, "revenue_max": rev_max,
        "employees_min": None, "employees_max": None, "loads_min": loads,
        "trigger_kinds": trigger_kinds,
        "reasoning": ("Matched on keywords only — no model is connected, so nothing "
                      "here was actually understood."),
    }


# ===========================================================================
# 2. Company research
# ===========================================================================

_RESEARCH_SYSTEM = """You are a freight brokerage's research analyst. Given a
company record and whatever signals have been observed about it, write the brief
a broker would want in front of them before dialing.

Rules:

* Reason from the record you are given. Where you are inferring rather than
  reading, say so in the text ("a plant that size typically runs...").
* Never state a fact about the company you were not given and cannot derive.
  A fabricated detail read aloud on a call ends the call.
* `opportunity_thesis` is the argument for why this company would move freight to
  a new broker in the next ninety days. If the honest answer is that they
  probably would not, write that — a brief that argues for every account is worth
  nothing on any account.
* `risks` is what would make this a waste of the rep's time. Be specific.
* `estimated_value` should be a dollar range per year, with the arithmetic shown
  in `value_basis` (loads/month x typical margin per load x 12).
* Keep every field tight. This is read in the ninety seconds before a dial."""

_RESEARCH_SCHEMA = _obj({
    "summary": _str("2-3 sentences: what this company does and how freight moves for them"),
    "why_now": _str("One paragraph on the timing, grounded in the signals given"),
    "likely_needs": _arr(_str(), "3-5 specific freight needs"),
    "opportunity_thesis": _str("The argument for a change of provider, or against"),
    "risks": _arr(_str(), "2-4 reasons this may waste the rep's time"),
    "estimated_value": _str("Annual revenue range, e.g. '$180K-$320K'"),
    "value_basis": _str("The arithmetic behind that range"),
    "score_rationale": _str("One sentence on how strong this really is"),
}, ["summary", "why_now", "likely_needs", "opportunity_thesis", "risks",
    "estimated_value", "value_basis", "score_rationale"])


def research_company(org, company, contact, triggers):
    """Company + signals -> a research brief. Returns (dict, engine)."""
    prompt = ("Write the pre-call research brief.\n\n"
              + _company_brief(company, contact, triggers, org))
    try:
        return _ask(_RESEARCH_SYSTEM, prompt, _RESEARCH_SCHEMA, max_tokens=3000), "claude"
    except Exception:
        return _offline_research(org, company, contact, triggers), "offline"


# Typical broker gross margin per load, by mode. Used by the offline value
# estimate; the Claude path reasons about it instead. Conservative mid-market
# numbers — a real deployment should replace these with the org's own.
_MARGIN = {"ltl": 55, "ftl": 260, "flatbed": 330, "reefer": 340,
           "expedited": 520, "intermodal": 210}


def _offline_research(org, company, contact, triggers):
    import signal_engine
    modes = [m.strip() for m in (company["modes"] or "ftl").split(",") if m.strip()]
    loads = company["loads_per_month"] or 20
    margin = sum(_MARGIN.get(m, 240) for m in modes) / max(len(modes), 1)
    low, high = int(loads * margin * 12 * 0.15), int(loads * margin * 12 * 0.4)

    needs, why = [], []
    for t in triggers or []:
        spec = signal_engine.TRIGGER_TYPES.get(t["kind"])
        if spec:
            needs.append(spec["need"])
            why.append(f"{t['headline']} ({t['observed_at']})")

    where = f"{company['city']}, {company['state']}".strip(", ")
    return {
        "summary": (f"{company['name']} is a {company['shipper_type'] or 'shipper'} in "
                    f"{company['industry'] or 'an unrecorded industry'} based in {where}, "
                    f"moving roughly {loads} loads a month across "
                    f"{', '.join(m.upper() for m in modes)}."
                    + (f" Known incumbents: {company['incumbents']}."
                       if company["incumbents"] else "")),
        "why_now": (" ".join(why) if why else
                    "No live signal on this account. This is a cold call and should be "
                    "prioritized behind anything with a reason attached."),
        "likely_needs": needs[:5] or [
            f"Overflow {modes[0].upper()} capacity beyond contracted carriers.",
            "Coverage on lanes the incumbent routing guide handles badly.",
        ],
        "opportunity_thesis": (
            f"At {loads} loads a month, a 10-20% share of their freight is worth "
            f"${low:,}-${high:,} a year in gross margin. "
            + ("A live signal says their logistics situation is changing, which is "
               "when a routing guide is actually open."
               if triggers else
               "Nothing suggests their situation is changing, so displacing an "
               "incumbent would mean out-executing on price or service alone.")),
        "risks": [
            "Incumbent relationships in mid-market freight are personal and often long.",
            "Volume and revenue figures on this record are estimates, not filings.",
            ("No verified contact data on the record — the number may not reach "
             "anybody." if not contact else
             "The contact's authority over carrier selection is inferred from their "
             "title, not confirmed."),
        ],
        "estimated_value": f"${low:,}-${high:,}",
        "value_basis": (f"{loads} loads/month x ${margin:,.0f} avg margin x 12 months "
                        f"x 15-40% share capture."),
        "score_rationale": ("Generated from the record by template, with no model "
                            "connected. Treat it as a formatted version of the data "
                            "you already have, not as research."),
    }


# ===========================================================================
# 3. The call plan
# ===========================================================================

_PLAN_SYSTEM = """You write the one-page call plan a freight broker holds while
the phone rings. It is read live, under pressure, by someone who has forty more
calls to make today.

Rules:

* The opening is TWO SENTENCES and names the specific reason for the call. No
  "how are you today", no permission-asking preamble, no company history.
* Discovery questions must be answerable by an operations person from memory —
  loads per week, lanes, who they use, what went wrong last month. Nothing that
  requires them to open a spreadsheet.
* Objections must be the ones actually said out loud in freight sales: "we're
  happy with who we have", "send me an email", "we're under contract", "you
  people call me every day". Each response must concede the point before it
  redirects. A response that argues with the objection loses the call.
* `voicemail` is UNDER 20 SECONDS spoken, gives a reason to call back, and states
  a specific time the rep will try again.
* Never promise capacity, a rate, or a transit time. The rep does not have
  pricing authority on a cold call and a promise made here becomes a service
  failure later.
* Write in the rep's voice, plain and direct. No sales-trainer vocabulary."""

_PLAN_SCHEMA = _obj({
    "opening": _str("Two sentences, spoken aloud, naming the specific reason"),
    "discovery": _arr(_obj({
        "question": _str(),
        "listening_for": _str("What a good answer tells the rep"),
    }, ["question", "listening_for"]), "4-6 questions in order"),
    "value_points": _arr(_obj({
        "point": _str("One sentence, in the rep's voice"),
        "when_to_use": _str("The cue in the conversation that makes this land"),
    }, ["point", "when_to_use"]), "3-4 points"),
    "objections": _arr(_obj({
        "objection": _str("What they actually say"),
        "response": _str("Concede, then redirect. Two sentences."),
    }, ["objection", "response"]), "4-5 objections"),
    "voicemail": _str("Under 20 seconds spoken"),
    "next_step_ask": _str("The specific commitment to ask for"),
    "do_not_say": _arr(_str(), "2-3 things that would kill this particular call"),
}, ["opening", "discovery", "value_points", "objections", "voicemail",
    "next_step_ask", "do_not_say"])


def build_call_plan(org, company, contact, triggers, research=None, signals=None):
    """Everything the rep needs on screen while it rings. Returns (dict, engine)."""
    prompt = ["Write the call plan.", "", _company_brief(company, contact, triggers, org)]
    if research:
        prompt += ["", "RESEARCH BRIEF ALREADY PRODUCED:",
                   f"Why now: {research.get('why_now', '')}",
                   f"Likely needs: {'; '.join(research.get('likely_needs', []))}",
                   f"Thesis: {research.get('opportunity_thesis', '')}"]
    if signals:
        top = [s for s in signals if s["points"] > 0][:6]
        prompt += ["", "WHAT PUT THEM ON TODAY'S LIST:"] + [
            f"  - {s['label']}: {s['detail']}" for s in top]
    try:
        return _ask(_PLAN_SYSTEM, "\n".join(prompt), _PLAN_SCHEMA, max_tokens=4000), "claude"
    except Exception:
        return _offline_plan(org, company, contact, triggers), "offline"


def _lower_first(text):
    """Lowercase only the first character, so "$42M" and "TQL" survive."""
    text = (text or "").strip()
    return text[:1].lower() + text[1:] if text else text


def _plural(word):
    """Good enough for the handful of shipper-type nouns this field holds."""
    word = (word or "").strip()
    if not word or word.endswith("s"):
        return word or "shippers"
    return word + ("es" if word.endswith(("ch", "sh", "x", "z")) else "s")


def _offline_plan(org, company, contact, triggers):
    first = (contact["name"].split()[0] if contact else "there")
    rep_org = org["name"]
    modes = [m.strip().upper() for m in (company["modes"] or "FTL").split(",") if m.strip()]
    mode = modes[0] if modes else "FTL"
    # Headlines are written as headlines — "Announced a $42M facility". Dropped
    # mid-sentence after "I saw", the capital reads as a copy-paste, which is
    # exactly what a prospect hears when a rep says it out loud.
    reason = (_lower_first(triggers[0]["headline"]) if triggers
              else f"your operation in {company['city'] or 'the area'}")
    audience = _plural(company["shipper_type"] or "shipper")
    lane = next((l.strip() for l in (company["lanes"] or "").splitlines() if l.strip()),
                "your main outbound lane")

    return {
        "opening": (f"{first}, this is [your name] with {rep_org} — I saw {reason}. "
                    f"I work with {audience} in "
                    f"{company['state'] or 'the region'} on {mode} capacity and I had "
                    f"one question about your outbound."),
        "discovery": [
            {"question": "Who handles carrier selection for outbound today — is that you?",
             "listening_for": "Whether you are on the phone with the decision maker or a route to one."},
            {"question": "Roughly how many loads a week are you moving right now?",
             "listening_for": f"Our record says about {company['loads_per_month'] or '?'} a month. A gap either way means the record is wrong."},
            {"question": f"Which lanes give you the most trouble — is {lane} still the heaviest?",
             "listening_for": "The lane where the incumbent is failing. That is the one to quote."},
            {"question": "Who are you using today, and how is that going?",
             "listening_for": "A named incumbent and an unprompted complaint. The complaint is the opening."},
            {"question": "When something has to be there tomorrow, what happens?",
             "listening_for": "Expedited pain. It is the highest-margin freight and the easiest to win."},
        ],
        "value_points": [
            {"point": f"You would have one person on our side who knows your lanes, not a queue.",
             "when_to_use": "After they complain about chasing someone for a status update."},
            {"point": f"We hold {mode} capacity in {company['state'] or 'the region'} rather than brokering it out at the last minute.",
             "when_to_use": "After they mention a missed pickup or a fall-off."},
            {"point": "Start with the lane that hurts. If we do not perform on it, you have lost nothing.",
             "when_to_use": "As the close — it is a small ask, which is why it gets said yes to."},
        ],
        "objections": [
            {"objection": "We're happy with who we have.",
             "response": "Good — you should be, and I'm not asking you to replace them. I'm asking to be the second call on the load they turn down."},
            {"objection": "Send me an email.",
             "response": "I will, today. So I send something useful instead of a brochure — what lane is giving you the most trouble right now?"},
            {"objection": "We're under contract.",
             "response": "Understood, and contracted rates rarely cover overflow or anything expedited. When does it come up for bid?"},
            {"objection": "You people call me every day.",
             "response": "I know, and I'm not going to pretend this is different because I said so. One question and I'll go: what does your incumbent do badly?"},
            {"objection": "Just send me a rate.",
             "response": "I can, but a rate with no lane attached is a made-up number. Give me one origin and destination and you'll have something real today."},
        ],
        "voicemail": (f"{first}, [your name] at {rep_org}, {org['pitch'][:40] if org['pitch'] else 'freight'}. "
                      f"Calling about {reason} — I have capacity on {lane} and wanted to "
                      f"ask one question about your outbound. I'll try you Thursday morning. "
                      f"[number], thanks."),
        "next_step_ask": ("One lane, one quote, this week. Not a meeting — a lane. "
                          "It is a smaller ask and it produces a real conversation."),
        "do_not_say": [
            "Any specific rate or transit time. You do not have pricing authority on a cold call.",
            "'Best rates in the industry.' Every caller before you said it this morning.",
            "Anything implying you already know their volumes — the numbers on this record are estimates.",
        ],
    }


# ===========================================================================
# 4. Call analysis
# ===========================================================================

_ANALYZE_SYSTEM = """You are analyzing a recorded B2B sales call for a freight
brokerage's CRM. The output is written directly onto the customer record and
drives what happens next, so precision matters more than polish.

Rules:

* Extract only what was actually said. If shipment volume was not mentioned,
  `volumes` is empty. An invented number here becomes a forecast.
* `commitments` are things ONE SIDE SAID THEY WOULD DO, with who owes what. A
  request the rep did not agree to is not a commitment.
* `disposition` must reflect what happened, not what the rep hoped. A call where
  the prospect said "send me something" is `connected`, not `meeting_set`.
  `meeting_set` requires an agreed time or an agreed specific next call.
* `next_action` is one concrete action, and `next_action_days` is when — 0 means
  today. If the prospect named a timeframe, use theirs.
* `coaching` is one specific observation about how the rep handled the call, of
  the kind a manager would say in a one-to-one. Not praise, not a rubric score.
* `data_corrections` captures anything said that contradicts our record — a
  different volume, a different incumbent, the wrong person. This is how the
  database gets better."""

_ANALYZE_SCHEMA = _obj({
    "summary": _str("3-4 sentences. What was said, what it means, what happens next"),
    "disposition": {"type": "string",
                    "enum": ["connected", "meeting_set", "callback", "not_interested",
                             "gatekeeper", "voicemail", "wrong_number", "no_answer"]},
    "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
    "pain_points": _arr(_str(), "Problems the prospect stated, in their words"),
    "volumes": _arr(_str(), "Any figures stated — loads, lanes, frequency"),
    "incumbents": _arr(_str(), "Providers named by the prospect"),
    "commitments": _arr(_obj({
        "who": {"type": "string", "enum": ["rep", "prospect"]},
        "what": _str(),
        "when": _str("As stated on the call, or 'unspecified'"),
    }, ["who", "what", "when"])),
    "objections_raised": _arr(_str()),
    "next_action": _str("One concrete action"),
    "next_action_days": {"type": "integer", "description": "Days from today; 0 = today"},
    "opportunity": {"type": "boolean", "description": "Should this become a pipeline deal"},
    "estimated_loads": {"type": ["integer", "null"], "description": "Loads/month if stated"},
    "coaching": _str("One specific observation for the rep's manager"),
    "data_corrections": _arr(_str(), "Where the call contradicted our record"),
}, ["summary", "disposition", "sentiment", "pain_points", "volumes", "incumbents",
    "commitments", "objections_raised", "next_action", "next_action_days",
    "opportunity", "estimated_loads", "coaching", "data_corrections"])


def analyze_call(org, company, contact, transcript, notes="", disposition=""):
    """Transcript -> structured outcome. Returns (dict, engine)."""
    prompt = ["Analyze this call.", "", _company_brief(company, contact, None, org), ""]
    if disposition:
        prompt.append(f"The rep marked this call: {disposition}")
    if notes:
        prompt += ["", "REP NOTES:", notes]
    prompt += ["", "TRANSCRIPT:", transcript or "(no transcript captured)"]
    try:
        return _ask(_ANALYZE_SYSTEM, "\n".join(prompt), _ANALYZE_SCHEMA,
                    max_tokens=3000), "claude"
    except Exception:
        return _offline_analysis(company, contact, transcript, notes, disposition), "offline"


_PAIN_CUES = [
    (r"rate[s]? (have been|are|were) (brutal|high|terrible)|quoted almost double|expedited rates",
     "Expedited and spot rates from the incumbent are too high."),
    (r"missed pickup|didn't show|no[- ]show|fall(-| )off",
     "Missed pickups the incumbent did not proactively report."),
    (r"tracking|where my (own )?freight|status update",
     "No proactive visibility — the shipper is chasing status themselves."),
    (r"disappear|time[- ]sensitive|when something is",
     "Incumbent performs on easy lanes and not on time-sensitive freight."),
    (r"nobody calls us|find out from the customer",
     "Service failures reach them via their own customer first."),
]

_COMMIT_CUES = [
    (r"send me (a |the |over )?(capability|something|what you|your)",
     "rep", "Send capability sheet / lane detail"),
    (r"call me back|try me|call.*(monday|tuesday|wednesday|thursday|friday)",
     "rep", "Call back at the agreed time"),
    (r"(mc|insurance cert|certificate of insurance)",
     "rep", "Send MC number and certificate of insurance"),
    (r"i'?ll (look|review|take it to)",
     "prospect", "Review what the rep sends"),
]


def _offline_analysis(company, contact, transcript, notes="", disposition=""):
    """
    Regex extraction over the transcript.

    It finds the cues it was told to look for and nothing else, which is exactly
    what it is: pattern matching wearing the shape of comprehension. It exists so
    the workflow completes without a key, and the UI says which engine produced
    it on every screen it appears.
    """
    text = f"{transcript}\n{notes}".lower()
    pains = [label for pattern, label in _PAIN_CUES if re.search(pattern, text)]
    commitments = [{"who": who, "what": what, "when": "unspecified"}
                   for pattern, who, what in _COMMIT_CUES if re.search(pattern, text)]

    loads = None
    m = re.search(r"(?:about|around|roughly)?\s*(\d{1,4})\s*loads?\s*a\s*(?:month|week)", text)
    if m:
        loads = int(m.group(1))
        if "week" in m.group(0):
            loads *= 4

    incumbents = []
    if company["incumbents"]:
        incumbents = [i.strip() for i in company["incumbents"].split(",") if i.strip()]

    disp = disposition or ("meeting_set" if re.search(r"let'?s do a call|ten'?s fine|"
                                                      r"(monday|tuesday|wednesday|thursday) morning",
                                                      text)
                           else "connected" if transcript else "no_answer")
    positive = disp in ("meeting_set", "callback")
    negative = disp in ("not_interested",)

    days = {"meeting_set": 1, "callback": 2, "connected": 3, "gatekeeper": 2,
            "voicemail": 4, "not_interested": 90}.get(disp, 3)
    action = {
        "meeting_set": "Send the capability sheet and confirm the meeting in writing.",
        "callback": "Call back at the time they named.",
        "connected": "Send what you promised, then follow up in three days.",
        "gatekeeper": "Try the direct line after 3pm as suggested.",
        "voicemail": "Second attempt at a different hour, then email.",
        "not_interested": "Park until their contract window and re-approach then.",
    }.get(disp, "Follow up.")

    return {
        "summary": (f"Call with {contact['name'] if contact else 'the contact'} at "
                    f"{company['name']}, marked {disp.replace('_', ' ')}. "
                    + (f"{len(pains)} stated problem(s) matched known patterns. "
                       if pains else "No stated problems matched a known pattern. ")
                    + (f"Approximately {loads} loads/month mentioned. " if loads else "")
                    + "Extracted by keyword matching, not by a model."),
        "disposition": disp,
        "sentiment": "positive" if positive else "negative" if negative else "neutral",
        "pain_points": pains,
        "volumes": [f"~{loads} loads/month (stated on the call)"] if loads else [],
        "incumbents": incumbents,
        "commitments": commitments,
        "objections_raised": ([o for o in ("under contract", "happy with who we have",
                                           "send me an email")
                               if o.replace(" ", "") in text.replace(" ", "")]),
        "next_action": action,
        "next_action_days": days,
        "opportunity": disp in ("meeting_set", "callback"),
        "estimated_loads": loads,
        "coaching": ("No coaching available without a model connected — the offline "
                     "engine cannot assess how the call was handled."),
        "data_corrections": ([f"Call mentioned ~{loads} loads/month; record says "
                              f"{company['loads_per_month']}."]
                             if loads and company["loads_per_month"]
                             and abs(loads - company["loads_per_month"]) > 5 else []),
    }


# ===========================================================================
# 5. The follow-up email
# ===========================================================================

_EMAIL_SYSTEM = """You write the follow-up email a freight broker sends within an
hour of a call. It is read on a phone, on a dock, between two other problems.

Rules:

* Under 120 words. Subject line under 50 characters, specific, no "Following up".
* Open by referencing what THEY said, not what the rep said.
* Deliver exactly what was promised on the call, and say it is attached or
  coming — do not re-pitch.
* One ask at the end, and make it small and specific: a lane, a date, a name.
* No bullet-point capability lists, no "circling back", no "as per our
  conversation", no exclamation marks.
* Never state a rate, a transit time, or a capacity guarantee.
* Sign with the rep's first name only. Do not invent a phone number, a title, or
  a signature block — those are appended by the sending system."""

_EMAIL_SCHEMA = _obj({
    "subject": _str("Under 50 characters"),
    "body": _str("Under 120 words, plain text, line breaks preserved"),
    "send_delay_hours": {"type": "integer", "description": "0 = now"},
    "rationale": _str("One sentence: why this email, in this shape, now"),
}, ["subject", "body", "send_delay_hours", "rationale"])


def draft_email(org, company, contact, analysis=None, kind="follow_up", rep_name="",
                transcript=""):
    """Call outcome -> the email that gets sent. Returns (dict, engine)."""
    prompt = [f"Write the {kind.replace('_', ' ')} email.", "",
              _company_brief(company, contact, None, org), "",
              f"Rep's first name: {rep_name.split()[0] if rep_name else '[rep]'}"]
    if analysis:
        prompt += ["", "WHAT HAPPENED ON THE CALL:",
                   analysis.get("summary", ""),
                   "They said: " + "; ".join(analysis.get("pain_points", []) or ["(nothing recorded)"]),
                   "We committed to: " + "; ".join(
                       c["what"] for c in analysis.get("commitments", [])
                       if c.get("who") == "rep") or "(nothing)"]
    if transcript:
        prompt += ["", "TRANSCRIPT:", transcript[:6000]]
    try:
        return _ask(_EMAIL_SYSTEM, "\n".join(prompt), _EMAIL_SCHEMA, max_tokens=1500), "claude"
    except Exception:
        return _offline_email(org, company, contact, analysis, rep_name), "offline"


def _offline_email(org, company, contact, analysis, rep_name=""):
    first = contact["name"].split()[0] if contact else "there"
    rep = rep_name.split()[0] if rep_name else "[your name]"
    analysis = analysis or {}
    pains = analysis.get("pain_points") or []
    promised = [c["what"] for c in analysis.get("commitments", []) if c.get("who") == "rep"]
    lane = next((l.strip() for l in (company["lanes"] or "").splitlines() if l.strip()),
                "your heaviest outbound lane")

    opener = (f"You mentioned {pains[0][0].lower() + pains[0][1:].rstrip('.')}"
              if pains else "Thanks for the few minutes earlier")
    body = (f"{first} —\n\n{opener}. "
            + (f"{promised[0]} is attached.\n\n" if promised
               else "Here's what we cover on your side of the map.\n\n")
            + f"One question so I can put something real in front of you: is {lane} "
              f"still the one that causes the most trouble? Give me that lane and "
              f"I'll come back with something specific this week.\n\n{rep}")
    return {
        "subject": f"{company['name']} — {lane[:30]}",
        "body": body,
        "send_delay_hours": 0,
        "rationale": ("Assembled from a template using the call's extracted fields. "
                      "No model is connected — read it before sending."),
    }
