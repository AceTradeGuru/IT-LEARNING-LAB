"""
QuotaOS — Salesforce and HubSpot import.

Nobody can switch to you without a migration path, so this is the feature that
makes the rest of the product reachable. Drop in a CSV exported from Salesforce or
HubSpot and QuotaOS parses it, maps it onto our model, and shows you exactly what
it is about to do before it writes anything.

Four things make this more than a CSV loader:

  1. **It recognizes the export.** Salesforce and HubSpot name the same column four
     different ways. `sniff_source` reads the header row and picks a field map, so
     the user is not asked to hand-map twenty columns on their first run.

  2. **It maps their stages to ours.** A pipeline with "Id. Decision Makers" and
     "Perception Analysis" has to land on our six stages. Unrecognized stages are
     surfaced in the preview rather than silently bucketed.

  3. **It previews, then applies.** `plan()` is read-only and returns exactly what
     would be created, updated, and skipped, with reasons. Nothing touches the
     database until `apply()` — an import that surprises you is one you can't trust
     with a book of business.

  4. **It makes slippage visible on the second run.** No CRM export contains the
     date a deal was *originally* forecast to close, so the first import sets
     `close_date_original = close_date` and the health engine's slippage signal
     stays quiet. On every later import, an existing deal keeps its original date
     while `close_date` moves — which is precisely how a pushed deal becomes
     visible here and never was in the CRM it came from.

Re-importing is safe and expected: rows are keyed on the source system's record id,
so the same file applied twice updates rather than duplicates.
"""

import csv
import io
import re
from datetime import date, datetime

# ---------------------------------------------------------------- detection --

# Header fingerprints. Matching is case- and whitespace-insensitive because
# exports vary by locale and by which columns the user happened to tick.
_SIGNATURES = {
    "salesforce": {"opportunity name", "stage", "close date"},
    "hubspot": {"deal name", "deal stage", "close date"},
}

# Our field <- their column, first match wins. Several aliases per field because
# Salesforce report exports and the Data Loader disagree with each other, and
# HubSpot renames columns between the UI export and the API export.
FIELD_MAPS = {
    "salesforce": {
        "external_id": ["opportunity id", "id", "18 digit id", "record id"],
        "account": ["account name", "account", "account name: account name"],
        "name": ["opportunity name", "name"],
        "amount": ["amount", "amount (converted)", "annual contract value"],
        "stage": ["stage", "stage name", "opportunity stage"],
        "probability": ["probability (%)", "probability"],
        "close_date": ["close date", "closedate"],
        "created_date": ["created date", "createddate", "opportunity created date"],
        "last_activity_date": ["last activity", "last activity date", "last modified date"],
        "next_step": ["next step", "nextstep"],
        "source": ["lead source", "opportunity source", "source"],
        "owner_name": ["opportunity owner", "owner name", "owner", "opportunity owner name"],
        "owner_email": ["opportunity owner email", "owner email", "owner: email"],
    },
    "hubspot": {
        "external_id": ["record id", "deal id", "id"],
        "account": ["associated company", "company name", "associated company (primary)"],
        "name": ["deal name", "name"],
        "amount": ["amount", "deal amount", "amount in company currency"],
        "stage": ["deal stage", "stage"],
        "probability": ["deal probability", "probability"],
        "close_date": ["close date", "closedate"],
        "created_date": ["create date", "created date"],
        "last_activity_date": ["last activity date", "last engagement date"],
        "next_step": ["next step", "next activity"],
        "source": ["original source", "deal type", "source"],
        "owner_name": ["deal owner", "owner", "hubspot owner"],
        "owner_email": ["deal owner email", "owner email"],
    },
}
# A hand-rolled or unrecognized export falls back to the union of both maps, which
# handles the common case of someone reordering or renaming a couple of columns.
FIELD_MAPS["generic"] = {
    k: FIELD_MAPS["salesforce"].get(k, []) + FIELD_MAPS["hubspot"].get(k, [])
    for k in FIELD_MAPS["salesforce"]
}

OUR_STAGES = ["discovery", "demo", "proposal", "negotiation", "closed_won", "closed_lost"]

# Their pipeline -> ours. Both platforms ship defaults most orgs then edit, so
# anything unrecognized is reported in the preview instead of being guessed at.
STAGE_MAPS = {
    "salesforce": {
        "prospecting": "discovery",
        "qualification": "discovery",
        "needs analysis": "discovery",
        "value proposition": "demo",
        "id. decision makers": "demo",
        "identify decision makers": "demo",
        "perception analysis": "demo",
        "proposal/price quote": "proposal",
        "proposal": "proposal",
        "negotiation/review": "negotiation",
        "negotiation": "negotiation",
        "closed won": "closed_won",
        "closed lost": "closed_lost",
    },
    "hubspot": {
        "appointmentscheduled": "discovery",
        "appointment scheduled": "discovery",
        "qualifiedtobuy": "discovery",
        "qualified to buy": "discovery",
        "presentationscheduled": "demo",
        "presentation scheduled": "demo",
        "decisionmakerboughtin": "proposal",
        "decision maker bought-in": "proposal",
        "contractsent": "negotiation",
        "contract sent": "negotiation",
        "closedwon": "closed_won",
        "closed won": "closed_won",
        "closedlost": "closed_lost",
        "closed lost": "closed_lost",
    },
}
STAGE_MAPS["generic"] = {**STAGE_MAPS["salesforce"], **STAGE_MAPS["hubspot"],
                         **{s.replace("_", " "): s for s in OUR_STAGES},
                         **{s: s for s in OUR_STAGES}}

