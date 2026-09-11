"""
Royal Grind — prospect list import.

Nobody can switch to you without a migration path, so this is the feature that
makes the rest of the product reachable. Drop in a CSV from ZoomInfo, Apollo,
Salesforce or HubSpot — or a hand-rolled list — and it lands as companies,
contacts, contact points and assigned targets.

Four things make this more than a CSV loader.

**1. It recognizes the export.** Four platforms name the same column nine
different ways, and a user should not hand-map twenty fields on their first run.
`sniff_source()` reads the header row and picks a field map; anything
unrecognized falls back to the union of all of them, which handles the common
case of somebody reordering or renaming a couple of columns.

**2. It does not launder somebody else's confidence.** This is the part that
matters, and it is the whole thesis of the product applied to its own front
door. A vendor marking an email "Verified" is *their* claim, on *their* date, by
*their* method. Importing that as our `verified` would mean a rep sees the same
green badge for a number we dialed last Tuesday and one a vendor asserted at
some point for reasons we cannot inspect. So by default a vendor's verification
lands as **probable**, with the vendor's own words preserved in `method`. There
is a checkbox to accept it at face value, it is off by default, and the sentence
next to it says exactly what turning it on means.

**3. It previews, then applies.** `plan()` is read-only and returns exactly what
would be created, updated and skipped, with a reason on every skipped row.
Nothing touches the database until `apply()`. An import that surprises you is
one you cannot trust with a book of business.

**4. It checks the suppression list before it creates a single dialable row.**
An import is the most likely way a number somebody asked you never to call again
re-enters the database. Matching rows still import — the company is still real —
but the contact point is written `invalid` with the reason, so it can never
reach a call list and the record shows why.

Re-importing is safe and expected. Companies are keyed on the source's own
record id, else domain, else name+state; contacts on email, else name within the
company. The same file applied twice updates in place rather than duplicating.
"""

import csv
import io
import json
import re
from datetime import date, datetime

import compliance
import enrichment

# ---------------------------------------------------------------- detection --

# Header fingerprints, lowercased and stripped. A file matches a source when it
# carries every column in the set — deliberately small sets, because exports vary
# by which columns the user happened to tick.
_SIGNATURES = {
    "zoominfo": {"company name", "job title"},
    "apollo": {"company", "title", "email status"},
    "salesforce": {"account name", "title"},
    "hubspot": {"associated company", "job title"},
}

# Our field <- their column, first match wins.
FIELD_MAPS = {
    "zoominfo": {
        "external_id": ["zoominfo contact id", "contact id", "id"],
        "company": ["company name", "company"],
        "domain": ["website", "company domain", "domain"],
        "first_name": ["first name"],
        "last_name": ["last name"],
        "full_name": ["contact name", "full name", "name"],
        "title": ["job title", "title"],
        "email": ["email address", "email"],
        "email_status": ["email validation status", "email status"],
        "direct_dial": ["direct phone number", "direct phone", "direct dial"],
        "mobile": ["mobile phone", "mobile"],
        "switchboard": ["company phone", "hq phone", "main phone"],
        "city": ["company city", "city"],
        "state": ["company state", "state"],
        "industry": ["primary industry", "industry"],
        "naics": ["naics code", "naics"],
        "employees": ["employees", "employee count", "number of employees"],
        "revenue": ["revenue (in 000s usd)", "revenue", "annual revenue"],
        "linkedin": ["linkedin contact profile url", "linkedin url", "linkedin"],
        "started": ["job start date", "start date"],
    },
    "apollo": {
        "external_id": ["apollo contact id", "contact id", "id"],
        "company": ["company", "company name", "account"],
        "domain": ["website", "company domain", "primary domain"],
        "first_name": ["first name"],
        "last_name": ["last name"],
        "full_name": ["name", "full name"],
        "title": ["title", "job title"],
        "email": ["email", "email address"],
        "email_status": ["email status", "email confidence"],
        "direct_dial": ["direct phone", "work direct phone", "direct dial"],
        "mobile": ["mobile phone", "home phone", "mobile"],
        "switchboard": ["corporate phone", "company phone"],
        "city": ["city", "company city"],
        "state": ["state", "company state"],
        "industry": ["industry", "company industry"],
        "naics": ["naics", "naics code"],
        "employees": ["# employees", "employees", "num employees"],
        "revenue": ["annual revenue", "revenue"],
        "linkedin": ["person linkedin url", "linkedin url", "linkedin"],
        "started": ["start date", "job start date"],
    },
    "salesforce": {
        "external_id": ["contact id", "id", "18 digit id", "record id"],
        "company": ["account name", "account", "company"],
        "domain": ["website", "account website", "domain"],
        "first_name": ["first name"],
        "last_name": ["last name"],
        "full_name": ["full name", "name", "contact name"],
        "title": ["title", "job title"],
        "email": ["email", "email address"],
        "email_status": ["email status"],
        "direct_dial": ["phone", "business phone", "direct"],
        "mobile": ["mobile", "mobile phone"],
        "switchboard": ["account phone", "company phone"],
        "city": ["mailing city", "city", "billing city"],
        "state": ["mailing state/province", "mailing state", "state", "billing state"],
        "industry": ["industry"],
        "naics": ["naics", "naics code"],
        "employees": ["employees", "number of employees"],
        "revenue": ["annual revenue", "revenue"],
        "linkedin": ["linkedin", "linkedin url"],
        "started": ["start date"],
    },
    "hubspot": {
        "external_id": ["record id", "contact id", "id"],
        "company": ["associated company", "company name", "company"],
        "domain": ["company domain name", "website url", "domain"],
        "first_name": ["first name"],
        "last_name": ["last name"],
        "full_name": ["name", "full name"],
        "title": ["job title", "title"],
        "email": ["email", "email address"],
        "email_status": ["email status", "marketing contact status"],
        "direct_dial": ["phone number", "phone", "direct phone"],
        "mobile": ["mobile phone number", "mobile phone", "mobile"],
        "switchboard": ["company phone number", "company phone"],
        "city": ["city"],
        "state": ["state/region", "state"],
        "industry": ["industry"],
        "naics": ["naics", "naics code"],
        "employees": ["number of employees", "employees"],
        "revenue": ["annual revenue", "revenue"],
        "linkedin": ["linkedin url", "linkedin"],
        "started": ["start date"],
    },
}