_DEFAULT_PROBABILITY = {"discovery": 15, "demo": 30, "proposal": 55,
                        "negotiation": 75, "closed_won": 100, "closed_lost": 0}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def sniff_source(headers):
    """Identify the export from its header row."""
    have = {_norm(h) for h in headers}
    best, score = "generic", 0
    for name, signature in _SIGNATURES.items():
        hits = len(signature & have)
        if hits > score:
            best, score = name, hits
    # Require most of the signature, otherwise this is somebody's own spreadsheet.
    return best if score >= 2 else "generic"


# ------------------------------------------------------------------ parsing --

_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%Y/%m/%d",
                 "%d-%b-%Y", "%b %d, %Y", "%Y-%m-%dT%H:%M:%S"]


def parse_date(raw):
    """
    Best-effort date parse across the formats these exports actually emit.

    Ambiguous D/M vs M/D is resolved in favour of M/D — both platforms default to
    US format on export — except where the first component is over 12 and the
    reading is unambiguous.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    head = raw.split("T")[0].split(" ")[0] if len(raw) > 10 else raw
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", head)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        month, day = (a, b) if a <= 12 else (b, a)
        try:
            return date(y, month, day)
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(raw):
    """'$1,250,000.00' -> 1250000. Currency symbols and separators vary by locale."""
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(raw))
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return int(round(float(cleaned)))
    except ValueError:
        return None


def _pick(row_lc, aliases):
    for a in aliases:
        if a in row_lc and str(row_lc[a]).strip() != "":
            return str(row_lc[a]).strip()
    return None


def parse_csv(text):
    """
    Read the export into normalized dicts.

    Returns (rows, source, headers). Each row carries `_row` — its line number in
    the original file — so every message in the preview can point at a real line.
    """
    # Excel on Windows writes a BOM; csv will otherwise fold it into header one.
    text = text.lstrip("﻿")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    source = sniff_source(headers)
    fmap = FIELD_MAPS[source]

    rows = []
    for i, raw in enumerate(reader, start=2):        # line 1 is the header
        row_lc = {_norm(k): v for k, v in raw.items() if k is not None}
        rec = {"_row": i}
        for field, aliases in fmap.items():
            rec[field] = _pick(row_lc, aliases)
        rec["amount"] = parse_amount(rec.get("amount"))
        rec["probability"] = parse_amount(rec.get("probability"))
        rec["close_date"] = parse_date(rec.get("close_date"))
        rec["created_date"] = parse_date(rec.get("created_date"))
        rec["last_activity_date"] = parse_date(rec.get("last_activity_date"))
        rec["stage_raw"] = rec.get("stage")
        rec["stage"] = map_stage(source, rec.get("stage"))
        rows.append(rec)
    return rows, source, headers


def map_stage(source, raw):
    """Their stage -> ours, or None when we don't recognize it."""
    key = _norm(raw)
    if not key:
        return None
    return STAGE_MAPS.get(source, STAGE_MAPS["generic"]).get(key)


# ----------------------------------------------------------------- planning --

def plan(conn, org_id, rows, source, create_missing_reps=False):
    """
    Work out what this import would do, without doing any of it.

    Read-only. Everything the preview screen shows comes from here, so what the
    user approves is exactly what runs.
    """
    reps = conn.execute(
        "SELECT id, name, email, external_id FROM reps WHERE org_id = ?", (org_id,)
    ).fetchall()
    by_email = {_norm(r["email"]): r["id"] for r in reps if r["email"]}
    by_name = {_norm(r["name"]): r["id"] for r in reps}

    existing = {}
    for d in conn.execute(
        """SELECT id, external_id, amount, stage, close_date, close_date_original
             FROM deals WHERE org_id = ? AND external_id IS NOT NULL""", (org_id,)
    ).fetchall():
        existing[d["external_id"]] = d

    creates, updates, skips = [], [], []
    unmatched_owners, unknown_stages = {}, {}
    new_reps = {}

    for r in rows:
        problems = []
        if not r.get("account") and not r.get("name"):
            problems.append("no account or opportunity name")
        if r.get("amount") is None:
            problems.append("amount missing or unparseable")
        if not r.get("close_date"):
            problems.append("close date missing or unparseable")
        if not r.get("stage"):
            problems.append(f"unrecognized stage {r.get('stage_raw')!r}")
            if r.get("stage_raw"):
                unknown_stages[r["stage_raw"]] = unknown_stages.get(r["stage_raw"], 0) + 1

        owner_key = _norm(r.get("owner_email")) or _norm(r.get("owner_name"))
        rep_id = by_email.get(_norm(r.get("owner_email"))) or by_name.get(_norm(r.get("owner_name")))
        if rep_id is None:
            label = r.get("owner_name") or r.get("owner_email") or "(no owner)"
            unmatched_owners[label] = unmatched_owners.get(label, 0) + 1
            if create_missing_reps and (r.get("owner_name") or r.get("owner_email")):
                new_reps[owner_key] = {"name": r.get("owner_name") or r.get("owner_email"),
                                       "email": r.get("owner_email") or ""}
            else:
                problems.append(f"owner {label!r} is not a rep in QuotaOS")

        if problems:
            skips.append({**r, "problems": problems})
            continue

        ext = r.get("external_id")
        prior = existing.get(ext) if ext else None
        if prior:
            changes = {}
            if prior["amount"] != r["amount"]:
                changes["amount"] = (prior["amount"], r["amount"])
            if prior["stage"] != r["stage"]:
                changes["stage"] = (prior["stage"], r["stage"])
            if prior["close_date"][:10] != r["close_date"].isoformat():
                changes["close_date"] = (prior["close_date"][:10], r["close_date"].isoformat())
            # The original forecast date is never overwritten. That is what turns
            # a second import into slippage the source CRM could not show.
            updates.append({**r, "deal_id": prior["id"], "changes": changes,
                            "close_date_original": prior["close_date_original"][:10],
                            "rep_id": rep_id})
        else:
            creates.append({**r, "rep_id": rep_id, "owner_key": owner_key})

    return {
        "source": source,
        "total": len(rows),
        "creates": creates,
        "updates": updates,
        "skips": skips,
        "unmatched_owners": unmatched_owners,
        "unknown_stages": unknown_stages,
        "new_reps": new_reps,
        "changed_updates": [u for u in updates if u["changes"]],
    }


# ------------------------------------------------------------------ applying --

def apply(conn, org_id, rows, source, user_id, create_missing_reps=False):
    """
    Write the plan. Returns a report suitable for storing and showing.

    Recomputed from `plan()` rather than trusting a plan handed in from a form —
    the database may have moved between preview and apply, and a stale plan would
    write stale decisions.
    """
    today = date.today().isoformat()
    p = plan(conn, org_id, rows, source, create_missing_reps)

    # Create any missing owners first, so their deals can reference them.
    created_reps = {}
    for key, info in p["new_reps"].items():
        rid = conn.execute(
            """INSERT INTO reps (org_id, name, email, title, role, team, region, segment,
                                 hire_date, quota_annual, status, notes, external_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, info["name"], info["email"] or f"{key}@imported.local",
             "Account Executive", "rep", "Imported", "—", "Mid-Market", today, 0,
             "active",
             f"Created by {source} import — set quota, team, and manager before "
             f"health scoring is meaningful.", source),
        ).lastrowid
        created_reps[key] = rid

    # Re-plan now that those reps exist, so their rows move from skip to create.
    if created_reps:
        p = plan(conn, org_id, rows, source, create_missing_reps=False)

    n_created = 0
    for c in p["creates"]:
        created = (c.get("created_date") or c["close_date"]).isoformat()
        conn.execute(
            """INSERT INTO deals (org_id, rep_id, account, amount, stage, probability,
                                  created_date, close_date, close_date_original,
                                  last_activity_date, next_step, source,
                                  external_id, external_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, c["rep_id"], c.get("account") or c.get("name"), c["amount"],
             c["stage"],
             c["probability"] if c["probability"] is not None
             else _DEFAULT_PROBABILITY[c["stage"]],
             created, c["close_date"].isoformat(),
             # First sighting: original == current, so nothing reads as slipped.
             c["close_date"].isoformat(),
             c["last_activity_date"].isoformat() if c["last_activity_date"] else None,
             c.get("next_step"), c.get("source") or source.title(),
             c.get("external_id"), source),
        )
        n_created += 1

    n_updated = 0
    for u in p["updates"]:
        if not u["changes"]:
            continue
        conn.execute(
            """UPDATE deals SET amount = ?, stage = ?, close_date = ?,
                     last_activity_date = ?, next_step = ?, rep_id = ?
                WHERE id = ? AND org_id = ?""",
            (u["amount"], u["stage"], u["close_date"].isoformat(),
             u["last_activity_date"].isoformat() if u["last_activity_date"] else None,
             u.get("next_step"), u["rep_id"], u["deal_id"], org_id),
        )
        n_updated += 1

    return {
        "source": source,
        "total": p["total"],
        "created": n_created,
        "updated": n_updated,
        "unchanged": len(p["updates"]) - n_updated,
        "skipped": len(p["skips"]),
        "reps_created": len(created_reps),
        "skip_reasons": _tally(p["skips"]),
    }


def _tally(skips):
    out = {}
    for s in skips:
        for p in s["problems"]:
            # Collapse per-row detail into a countable reason.
            key = re.sub(r"'[^']*'", "…", p)
            out[key] = out.get(key, 0) + 1
    return out