# Freight columns nobody's CRM has, which a broker's own list very often does.
# They are appended to every map rather than being a fifth source, because a
# ZoomInfo export with two hand-added columns is still a ZoomInfo export.
# Columns that really do identify the COMPANY. Kept separate from `external_id`,
# which in every one of these exports identifies the contact: keying a company on
# a contact id creates one company per employee, and the second row at the same
# firm then imports as a separate account with its own call list entry.
_COMPANY_ID_FIELDS = {
    "company_external_id": ["zoominfo company id", "company id", "account id",
                            "associated company id", "company record id",
                            "apollo account id"],
}

_FREIGHT_FIELDS = {
    "modes": ["modes", "freight modes", "equipment", "equipment types"],
    "loads_per_month": ["loads per month", "loads/mo", "monthly loads", "volume"],
    "lanes": ["lanes", "primary lanes", "key lanes"],
    "incumbents": ["incumbents", "current carrier", "current broker", "carriers used"],
    "shipper_type": ["shipper type", "company type", "type"],
    "dock_count": ["docks", "dock count", "dock doors"],
    "owner_email": ["owner email", "assigned to email", "rep email"],
    "owner_name": ["owner", "assigned to", "rep", "contact owner"],
}

for _m in FIELD_MAPS.values():
    _m.update(_FREIGHT_FIELDS)
    _m.update(_COMPANY_ID_FIELDS)

# A hand-rolled or unrecognized export falls back to the union of all four.
FIELD_MAPS["generic"] = {}
for _key in set(k for m in FIELD_MAPS.values() for k in m):
    seen, merged = set(), []
    for _src in ("zoominfo", "apollo", "salesforce", "hubspot"):
        for alias in FIELD_MAPS[_src].get(_key, []):
            if alias not in seen:
                seen.add(alias)
                merged.append(alias)
    FIELD_MAPS["generic"][_key] = merged

SOURCE_LABELS = {
    "zoominfo": "ZoomInfo", "apollo": "Apollo", "salesforce": "Salesforce",
    "hubspot": "HubSpot", "generic": "Generic CSV",
}

# What each source's own email-status vocabulary means in ours. The value on the
# right is what we record when the user does NOT accept vendor verification at
# face value — see the module docstring. `_VENDOR_TRUSTED` is what we record when
# they do.
_EMAIL_STATUS = {
    "verified": "probable", "valid": "probable", "deliverable": "probable",
    "safe": "probable", "confirmed": "probable",
    "catch all": "catch_all", "catch-all": "catch_all", "accept all": "catch_all",
    "unknown": "probable", "unverified": "probable", "risky": "probable",
    "guessed": "guessed", "pattern": "guessed", "inferred": "guessed",
    "invalid": "invalid", "bounced": "invalid", "undeliverable": "invalid",
    "unavailable": "invalid",
}
_VENDOR_TRUSTED = {"verified", "valid", "deliverable", "safe", "confirmed"}


def _norm(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def sniff_source(headers):
    """Which platform produced this file. Falls back to 'generic'."""
    lower = {_norm(h) for h in headers}
    best, best_score = "generic", 0
    for source, signature in _SIGNATURES.items():
        if signature <= lower:
            # Tie-break on how many of the source's own aliases are present, so
            # a file matching two signatures lands on the closer one.
            score = sum(1 for aliases in FIELD_MAPS[source].values()
                        if any(a in lower for a in aliases))
            if score > best_score:
                best, best_score = source, score
    return best


# ------------------------------------------------------------------ parsing --

# Each side may carry its own magnitude suffix — "$10M-$50M" and "201-500" are
# both shapes these exports contain. The separator is an alternation rather than
# a character class: `[-–to]+` would also match "oot".
_RANGE = re.compile(
    r"^\s*([\d.,]+\s*(?:k|m|bn|b)?)\s*(?:-|–|—|to)\s*([\d.,]+\s*(?:k|m|bn|b)?)\s*$",
    re.I)
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}


def parse_number(raw, thousands=False):
    """
    Parse the shapes these exports actually contain.

    Handles "$74M", "74,000,000", "201-500" (midpoint), "1,000+", and
    ZoomInfo's "Revenue (in 000s USD)" via `thousands`. Returns None rather than
    zero when it cannot tell — zero is a claim, and an unreadable cell is not
    one we are entitled to make.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("$", "").replace("usd", "").strip()
    if not text or text in ("-", "n/a", "na", "unknown", "none"):
        return None

    match = _RANGE.match(text)
    if match:
        low = parse_number(match.group(1), thousands)
        high = parse_number(match.group(2), thousands)
        if low is not None and high is not None:
            return (low + high) / 2
        return low if low is not None else high

    plus = text.endswith("+")
    text = text.rstrip("+").strip()

    multiplier = 1.0
    for suffix, factor in sorted(_SUFFIX.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)].strip()
            break

    text = text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None
    value *= multiplier
    if thousands and multiplier == 1.0:
        value *= 1000
    return value


def parse_date(raw):
    """ISO out, or None. Accepts the formats these platforms export."""
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.split("T")[0].split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%b %d, %Y",
                "%d %b %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def clean_phone(raw):
    """
    Return (e164ish, problem). Ten digits is the bar; anything shorter is not a
    number somebody can dial and is reported rather than stored.
    """
    digits = compliance.normalize_phone(raw)
    if not digits:
        return None, None
    if len(digits) < 10:
        return None, f"phone {str(raw).strip()!r} has {len(digits)} digits"
    if len(digits) > 11:
        # An extension glued onto the number. Keep the first ten, note it.
        return f"+1{digits[:10]}", f"phone {str(raw).strip()!r} truncated to 10 digits"
    return f"+1{digits[-10:]}", None


_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def clean_email(raw):
    text = compliance.normalize_email(raw)
    if not text:
        return None, None
    if not _EMAIL.match(text):
        return None, f"email {text!r} is not a valid address"
    return text, None


def clean_domain(raw):
    text = _norm(raw).replace("https://", "").replace("http://", "")
    text = text.split("/")[0].removeprefix("www.")
    return text if "." in text else ""


# Matched on word boundaries, not as substrings. "LTL and expedited" contains
# the letters "tl", and a naive `"tl" in text` tagged every LTL shipper as
# truckload — a mode-fit error that reaches the scoring engine and the call
# script. "ltl" is therefore checked before "ftl" and both are whole words.
_MODE_WORDS = {
    "ltl": [r"ltl", r"less[- ]than[- ]truckload", r"partial"],
    "ftl": [r"ftl", r"\btl\b", r"truckload", r"dry ?van", r"\bvan\b"],
    "flatbed": [r"flat ?bed", r"step ?deck", r"oversize", r"\brgn\b", r"conestoga"],
    "reefer": [r"reefer", r"refrigerated", r"temp(erature)? ?control", r"frozen",
               r"cold ?chain"],
    "expedited": [r"expedited", r"hot ?shot", r"sprinter", r"\bteam\b",
                  r"time[- ]critical"],
    "intermodal": [r"intermodal", r"\brail\b", r"drayage", r"container"],
}
_MODE_RE = {mode: re.compile("|".join(rf"\b(?:{w})" for w in words), re.I)
            for mode, words in _MODE_WORDS.items()}


def parse_modes(raw):
    """
    Free text -> our controlled mode vocabulary. Unknown words are dropped.

    "less than truckload" must not also register as truckload, so the LTL phrase
    is removed once matched before the FTL patterns run.
    """
    text = _norm(raw)
    if not text:
        return ""
    found = []
    if _MODE_RE["ltl"].search(text):
        found.append("ltl")
        text = _MODE_RE["ltl"].sub(" ", text)
    for mode, pattern in _MODE_RE.items():
        if mode != "ltl" and pattern.search(text):
            found.append(mode)
    order = list(_MODE_WORDS)
    return ",".join(sorted(set(found), key=order.index))


_SHIPPER_WORDS = {
    "manufacturer": ["manufactur", "factory", "plant", "fabricat", "mill", "producer"],
    "distributor": ["distribut", "wholesale", "supply", "supplier"],
    "retailer": ["retail", "store", "ecommerce", "e-commerce"],
    "3pl": ["3pl", "third party", "third-party", "warehous", "logistics"],
}


def parse_shipper_type(raw, industry=""):
    text = _norm(raw) or _norm(industry)
    for kind, words in _SHIPPER_WORDS.items():
        if any(w in text for w in words):
            return kind
    return ""


def _pick(row, aliases):
    for alias in aliases:
        if alias in row and str(row[alias]).strip():
            return str(row[alias]).strip()
    return ""


def parse_csv(text, source=None):
    """
    Read the upload into normalized dicts. Returns (rows, source, headers).

    Every row keeps `_line` (its line number in the file) and `_problems`, so the
    preview can point at a specific line rather than saying "some rows failed".
    """
    stream = io.StringIO(text)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(stream, dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h]
    if not headers:
        return [], "generic", []

    source = source or sniff_source(headers)
    fields = FIELD_MAPS[source]
    thousands = source == "zoominfo" and any(
        "000s" in _norm(h) for h in headers)

    rows = []
    for line_no, raw_row in enumerate(reader, start=2):
        row = {_norm(k): v for k, v in raw_row.items() if k}
        if not any(str(v or "").strip() for v in row.values()):
            continue

        problems = []
        get = lambda key: _pick(row, fields.get(key, []))    # noqa: E731

        first, last = get("first_name"), get("last_name")
        full = get("full_name")
        if not (first or last) and full:
            parts = full.split()
            first, last = (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (full, "")
        name = " ".join(p for p in (first, last) if p).strip()

        email, problem = clean_email(get("email"))
        if problem:
            problems.append(problem)
        direct, problem = clean_phone(get("direct_dial"))
        if problem:
            problems.append(problem)
        mobile, problem = clean_phone(get("mobile"))
        if problem:
            problems.append(problem)
        switchboard, problem = clean_phone(get("switchboard"))
        if problem:
            problems.append(problem)

        industry = get("industry")
        rows.append({
            "_line": line_no,
            "_problems": problems,
            "external_id": get("external_id"),
            "company_external_id": get("company_external_id"),
            "company": get("company"),
            "domain": clean_domain(get("domain")),
            "name": name,
            "title": get("title"),
            "email": email,
            "email_status_raw": get("email_status"),
            "direct_dial": direct,
            "mobile": mobile,
            "switchboard": switchboard,
            "city": get("city"),
            "state": get("state").upper()[:2],
            "industry": industry,
            "naics": get("naics"),
            "employees": parse_number(get("employees")),
            "revenue": parse_number(get("revenue"), thousands=thousands),
            "linkedin": get("linkedin"),
            "role_started": parse_date(get("started")),
            "modes": parse_modes(get("modes")),
            "loads_per_month": parse_number(get("loads_per_month")),
            "lanes": get("lanes"),
            "incumbents": get("incumbents"),
            "shipper_type": parse_shipper_type(get("shipper_type"), industry),
            "dock_count": parse_number(get("dock_count")),
            "owner_email": _norm(get("owner_email")),
            "owner_name": get("owner_name"),
        })
    return rows, source, headers


# --------------------------------------------------------------- provenance --

def point_provenance(kind, source, status_raw, trust_vendor, today):
    """
    What we are willing to say about an imported number or address.

    This is the honest core of the module. `trust_vendor` off — the default —
    means a vendor's "Verified" lands as `probable` with their wording preserved
    in `method`, and `last_verified` stays NULL because we do not know the date
    they checked. With it on, their claim becomes ours, dated today, and the
    method line says whose claim it originally was so the provenance survives.
    """
    label = SOURCE_LABELS.get(source, source)
    raw = _norm(status_raw)
    mapped = _EMAIL_STATUS.get(raw) if raw else None

    if kind == "email":
        status = mapped or "probable"
        if raw and raw in _VENDOR_TRUSTED and trust_vendor:
            return {"status": "verified", "confidence": 82,
                    "source": "vendor_verified", "last_verified": today,
                    "method": f"{label} reported {status_raw!r} at import; accepted "
                              f"at face value"}
        if raw:
            return {"status": status, "confidence":
                    {"invalid": 0, "guessed": 32, "catch_all": 40}.get(status, 60),
                    "source": "vendor", "last_verified": None,
                    "method": f"{label} reported {status_raw!r}; not independently "
                              f"confirmed"}
        return {"status": "probable", "confidence": 52, "source": "vendor",
                "last_verified": None,
                "method": f"imported from {label}; no verification status supplied"}

    # Phones. No mainstream export states a per-number verification status, so
    # there is nothing to trust or distrust — it is an unconfirmed claim either
    # way, and a switchboard is a published number rather than a vendor's.
    if kind == "switchboard":
        return {"status": "probable", "confidence": 55, "source": "website",
                "last_verified": None,
                "method": f"main line imported from {label}"}
    return {"status": "probable", "confidence": enrichment.SOURCE_TRUST["vendor"],
            "source": "vendor", "last_verified": None,
            "method": f"imported from {label}; never dialed by us"}


# ----------------------------------------------------------------- planning --

def _company_key(row):
    """
    Stable identity for a company across re-imports, best available first.

    Deliberately does NOT fall back to the contact's `external_id`. Two people at
    the same firm carry two contact ids and one company; keying on the contact id
    would import them as two companies, each with its own call-list entry, and a
    rep would dial the same switchboard twice.
    """
    if row["company_external_id"]:
        return ("ext", _norm(row["company_external_id"]))
    if row["domain"]:
        return ("domain", row["domain"])
    return ("name", _norm(row["company"]), row["state"])


def plan(conn, org_id, rows, source, options=None):
    """
    Work out what this import would do, without doing any of it.

    Read-only. Everything the preview screen shows comes from here, so what the
    user approves is exactly what runs.
    """
    options = options or {}
    trust_vendor = bool(options.get("trust_vendor"))
    today = date.today().isoformat()

    reps = conn.execute(
        "SELECT id, name, email, territory FROM reps WHERE org_id = ? AND status = 'active'",
        (org_id,)).fetchall()
    rep_by_email = {_norm(r["email"]): r["id"] for r in reps if r["email"]}
    rep_by_name = {_norm(r["name"]): r["id"] for r in reps}
    rep_by_state = {}
    for r in reps:
        for st in (s.strip().upper() for s in (r["territory"] or "").split(",") if s.strip()):
            rep_by_state.setdefault(st, r["id"])
    default_rep = options.get("default_rep_id") or (reps[0]["id"] if reps else None)

    existing_companies = {}
    for c in conn.execute(
            "SELECT id, name, state, domain, external_id FROM companies WHERE org_id = ?",
            (org_id,)).fetchall():
        if c["external_id"]:
            existing_companies[("ext", _norm(c["external_id"]))] = c
        if c["domain"]:
            existing_companies[("domain", _norm(c["domain"]))] = c
        existing_companies.setdefault(("name", _norm(c["name"]), (c["state"] or "").upper()), c)

    existing_contacts = {}
    for row in conn.execute(
            """SELECT ct.id, ct.name, ct.company_id, cp.value AS email
                 FROM contacts ct
            LEFT JOIN contact_points cp ON cp.contact_id = ct.id AND cp.kind = 'email'
                WHERE ct.org_id = ?""", (org_id,)).fetchall():
        if row["email"]:
            existing_contacts[("email", _norm(row["email"]))] = row
        existing_contacts.setdefault(("name", row["company_id"], _norm(row["name"])), row)

    suppressed_values = {s["value"] for s in conn.execute(
        "SELECT value FROM suppression WHERE org_id = ?", (org_id,)).fetchall()}

    companies_new, companies_seen = {}, {}
    creates, updates, skips = [], [], []
    suppressed_rows, duplicate_rows = [], []
    unmatched_owners, states = {}, {}

    for row in rows:
        problems = list(row["_problems"])
        if not row["company"]:
            problems.append("no company name")
        if not row["name"]:
            problems.append("no contact name")
        if not (row["email"] or row["direct_dial"] or row["mobile"] or row["switchboard"]):
            problems.append("no phone or email — nothing to reach them on")

        if [p for p in problems if "truncated" not in p]:
            skips.append({"line": row["_line"], "company": row["company"],
                          "name": row["name"], "problems": problems})
            continue

        # Deduplicate within the file itself. Vendor exports routinely repeat a
        # contact once per matched account.
        dedupe_key = ("email", row["email"]) if row["email"] else \
            ("name", _norm(row["company"]), _norm(row["name"]))
        if dedupe_key in companies_seen:
            duplicate_rows.append({"line": row["_line"], "name": row["name"],
                                   "company": row["company"],
                                   "first_seen": companies_seen[dedupe_key]})
            continue
        companies_seen[dedupe_key] = row["_line"]

        ckey = _company_key(row)
        prior_company = existing_companies.get(ckey)
        if prior_company is None and ckey not in companies_new:
            companies_new[ckey] = row["company"]

        # Which rep gets it: an explicit owner column, else the territory that
        # covers the state, else the fallback. An unmatched owner is reported
        # rather than silently reassigned.
        rep_id = rep_by_email.get(row["owner_email"]) or rep_by_name.get(_norm(row["owner_name"]))
        if rep_id is None and (row["owner_email"] or row["owner_name"]):
            label = row["owner_name"] or row["owner_email"]
            unmatched_owners[label] = unmatched_owners.get(label, 0) + 1
        if rep_id is None:
            rep_id = rep_by_state.get(row["state"], default_rep)

        points, blocked = [], []
        for kind, value in (("direct_dial", row["direct_dial"]),
                            ("mobile", row["mobile"]),
                            ("switchboard", row["switchboard"]),
                            ("email", row["email"])):
            if not value:
                continue
            prov = point_provenance(kind, source,
                                    row["email_status_raw"] if kind == "email" else "",
                                    trust_vendor, today)
            key = (compliance.normalize_email(value) if kind == "email"
                   else compliance.normalize_phone(value))
            if key in suppressed_values:
                blocked.append(kind)
                prov = {"status": "invalid", "confidence": 0, "source": prov["source"],
                        "last_verified": today,
                        "method": "on this organization's suppression list at import"}
            points.append({"kind": kind, "value": value, **prov})

        if blocked:
            suppressed_rows.append({"line": row["_line"], "name": row["name"],
                                    "company": row["company"], "kinds": blocked})

        contact_key = ("email", row["email"]) if row["email"] else None
        prior_contact = existing_contacts.get(contact_key) if contact_key else None
        if prior_contact is None and prior_company is not None:
            prior_contact = existing_contacts.get(
                ("name", prior_company["id"], _norm(row["name"])))

        entry = {"line": row["_line"], "row": row, "points": points,
                 "rep_id": rep_id, "company_key": ckey,
                 "company_exists": prior_company is not None,
                 "company_id": prior_company["id"] if prior_company else None,
                 "contact_id": prior_contact["id"] if prior_contact else None,
                 "suppressed": bool(blocked), "warnings":
                     [p for p in problems if "truncated" in p]}
        (updates if prior_contact else creates).append(entry)
        if row["state"]:
            states[row["state"]] = states.get(row["state"], 0) + 1

    return {
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "trust_vendor": trust_vendor,
        "total_rows": len(rows),
        "creates": creates,
        "updates": updates,
        "skips": skips,
        "duplicates": duplicate_rows,
        "suppressed": suppressed_rows,
        "new_companies": sorted(companies_new.values()),
        "unmatched_owners": unmatched_owners,
        "states": dict(sorted(states.items(), key=lambda kv: -kv[1])),
        "counts": {
            "contacts_new": len(creates),
            "contacts_update": len(updates),
            "companies_new": len(companies_new),
            "points": sum(len(e["points"]) for e in creates + updates),
            "skipped": len(skips),
            "duplicates": len(duplicate_rows),
            "suppressed": len(suppressed_rows),
        },
    }


# ------------------------------------------------------------------ applying --

def apply(conn, org_id, rows, source, user_id, options=None):
    """
    Write the plan. Returns a report shaped like the plan's counts.

    Re-planned from the stored bytes rather than from a serialized plan, so an
    import applied an hour after it was previewed sees the database as it is now
    — a contact somebody added in between is an update, not a duplicate.
    """
    result = plan(conn, org_id, rows, source, options)
    now = datetime.now().isoformat(timespec="seconds")
    today = date.today().isoformat()
    created = {"companies": 0, "contacts": 0, "points": 0, "targets": 0}
    updated = {"companies": 0, "contacts": 0, "points": 0}

    company_ids = {}

    for entry in result["creates"] + result["updates"]:
        row = entry["row"]
        ckey = entry["company_key"]

        # ---- company -----------------------------------------------------
        company_id = company_ids.get(ckey) or entry["company_id"]
        if company_id is None:
            company_id = conn.execute(
                """INSERT INTO companies (org_id, name, domain, industry, naics, city,
                                          state, timezone, employees, revenue_est, modes,
                                          loads_per_month, dock_count, lanes, incumbents,
                                          shipper_type, source, confidence, last_verified,
                                          external_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (org_id, row["company"], row["domain"], row["industry"], row["naics"],
                 row["city"], row["state"], _timezone_for(row["state"]),
                 int(row["employees"]) if row["employees"] else None, row["revenue"],
                 row["modes"], int(row["loads_per_month"]) if row["loads_per_month"] else None,
                 int(row["dock_count"]) if row["dock_count"] else None,
                 row["lanes"], row["incumbents"], row["shipper_type"],
                 "vendor" if source != "generic" else "manual",
                 enrichment.SOURCE_TRUST.get("vendor", 58), None,
                 # NULL, not "". Two companies with no external id must not
                 # collide on UNIQUE(org_id, external_id) — NULLs don't compare
                 # equal, empty strings do.
                 f"{source}:{row['company_external_id']}"
                 if row["company_external_id"] else None,
                 now)).lastrowid
            created["companies"] += 1
        else:
            # Only fill blanks. An import must not overwrite a field a rep
            # corrected by hand after a conversation with the customer — that
            # correction is better data than the vendor's, by definition.
            filled = _fill_blanks(conn, org_id, "companies", company_id, {
                "domain": row["domain"], "industry": row["industry"],
                "naics": row["naics"], "city": row["city"], "state": row["state"],
                "employees": int(row["employees"]) if row["employees"] else None,
                "revenue_est": row["revenue"], "modes": row["modes"],
                "loads_per_month": int(row["loads_per_month"]) if row["loads_per_month"] else None,
                "dock_count": int(row["dock_count"]) if row["dock_count"] else None,
                "lanes": row["lanes"], "incumbents": row["incumbents"],
                "shipper_type": row["shipper_type"],
            })
            if filled:
                updated["companies"] += 1
        company_ids[ckey] = company_id

        # ---- contact -----------------------------------------------------
        contact_id = entry["contact_id"]
        if contact_id is None:
            contact_id = conn.execute(
                """INSERT INTO contacts (org_id, company_id, name, title, role_type,
                                         seniority, department, linkedin, role_started,
                                         status, source, confidence, last_verified,
                                         created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (org_id, company_id, row["name"], row["title"],
                 _role_type(row["title"]), _seniority(row["title"]), "",
                 row["linkedin"], row["role_started"], "active",
                 "vendor" if source != "generic" else "manual",
                 enrichment.SOURCE_TRUST.get("vendor", 58), None, now)).lastrowid
            created["contacts"] += 1
        else:
            filled = _fill_blanks(conn, org_id, "contacts", contact_id, {
                "title": row["title"], "linkedin": row["linkedin"],
                "role_started": row["role_started"],
            })
            if filled:
                updated["contacts"] += 1

        # ---- contact points ----------------------------------------------
        have = {(p["kind"], p["value"]): p for p in conn.execute(
            "SELECT * FROM contact_points WHERE org_id = ? AND contact_id = ?",
            (org_id, contact_id)).fetchall()}
        for point in entry["points"]:
            key = (point["kind"], point["value"])
            prior = have.get(key)
            if prior is None:
                conn.execute(
                    """INSERT INTO contact_points (org_id, contact_id, kind, value, status,
                                                   confidence, source, method, last_verified,
                                                   fail_count, is_primary, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (org_id, contact_id, point["kind"], point["value"], point["status"],
                     point["confidence"], point["source"], point["method"],
                     point["last_verified"], 0,
                     1 if point["kind"] == "direct_dial" else 0, now))
                created["points"] += 1
            elif point["status"] == "invalid" and prior["status"] != "invalid":
                # A suppression hit always wins over whatever was there.
                conn.execute(
                    """UPDATE contact_points SET status = 'invalid', confidence = 0,
                              method = ?, last_verified = ? WHERE id = ? AND org_id = ?""",
                    (point["method"], today, prior["id"], org_id))
                updated["points"] += 1

        # ---- target ------------------------------------------------------
        existing_target = conn.execute(
            """SELECT id FROM targets WHERE org_id = ? AND company_id = ? AND contact_id = ?""",
            (org_id, company_id, contact_id)).fetchone()
        if existing_target is None:
            target_id = conn.execute(
                """INSERT INTO targets (org_id, company_id, contact_id, rep_id, status,
                                        created_at) VALUES (?,?,?,?,?,?)""",
                (org_id, company_id, contact_id, entry["rep_id"], "new", now)).lastrowid
            created["targets"] += 1
            conn.execute(
                """INSERT INTO activities (org_id, target_id, rep_id, kind, summary,
                                           detail, actor, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (org_id, target_id, entry["rep_id"], "system",
                 f"Imported from {result['source_label']}",
                 f"Line {entry['line']} of the upload."
                 + (" One or more contact points are on the suppression list and were "
                    "written invalid." if entry["suppressed"] else ""),
                 "system", now))

    conn.commit()
    report = {
        "source": source,
        "source_label": result["source_label"],
        "trust_vendor": result["trust_vendor"],
        "total_rows": result["total_rows"],
        "created": created,
        "updated": updated,
        "skipped": len(result["skips"]),
        "duplicates": len(result["duplicates"]),
        "suppressed": len(result["suppressed"]),
        "skip_reasons": _tally(result["skips"]),
        "applied_at": now,
    }
    return report


def _fill_blanks(conn, org_id, table, row_id, values):
    """
    Write only the columns that are currently empty.

    An import is newer than the database but not necessarily better than it: a
    rep who wrote "38 loads/month" after asking the customer holds better data
    than a vendor's estimate, and a re-import must not quietly undo that.
    """
    if table not in ("companies", "contacts"):
        raise ValueError(f"_fill_blanks called on unexpected table: {table}")
    current = conn.execute(f"SELECT * FROM {table} WHERE id = ? AND org_id = ?",
                           (row_id, org_id)).fetchone()
    if current is None:
        return 0
    updates = {k: v for k, v in values.items()
               if v not in (None, "") and not current[k]}
    if not updates:
        return 0
    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = ? AND org_id = ?",
                 (*updates.values(), row_id, org_id))
    return len(updates)


_DECISION_WORDS = ["vp ", "vice president", "director", "head of", "chief", "owner",
                   "president", "manager of logistics", "transportation manager",
                   "logistics manager", "supply chain manager", "traffic manager"]
_GATEKEEPER_WORDS = ["assistant", "receptionist", "coordinator", "administrator"]
_INFLUENCER_WORDS = ["manager", "supervisor", "lead", "planner", "buyer", "purchasing"]


def _role_type(title):
    text = _norm(title)
    if not text:
        return "unknown"
    if any(w in text for w in _GATEKEEPER_WORDS):
        return "gatekeeper"
    if any(w in text for w in _DECISION_WORDS):
        return "decision_maker"
    if any(w in text for w in _INFLUENCER_WORDS):
        return "influencer"
    return "unknown"


def _seniority(title):
    text = _norm(title)
    if any(w in text for w in ("chief", "ceo", "coo", "cfo", "president", "owner")):
        return "c_level"
    if "vp" in text or "vice president" in text:
        return "vp"
    if "director" in text or "head of" in text:
        return "director"
    if "manager" in text or "supervisor" in text:
        return "manager"
    return "ic" if text else ""


# Minimal state -> timezone table. Quiet hours are evaluated in the prospect's
# local time, so a company row with no timezone is a company that cannot be
# safely dialed — Central is the wrong default for California and the dial gate
# would happily allow a 6am call. Anything unmapped gets Central and the gate's
# "could not be resolved" warning surfaces it.
_TZ = {
    "CA": "America/Los_Angeles", "WA": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "AZ": "America/Phoenix",
    "CO": "America/Denver", "UT": "America/Denver", "NM": "America/Denver",
    "MT": "America/Denver", "WY": "America/Denver", "ID": "America/Denver",
    "NY": "America/New_York", "NJ": "America/New_York", "PA": "America/New_York",
    "MA": "America/New_York", "CT": "America/New_York", "RI": "America/New_York",
    "VT": "America/New_York", "NH": "America/New_York", "ME": "America/New_York",
    "DE": "America/New_York", "MD": "America/New_York", "DC": "America/New_York",
    "VA": "America/New_York", "WV": "America/New_York", "NC": "America/New_York",
    "SC": "America/New_York", "GA": "America/New_York", "FL": "America/New_York",
    "OH": "America/New_York", "MI": "America/New_York",
    "IN": "America/Indiana/Indianapolis",
    "KY": "America/New_York",
}


def _timezone_for(state):
    return _TZ.get((state or "").upper(), "America/Chicago")


def _tally(skips):
    counts = {}
    for skip in skips:
        for problem in skip["problems"]:
            # Collapse the value-specific part so the tally is readable.
            key = re.sub(r"'[^']*'", "…", problem)
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
