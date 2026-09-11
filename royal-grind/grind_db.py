"""
Royal Grind — database build and demo seed.

Rebuild from scratch:

    python3 grind_db.py            # schema + a demo brokerage to click through
    python3 grind_db.py --empty    # schema only, for a real deployment

The seed is a mid-market freight brokerage in Kansas City with six reps and a
prospect list with a story on every record, because a demo where every record is
clean proves nothing about a product whose entire thesis is that records are not
clean. The ones that matter:

    Vantage Aerospace       the hero. New facility, new VP of supply chain,
                            verified direct dial. Scores in the nineties and
                            every point of it is attributable.
    Cedar Ridge Foods       high fit, no phone number at all. Proves the list
                            does not silently hide what it cannot dial.
    Ironwood Building       the contact left four months ago and the record
                            still says Director. This is the failure mode the
                            whole data layer exists to surface.
    Halcyon Plastics        verified number, verification expired in June.
                            Reads as probable today, not verified.
    Kestrel Chemical        on the suppression list. Never appears on a list.
    Sandpiper Produce       California — blocked before 8am Pacific no matter
                            what the opportunity score says.
    Longview Steel          flatbed-only shipper, this broker runs LTL and dry
                            van. High volume, negative fit, correctly buried.
    Meridian Industries     $4B enterprise. Real freight, wrong sales motion.
    Thistle Distribution    six attempts, zero connects. The number is the
                            problem, and the engine says so.

Call history is not hand-written. Sixty business days of calls are *simulated*
through `telephony.SimulatedProvider`, whose outcome distribution shifts with
data quality, time of day, and opportunity score — then those outcomes are fed
through `signal_engine.record_outcome` exactly as a live call would be. So the
learning loop on /intelligence is emergent: the lift numbers are whatever the
simulated floor actually produced, and they are different every time the seed is
rebuilt with a different RNG seed. Hand-authored lift figures would have been
faster and would have taught a buyer nothing.

Everything is dated relative to today, so the demo never goes stale.
"""

import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import db
import enrichment
import signal_engine
import telephony

DB_FILE = Path(__file__).parent / "royalgrind.db"
SCHEMA_FILE = Path(__file__).parent / "schema.sql"

TODAY = date.today()
RNG = random.Random(20260911)     # fixed: the demo looks the same every rebuild


def get_connection():
    """SQLite locally, Postgres when DATABASE_URL is set. See db.py."""
    return db.connect(DB_FILE)


def create_tables():
    conn = get_connection()
    conn.executescript(SCHEMA_FILE.read_text())
    conn.commit()
    conn.close()


def _iso(d):
    return d.isoformat()


def _ago(days):
    return _iso(TODAY - timedelta(days=days))


NOW = datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

TZ = {
    "MO": "America/Chicago", "KS": "America/Chicago", "AR": "America/Chicago",
    "IL": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "OK": "America/Chicago", "IA": "America/Chicago", "NE": "America/Chicago",
    "MN": "America/Chicago", "WI": "America/Chicago", "IN": "America/Indiana/Indianapolis",
    "OH": "America/New_York", "GA": "America/New_York", "NC": "America/New_York",
    "PA": "America/New_York", "CA": "America/Los_Angeles", "WA": "America/Los_Angeles",
    "CO": "America/Denver", "AZ": "America/Phoenix", "UT": "America/Denver",
}

CITIES = {
    "MO": ["Springfield", "Joplin", "St. Joseph", "Columbia", "Lee's Summit", "Cape Girardeau"],
    "KS": ["Wichita", "Olathe", "Topeka", "Salina", "Hutchinson"],
    "AR": ["Springdale", "Fort Smith", "Jonesboro", "Conway"],
    "IL": ["Peoria", "Rockford", "Decatur", "Bloomington"],
    "TN": ["Memphis", "Jackson", "Chattanooga", "Murfreesboro"],
    "TX": ["Fort Worth", "Waco", "Tyler", "Lubbock"],
    "OK": ["Tulsa", "Enid", "Muskogee"],
    "IA": ["Cedar Rapids", "Davenport", "Waterloo"],
    "NE": ["Omaha", "Lincoln", "Grand Island"],
    "CA": ["Fresno", "Stockton", "Bakersfield"],
    "WA": ["Yakima", "Spokane"],
    "OH": ["Dayton", "Toledo", "Akron"],
    "GA": ["Macon", "Savannah", "Columbus"],
}

INDUSTRIES = [
    ("Aerospace components", "336413", ["ltl", "expedited", "ftl"], "manufacturer"),
    ("Food processing", "311412", ["reefer", "ftl"], "manufacturer"),
    ("Building products", "321999", ["flatbed", "ftl"], "manufacturer"),
    ("Injection molding", "326199", ["ltl", "ftl"], "manufacturer"),
    ("Steel fabrication", "332312", ["flatbed"], "manufacturer"),
    ("Agricultural equipment", "333111", ["flatbed", "ftl"], "manufacturer"),
    ("Pharmaceutical distribution", "424210", ["ltl", "expedited"], "distributor"),
    ("Automotive parts", "336390", ["ltl", "ftl", "expedited"], "manufacturer"),
    ("Consumer packaged goods", "311999", ["ftl", "ltl"], "manufacturer"),
    ("Industrial chemicals", "325199", ["ftl", "intermodal"], "manufacturer"),
    ("Paper & packaging", "322211", ["ftl", "ltl"], "manufacturer"),
    ("Electrical equipment", "335312", ["ltl", "ftl"], "manufacturer"),
    ("Plumbing supply", "423720", ["ltl", "ftl"], "distributor"),
    ("Furniture", "337121", ["ftl", "ltl"], "manufacturer"),
    ("Cold storage logistics", "493120", ["reefer", "intermodal"], "3pl"),
]

# Deliberately does NOT contain the nine archetype prefixes (Vantage, Cedar
# Ridge, Ironwood, Halcyon, Kestrel, Sandpiper, Longview, Meridian, Thistle) —
# a generated "Halcyon Industries" sitting next to the hand-built "Halcyon
# Plastics" makes the demo read as a bug.
PREFIX = ["Brightline", "Fairhaven", "Blue Harbor", "Talon", "Orchard",
          "Copperfield", "Rosewood", "Grayson", "Pinnacle", "Foxglove", "Wexford",
          "Ashfield", "Marlowe", "Dunmore", "Salt Creek", "Brambleton", "Highgate",
          "Cranbrook", "Elderfield", "Norwood", "Redstone", "Cobalt", "Windrow",
          "Barlow", "Keystone", "Hollis", "Rockport", "Tamarack", "Selby",
          "Alder Creek", "Winfield", "Gantry", "Mayfield", "Sutter", "Delmar",
          "Quarry Hill", "Beaumont", "Ledger", "Palisade", "Ashcroft", "Verdon",
          "Stonebridge", "Kingsley", "Larkspur", "Meadowbrook", "Fenwick",
          "Harrowgate", "Bellweather", "Camden Row", "Oakhurst", "Pemberton",
          "Ravenswood", "Shackleton", "Tilden", "Underwood", "Ваlmoral"]

# Second word, so the name space is prefix x midword x suffix rather than
# prefix x suffix. Two hundred companies out of forty-eight prefixes needs it.
MIDWORD = ["", "", "", "Valley", "Ridge", "Creek", "Point", "Park", "Summit"]

SUFFIX = {
    "manufacturer": ["Industries", "Manufacturing", "Works", "Fabrication", "Products"],
    "distributor": ["Distribution", "Supply", "Wholesale", "Supply Group"],
    "3pl": ["Logistics", "Cold Chain", "Warehousing"],
}

TITLES = [
    ("VP Supply Chain", "decision_maker", "vp", 0.10),
    ("Director of Logistics", "decision_maker", "director", 0.18),
    ("Transportation Manager", "decision_maker", "manager", 0.20),
    ("Logistics Manager", "decision_maker", "manager", 0.14),
    ("Director of Operations", "influencer", "director", 0.10),
    ("Plant Manager", "influencer", "manager", 0.08),
    ("Purchasing Manager", "influencer", "manager", 0.07),
    ("Shipping Supervisor", "influencer", "ic", 0.07),
    ("Executive Assistant", "gatekeeper", "ic", 0.06),
]

FIRST = ["Sarah", "Mike", "Dana", "Carlos", "Priya", "Tom", "Angela", "Derrick",
         "Megan", "Hector", "Joyce", "Ryan", "Nicole", "Samir", "Beth", "Aaron",
         "Lindsey", "Tyrone", "Katie", "Brandon", "Rosa", "Ellis", "Marta",
         "Grant", "Yvonne", "Curtis", "Jenna", "Paul", "Renee", "Victor",
         "Alicia", "Neil", "Tara", "Owen", "Bianca", "Scott", "Leah", "Gus"]
LAST = ["Williams", "Doyle", "Ferraro", "Mendez", "Rao", "Hollister", "Byrne",
        "Ackerman", "Voss", "Delgado", "Kirkland", "Nabors", "Chastain", "Qureshi",
        "Ramsay", "Lindgren", "Pruitt", "Okafor", "Vance", "Salas", "Hutton",
        "Brenner", "Whitlock", "Marsh", "Okonkwo", "Petrov", "Alvarado", "Teague"]

BROKERS = ["TQL", "Echo Global", "Coyote", "C.H. Robinson", "Landstar", "Arrive",
           "Worldwide Express", "RXO", "Uber Freight", "a regional broker"]

TRIGGER_TEXT = {
    "new_facility": [
        ("Announced a ${m}M {city} facility", "Groundbreaking reported; production "
         "expected to begin within {q} quarters. New site means outbound lanes with "
         "no incumbent coverage."),
        ("Broke ground on a {sqft}k sq ft distribution center",
         "Permit filing shows a {dock}-dock building — that is a real outbound operation."),
    ],
    "expansion": [
        ("Adding a second shift at the {city} plant",
         "A second shift is roughly a doubling of outbound volume on the same lanes."),
        ("Capital investment of ${m}M in {city} capacity",
         "Capacity investment ahead of volume they have not yet contracted freight for."),
    ],
    "logistics_hire": [
        ("Posted {n} logistics and supply-chain roles",
         "Three transportation roles open at once usually means the function is "
         "being rebuilt, and vendor lists get rewritten when it is."),
        ("Hiring a Transportation Manager",
         "The seat that owns carrier selection is currently empty."),
    ],
    "leadership_change": [
        ("{name} named VP of Supply Chain",
         "New in the role. New supply-chain leaders review incumbents inside two "
         "quarters and owe nothing to the previous vendor list."),
        ("New Director of Logistics started this quarter",
         "Incoming leadership with budget authority they have not yet spent."),
    ],
    "carrier_trouble": [
        ("Public complaint about missed pickups",
         "Named their broker in a post about a missed pickup on a customer order. "
         "This is the rare case of a shipper stating the pain unprompted."),
        ("Review names service failures on expedited freight",
         "Complaint is specifically about time-sensitive loads — the highest-margin "
         "freight to win."),
    ],
    "contract_window": [
        ("Annual freight RFP opens {month}",
         "Bid window is live. There is a door and it closes."),
        ("Requesting carrier bids for {year} lanes",
         "Procurement is actively collecting bids."),
    ],
    "seasonal_peak": [
        ("Seasonal peak begins in {n} weeks",
         "Volume above what contracted capacity covers, every year, on a schedule."),
    ],
    "new_product": [
        ("Launching a new product line",
         "New pack sizes and new destinations the current routing guide does not cover."),
    ],
    "funding": [
        ("Closed a ${m}M growth round",
         "Budget and a mandate to scale operations."),
        ("Acquired by a strategic buyer",
         "Integration means a consolidated vendor list, and consolidations are bids."),
    ],
    "compliance_event": [
        ("Incumbent carrier's safety rating downgraded",
         "Their carrier's authority is now a risk their own customers will ask about."),
    ],
    "facility_closure": [
        ("Consolidating two sites into {city}",
         "Lanes are being redrawn. The routing guide is briefly in play."),
    ],
    "tariff_shift": [
        ("Shifting sourcing away from overseas suppliers",
         "Inbound origin change forces a new drayage and linehaul plan."),
    ],
}

SOURCES = ["Business Journal", "Company newsroom", "Local news", "LinkedIn",
           "Job board", "Trade press", "Permit filing", "SEC filing"]


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

def seed(conn, quiet=False):
    org_id = _seed_org(conn)
    rep_ids = _seed_people(conn, org_id)
    companies = _seed_companies(conn, org_id)
    _seed_triggers(conn, org_id, companies)
    targets = _seed_targets(conn, org_id, companies, rep_ids)
    _seed_suppression(conn, org_id)
    seq_ids = _seed_sequences(conn, org_id)
    conn.commit()

    calls = _simulate_history(conn, org_id, rep_ids, quiet=quiet)
    _seed_pipeline(conn, org_id, rep_ids, targets)
    _seed_tasks(conn, org_id, rep_ids, targets, seq_ids)
    _rescore(conn, org_id)
    conn.commit()

    # A second tenant with one company, so multi-tenant isolation is something
    # the smoke test can actually assert rather than something the code claims.
    _seed_second_tenant(conn)
    conn.commit()
    return org_id, calls


def _seed_org(conn):
    return conn.execute(
        """INSERT INTO orgs (name, slug, plan, status, pitch, modes, home_markets,
                             ai_cents_cap, recording_policy, quiet_start, quiet_end,
                             retention_days, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("Royal Grind Logistics", "royal-grind", "growth", "active",
         "Asset-light freight brokerage — LTL, dry van and expedited capacity across "
         "the central corridor, with one named rep per account instead of a queue.",
         "ltl,ftl,expedited", "MO,KS,AR,OK,IL",
         9000, "one_party", 8, 20, 730, NOW)).lastrowid


DEMO_PASSWORD = "grind-demo-2026"


def _seed_people(conn, org_id):
    import auth
    reps = [
        ("Marcus Hale", "marcus@royalgrind.example", None, "MO", 210_000, 45),
        ("Dana Whitfield", "dana@royalgrind.example", 1, "KS", 165_000, 50),
        ("Eli Brannon", "eli@royalgrind.example", 1, "AR", 140_000, 55),
        ("Priya Raman", "priya@royalgrind.example", 1, "IL", 150_000, 50),
        ("Colt Jessup", "colt@royalgrind.example", 1, "OK", 120_000, 60),
        ("Nadia Sharpe", "nadia@royalgrind.example", 1, "TX", 135_000, 50),
    ]
    rep_ids = []
    for i, (name, email, mgr, terr, quota, goal) in enumerate(reps):
        rid = conn.execute(
            """INSERT INTO reps (org_id, name, email, manager_id, territory,
                                 quota_quarter, caller_id, daily_call_goal, status,
                                 started_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, name, email, rep_ids[mgr - 1] if mgr else None, terr, quota,
             f"+1816555{1200 + i:04d}"[:14], goal, "active",
             _ago(RNG.randint(200, 1400)), NOW)).lastrowid
        rep_ids.append(rid)

    users = [
        ("Marcus Hale", "owner@royalgrind.example", "owner", rep_ids[0]),
        ("Dana Whitfield", "manager@royalgrind.example", "manager", rep_ids[1]),
        ("Eli Brannon", "rep@royalgrind.example", "member", rep_ids[2]),
    ]
    for name, email, role, rep_id in users:
        conn.execute(
            """INSERT INTO users (org_id, email, password_hash, name, role, status,
                                  rep_id, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (org_id, email, auth.hash_password(DEMO_PASSWORD), name, role, "active",
             rep_id, NOW))
    return rep_ids


# --------------------------------------------------------------- companies --

def _company_row(conn, org_id, **kw):
    kw.setdefault("created_at", NOW)
    cols = ", ".join(kw)
    marks = ", ".join("?" * len(kw))
    return conn.execute(
        f"INSERT INTO companies (org_id, {cols}) VALUES (?, {marks})",
        (org_id, *kw.values())).lastrowid


def _lanes(state, city, n=2):
    others = [(s, RNG.choice(CITIES[s])) for s in RNG.sample(list(CITIES), n)]
    return "\n".join(f"{city}, {state} -> {c}, {s}" for s, c in others)


def _seed_companies(conn, org_id):
    """
    The list. Nine hand-built archetypes, then sixty generated ones so the
    dashboard has a real population behind the stories.
    """
    made = []

    def add(name, state, city, industry, naics, modes, shipper, employees, revenue,
            loads, docks, incumbents, source, confidence, verified_days):
        cid = _company_row(
            conn, org_id, name=name, domain=_domain(name), industry=industry,
            naics=naics, city=city, state=state, timezone=TZ.get(state, "America/Chicago"),
            employees=employees, revenue_est=revenue, modes=",".join(modes),
            loads_per_month=loads, dock_count=docks, lanes=_lanes(state, city),
            incumbents=incumbents, shipper_type=shipper, source=source,
            confidence=confidence,
            last_verified=_ago(verified_days) if verified_days is not None else None,
            external_id=f"rg-{len(made) + 1:05d}")
        made.append({"id": cid, "name": name, "state": state, "city": city,
                     "modes": modes, "loads": loads})
        return cid

    # ---- the nine that carry the demo -------------------------------------
    hero = add("Vantage Aerospace", "MO", "Springfield", "Aerospace components",
               "336413", ["ltl", "expedited", "ftl"], "manufacturer", 220, 74_000_000,
               34, 6, "TQL, Echo Global", "vendor_verified", 88, 9)
    cedar = add("Cedar Ridge Foods", "KS", "Salina", "Food processing", "311412",
                ["reefer", "ftl"], "manufacturer", 410, 132_000_000, 96, 12,
                "C.H. Robinson", "website", 72, 40)
    ironwood = add("Ironwood Building Products", "AR", "Springdale", "Building products",
                   "321999", ["flatbed", "ftl"], "manufacturer", 180, 58_000_000, 44, 5,
                   "Landstar", "vendor", 61, 150)
    halcyon = add("Halcyon Plastics", "MO", "Joplin", "Injection molding", "326199",
                  ["ltl", "ftl"], "manufacturer", 145, 41_000_000, 28, 4,
                  "Worldwide Express", "vendor_verified", 80, 104)
    kestrel = add("Kestrel Chemical", "IL", "Decatur", "Industrial chemicals", "325199",
                  ["ftl", "intermodal"], "manufacturer", 300, 190_000_000, 70, 8,
                  "RXO", "vendor", 65, 60)
    sandpiper = add("Sandpiper Produce", "CA", "Fresno", "Food processing", "311412",
                    ["reefer", "expedited"], "manufacturer", 260, 88_000_000, 140, 14,
                    "Coyote", "vendor_verified", 83, 21)
    longview = add("Longview Steel", "MO", "St. Joseph", "Steel fabrication", "332312",
                   ["flatbed"], "manufacturer", 240, 96_000_000, 130, 9,
                   "Landstar", "vendor_verified", 86, 17)
    meridian = add("Meridian Industries", "TX", "Fort Worth", "Automotive parts",
                   "336390", ["ltl", "ftl", "expedited"], "manufacturer", 9400,
                   4_200_000_000, 1800, 40, "C.H. Robinson, Uber Freight",
                   "public_filing", 90, 30)
    thistle = add("Thistle Distribution", "OK", "Tulsa", "Plumbing supply", "423720",
                  ["ltl", "ftl"], "distributor", 95, 28_000_000, 22, 3,
                  "a regional broker", "pattern", 38, None)

    # ---- the population ----------------------------------------------------
    used = {m["name"] for m in made}
    attempts = 0
    while len(made) < 205 and attempts < 4000:
        attempts += 1
        industry, naics, modes, shipper = RNG.choice(INDUSTRIES)
        mid = RNG.choice(MIDWORD)
        name = " ".join(p for p in (RNG.choice(PREFIX), mid,
                                    RNG.choice(SUFFIX[shipper])) if p)
        if name in used:
            continue
        used.add(name)
        state = RNG.choices(list(CITIES), weights=[5, 4, 3, 3, 2, 2, 2, 2, 2, 1, 1, 1, 1],
                            k=1)[0]
        city = RNG.choice(CITIES[state])
        employees = RNG.choice([45, 70, 110, 160, 230, 320, 480, 640, 900])
        revenue = employees * RNG.randint(240_000, 620_000)
        loads = max(4, int(employees * RNG.uniform(0.08, 0.45)))
        source = RNG.choices(
            ["vendor_verified", "vendor", "website", "public_filing", "pattern", "manual"],
            weights=[3, 4, 3, 1, 2, 1])[0]
        add(name, state, city, industry, naics, modes, shipper, employees, revenue,
            loads, max(2, loads // 9), RNG.choice(BROKERS),
            source, enrichment.SOURCE_TRUST.get(source, 55) + RNG.randint(-8, 8),
            RNG.choice([5, 12, 25, 40, 70, 110, 200, None]))

    _seed_contacts(conn, org_id, made, specials={
        "hero": hero, "cedar": cedar, "ironwood": ironwood, "halcyon": halcyon,
        "kestrel": kestrel, "sandpiper": sandpiper, "longview": longview,
        "meridian": meridian, "thistle": thistle})
    return made


def _domain(name):
    slug = "".join(c.lower() for c in name if c.isalnum() or c == " ")
    return slug.replace(" ", "")[:22] + ".example"


def _contact(conn, org_id, company_id, name, title, role_type, seniority,
             role_started=None, status="active", source="vendor", confidence=60,
             verified=None):
    return conn.execute(
        """INSERT INTO contacts (org_id, company_id, name, title, role_type, seniority,
                                 department, linkedin, role_started, status, source,
                                 confidence, last_verified, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (org_id, company_id, name, title, role_type, seniority, "Supply chain",
         f"linkedin.example/in/{name.lower().replace(' ', '-')}", role_started, status,
         source, confidence, verified, NOW)).lastrowid


def _point(conn, org_id, contact_id, kind, value, status, confidence, source,
           method="", verified_days=None, fail_count=0, primary=0):
    conn.execute(
        """INSERT INTO contact_points (org_id, contact_id, kind, value, status,
                                       confidence, source, method, last_verified,
                                       fail_count, is_primary, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (org_id, contact_id, kind, value, status, confidence, source, method,
         _ago(verified_days) if verified_days is not None else None,
         fail_count, primary, NOW))


_phone_counter = [1000]


def _number(area="816"):
    _phone_counter[0] += RNG.randint(1, 9)
    return f"+1{area}555{_phone_counter[0] % 10000:04d}"


AREA = {"MO": "816", "KS": "913", "AR": "479", "IL": "217", "TN": "901",
        "TX": "817", "OK": "918", "IA": "319", "NE": "402", "CA": "559",
        "WA": "509", "OH": "937", "GA": "478"}


def _email_for(name, company_name):
    first, _, last = name.partition(" ")
    return f"{first.lower()}.{last.lower().replace(' ', '')}@{_domain(company_name)}"


def _seed_contacts(conn, org_id, companies, specials):
    by_id = {c["id"]: c for c in companies}

    def make(company_id, name, title, role_type, seniority, **kw):
        return _contact(conn, org_id, company_id, name, title, role_type, seniority, **kw)

    # ---- the archetypes ----------------------------------------------------
    # The hero: new in role, verified direct dial checked nine days ago.
    c = make(specials["hero"], "Sarah Williams", "VP Supply Chain", "decision_maker",
             "vp", role_started=_ago(63), source="vendor_verified", confidence=92,
             verified=_ago(9))
    _point(conn, org_id, c, "direct_dial", _number("417"), "verified", 94,
           "vendor_verified", "provider — line active, name matched", 9, primary=1)
    _point(conn, org_id, c, "mobile", _number("417"), "probable", 61, "vendor",
           "provider — no name match", 40)
    _point(conn, org_id, c, "email", _email_for("Sarah Williams", "Vantage Aerospace"),
           "verified", 93, "vendor_verified", "provider — mailbox accepted", 9)

    # High fit, nothing to dial. The list must not hide this.
    c = make(specials["cedar"], "Hector Delgado", "Director of Logistics",
             "decision_maker", "director", role_started=_ago(900), source="website",
             confidence=68, verified=_ago(40))
    _point(conn, org_id, c, "email", _email_for("Hector Delgado", "Cedar Ridge Foods"),
           "guessed", 34, "pattern", "constructed from naming convention", None, primary=1)
    _point(conn, org_id, c, "switchboard", _number("785"), "probable", 55, "website",
           "listed on the company site", 40)

    # Gone four months. The record still says Director.
    c = make(specials["ironwood"], "Joyce Kirkland", "Director of Logistics",
             "decision_maker", "director", role_started=_ago(1600),
             status="left_company", source="vendor", confidence=44, verified=_ago(150))
    _point(conn, org_id, c, "direct_dial", _number("479"), "probable", 40, "vendor",
           "provider — line active, name not matched", 150, fail_count=1, primary=1)
    _point(conn, org_id, c, "email", _email_for("Joyce Kirkland", "Ironwood Building Products"),
           "invalid", 0, "vendor", "hard bounce — mailbox does not exist", 22, fail_count=3)

    # Verified, but the verification expired.
    c = make(specials["halcyon"], "Tom Byrne", "Transportation Manager",
             "decision_maker", "manager", role_started=_ago(1100),
             source="vendor_verified", confidence=80, verified=_ago(104))
    _point(conn, org_id, c, "direct_dial", _number("417"), "verified", 88,
           "vendor_verified", "provider — line active, name matched", 104, primary=1)
    _point(conn, org_id, c, "email", _email_for("Tom Byrne", "Halcyon Plastics"),
           "catch_all", 40, "vendor", "domain accepts all mail", 104)

    # Suppressed — the number is added to the suppression list in _seed_suppression.
    c = make(specials["kestrel"], "Angela Voss", "VP Supply Chain", "decision_maker",
             "vp", role_started=_ago(700), source="vendor", confidence=62,
             verified=_ago(60))
    _point(conn, org_id, c, "direct_dial", "+12175550188", "verified", 90, "vendor_verified",
           "provider — line active, name matched", 60, primary=1)

    # California. Blocked before 8am Pacific whatever the score says.
    c = make(specials["sandpiper"], "Carlos Mendez", "Director of Operations",
             "decision_maker", "director", role_started=_ago(110),
             source="vendor_verified", confidence=85, verified=_ago(21))
    _point(conn, org_id, c, "direct_dial", _number("559"), "verified", 91,
           "vendor_verified", "provider — line active, name matched", 21, primary=1)
    _point(conn, org_id, c, "email", _email_for("Carlos Mendez", "Sandpiper Produce"),
           "verified", 89, "vendor_verified", "provider — mailbox accepted", 21)

    # Flatbed-only. Real volume, wrong broker.
    c = make(specials["longview"], "Derrick Ackerman", "Plant Manager", "influencer",
             "manager", role_started=_ago(2900), source="vendor_verified",
             confidence=84, verified=_ago(17))
    _point(conn, org_id, c, "direct_dial", _number("816"), "verified", 90,
           "vendor_verified", "provider — line active, name matched", 17, primary=1)

    # Enterprise. Real freight, wrong motion.
    c = make(specials["meridian"], "Priya Rao", "VP Supply Chain", "decision_maker",
             "vp", role_started=_ago(1500), source="public_filing", confidence=88,
             verified=_ago(30))
    _point(conn, org_id, c, "switchboard", _number("817"), "verified", 85, "website",
           "listed on the company site", 30, primary=1)
    _point(conn, org_id, c, "email", _email_for("Priya Rao", "Meridian Industries"),
           "catch_all", 38, "pattern", "domain accepts all mail", 30)

    # Six attempts, zero connects.
    c = make(specials["thistle"], "Curtis Pruitt", "Logistics Manager",
             "decision_maker", "manager", role_started=_ago(2100), source="pattern",
             confidence=36)
    _point(conn, org_id, c, "direct_dial", _number("918"), "guessed", 28, "pattern",
           "constructed from a dialing pattern", None, fail_count=2, primary=1)
    _point(conn, org_id, c, "email", _email_for("Curtis Pruitt", "Thistle Distribution"),
           "guessed", 30, "pattern", "constructed from naming convention", None)

    # ---- the population ----------------------------------------------------
    special_ids = set(specials.values())
    for comp in companies:
        if comp["id"] in special_ids:
            continue
        area = AREA.get(comp["state"], "816")
        for _ in range(RNG.choices([1, 2, 3], weights=[5, 4, 2])[0]):
            title, role, seniority, _w = RNG.choices(
                TITLES, weights=[t[3] for t in TITLES])[0]
            name = f"{RNG.choice(FIRST)} {RNG.choice(LAST)}"
            tenure = RNG.choices([RNG.randint(25, 170), RNG.randint(200, 900),
                                  RNG.randint(1000, 3500)], weights=[2, 4, 3])[0]
            gone = RNG.random() < 0.07
            source = RNG.choices(["vendor_verified", "vendor", "website", "pattern"],
                                 weights=[3, 4, 2, 2])[0]
            verified_days = RNG.choice([6, 14, 30, 55, 95, 140, 240, None])
            cid = _contact(conn, org_id, comp["id"], name, title, role, seniority,
                           role_started=_ago(tenure),
                           status="left_company" if gone else "active",
                           source=source,
                           confidence=enrichment.SOURCE_TRUST.get(source, 55),
                           verified=_ago(verified_days) if verified_days else None)

            roll = RNG.random()
            if roll < 0.46:
                _point(conn, org_id, cid, "direct_dial", _number(area), "verified",
                       RNG.randint(82, 95), "vendor_verified",
                       "provider — line active, name matched",
                       verified_days if verified_days is not None else 60, primary=1)
            elif roll < 0.68:
                _point(conn, org_id, cid, "direct_dial", _number(area), "probable",
                       RNG.randint(48, 66), "vendor",
                       "provider — line active, name not matched",
                       verified_days, primary=1)
            elif roll < 0.84:
                _point(conn, org_id, cid, "direct_dial", _number(area), "guessed",
                       RNG.randint(22, 38), "pattern",
                       "constructed from a dialing pattern", None,
                       fail_count=RNG.choices([0, 1, 2, 3], weights=[6, 2, 1, 1])[0],
                       primary=1)
            else:
                _point(conn, org_id, cid, "switchboard", _number(area), "probable",
                       RNG.randint(50, 70), "website", "listed on the company site",
                       verified_days, primary=1)

            if RNG.random() < 0.35:
                _point(conn, org_id, cid, "mobile", _number(area),
                       RNG.choices(["verified", "probable"], weights=[2, 3])[0],
                       RNG.randint(55, 90), "vendor", "provider match", verified_days)

            estatus = RNG.choices(["verified", "guessed", "catch_all", "probable", "invalid"],
                                  weights=[5, 3, 2, 2, 1])[0]
            _point(conn, org_id, cid, "email", _email_for(name, comp["name"]), estatus,
                   {"verified": 90, "guessed": 32, "catch_all": 40, "probable": 55,
                    "invalid": 0}[estatus],
                   "vendor_verified" if estatus == "verified" else "pattern",
                   {"verified": "provider — mailbox accepted",
                    "guessed": "constructed from naming convention",
                    "catch_all": "domain accepts all mail",
                    "probable": "no definitive answer",
                    "invalid": "hard bounce — mailbox does not exist"}[estatus],
                   verified_days, fail_count=3 if estatus == "invalid" else 0)


# ---------------------------------------------------------------- triggers --

def _seed_triggers(conn, org_id, companies):
    by_name = {c["name"]: c["id"] for c in companies}

    def add(company_id, kind, headline, detail, days_ago, strength, source):
        conn.execute(
            """INSERT INTO triggers (org_id, company_id, kind, headline, detail,
                                     source_name, source_url, observed_at, strength,
                                     expires_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, company_id, kind, headline, detail, source,
             f"https://example.invalid/news/{company_id}-{kind}", _ago(days_ago),
             strength, _iso(TODAY + timedelta(days=180)), NOW))

    # The hero's stack: two strong, recent, mode-relevant signals.
    add(by_name["Vantage Aerospace"], "new_facility",
        "Announced a $42M Springfield production facility",
        "Groundbreaking reported in June; production expected to begin in Q1. A new "
        "site means outbound lanes with no incumbent coverage yet.", 11, 88,
        "Springfield Business Journal")
    add(by_name["Vantage Aerospace"], "leadership_change",
        "Sarah Williams named VP of Supply Chain",
        "Started nine weeks ago. New supply-chain leaders review incumbents inside "
        "two quarters and owe nothing to the previous vendor list.", 61, 82, "LinkedIn")
    add(by_name["Cedar Ridge Foods"], "contract_window",
        "Annual freight RFP opens October 1",
        "Procurement is collecting carrier bids for next year's lanes.", 6, 79,
        "Company newsroom")
    add(by_name["Sandpiper Produce"], "seasonal_peak",
        "Harvest peak begins in five weeks",
        "Volume above contracted reefer capacity, every year, on a schedule.", 4, 84,
        "Trade press")
    add(by_name["Longview Steel"], "expansion",
        "Adding a second shift at the St. Joseph mill",
        "A second shift is roughly a doubling of outbound flatbed volume.", 9, 86,
        "Local news")
    add(by_name["Halcyon Plastics"], "carrier_trouble",
        "Public complaint about missed pickups",
        "Named their broker in a post about a missed pickup on a customer order.",
        3, 74, "LinkedIn")
    add(by_name["Thistle Distribution"], "logistics_hire",
        "Hiring a Transportation Manager",
        "The seat that owns carrier selection is currently empty.", 20, 70, "Job board")
    add(by_name["Ironwood Building Products"], "new_facility",
        "Broke ground on a 180k sq ft distribution center",
        "Permit filing shows a 14-dock building — a real outbound operation.",
        30, 81, "Permit filing")

    # The population. Roughly half of all companies carry a live signal, which is
    # about what a real trigger feed produces; a list where everything is hot is a
    # list a rep stops reading.
    special = set(by_name[n] for n in
                  ["Vantage Aerospace", "Cedar Ridge Foods", "Sandpiper Produce",
                   "Longview Steel", "Halcyon Plastics", "Thistle Distribution",
                   "Ironwood Building Products"])
    for comp in companies:
        if comp["id"] in special:
            continue
        for _ in range(RNG.choices([0, 1, 2], weights=[9, 8, 3])[0]):
            kind = RNG.choice(list(TRIGGER_TEXT))
            headline, detail = RNG.choice(TRIGGER_TEXT[kind])
            fields = {
                "m": RNG.choice([8, 14, 22, 35, 60]), "city": comp["city"],
                "q": RNG.choice([2, 3]), "sqft": RNG.choice([60, 120, 180, 240]),
                "dock": RNG.choice([8, 12, 18, 24]), "n": RNG.randint(2, 6),
                "name": f"{RNG.choice(FIRST)} {RNG.choice(LAST)}",
                "month": RNG.choice(["October", "November", "January", "March"]),
                "year": TODAY.year + 1,
            }
            add(comp["id"], kind, headline.format(**fields), detail.format(**fields),
                RNG.choices([RNG.randint(1, 20), RNG.randint(21, 70),
                             RNG.randint(71, 200)], weights=[4, 4, 2])[0],
                RNG.randint(55, 92), RNG.choice(SOURCES))


# ----------------------------------------------------------------- targets --

def _seed_targets(conn, org_id, companies, rep_ids):
    """Assign every company's best contact to a rep, roughly by territory."""
    targets = []
    territory = {"MO": rep_ids[0], "KS": rep_ids[1], "AR": rep_ids[2],
                 "IL": rep_ids[3], "OK": rep_ids[4], "TX": rep_ids[5]}

    for comp in companies:
        contacts = conn.execute(
            """SELECT * FROM contacts WHERE org_id = ? AND company_id = ?
                ORDER BY CASE role_type WHEN 'decision_maker' THEN 0
                                        WHEN 'influencer' THEN 1 ELSE 2 END,
                         confidence DESC""", (org_id, comp["id"])).fetchall()
        if not contacts:
            continue
        rep_id = territory.get(comp["state"], RNG.choice(rep_ids))
        for contact in contacts[:2]:
            tid = conn.execute(
                """INSERT INTO targets (org_id, company_id, contact_id, rep_id, status,
                                        created_at)
                   VALUES (?,?,?,?,?,?)""",
                (org_id, comp["id"], contact["id"], rep_id, "new", NOW)).lastrowid
            targets.append({"id": tid, "company_id": comp["id"],
                            "contact_id": contact["id"], "rep_id": rep_id})
    return targets


def _seed_suppression(conn, org_id):
    import compliance
    compliance.suppress(conn, org_id, "phone", "+12175550188",
                        reason="asked to be removed on a call, 2026-07-14",
                        source="request", commit=False)
    compliance.suppress(conn, org_id, "phone", "+18165550101",
                        reason="national DNC registry match", source="dnc_registry",
                        commit=False)
    compliance.suppress(conn, org_id, "email", "legal@meridianindustries.example",
                        reason="written opt-out", source="request", commit=False)


# --------------------------------------------------------------- sequences --

SEQUENCES = [
    ("No-answer cadence", "Nobody picked up. Six touches over eleven business days, "
     "then the target goes back to the pool rather than being called forever.",
     "default", [
         (0, "call", "Second attempt, different hour",
          "Try a different part of the day. The same 10am three days running tests "
          "one hypothesis, not three."),
         (1, "email", "Vantage — one question about your outbound",
          "Two sentences. Name the signal, ask one question, no attachment."),
         (3, "call", "Third attempt — try the switchboard",
          "Ask for the department rather than the person. Different route, same target."),
         (5, "linkedin", "Connection request, no pitch",
          "No message beyond a connection request. A pitch in a connection request is "
          "a declined connection request."),
         (8, "email", "Closing the loop",
          "The permission-to-close email. It gets the highest reply rate in the "
          "cadence and most of those replies are 'no' — which is the point."),
         (11, "task", "Return to the pool",
          "Six touches, no answer. Release the target and let the scoring engine "
          "re-surface it when something changes."),
     ]),
    ("Post-conversation follow-through", "They talked but did not commit. Deliver "
     "what was promised, then two spaced touches before it goes quiet.",
     "", [
         (0, "email", "What we talked about",
          "Send exactly what was promised on the call. Do not re-pitch."),
         (3, "call", "Did it land?",
          "One question: did you get a chance to look at the lane?"),
         (10, "email", "The lane you mentioned",
          "Bring one new piece of information about their specific lane."),
         (21, "task", "Decide: nurture or release",
          "Three weeks of silence after a real conversation is an answer."),
     ]),
    ("Contract-window watch", "They are under contract. Stay visible without "
     "calling, and be first in when the bid opens.",
     "not_interested", [
         (0, "task", "Log the renewal month",
          "Write the month they named onto the record. It is the single most "
          "valuable thing they said."),
         (30, "email", "Something useful, not a pitch",
          "One piece of market information about their lane. No ask."),
         (75, "email", "Market check-in",
          "Rates on their corridor, nothing else."),
         (120, "call", "Ahead of the bid window",
          "Call four to six weeks before the renewal month they named."),
     ]),
]


def _seed_sequences(conn, org_id):
    ids = []
    for name, desc, entry, steps in SEQUENCES:
        sid = conn.execute(
            """INSERT INTO sequences (org_id, name, description, entry_trigger,
                                      active, created_at)
               VALUES (?,?,?,?,?,?)""",
            (org_id, name, desc, entry, 1, NOW)).lastrowid
        for i, (offset, channel, title, guidance) in enumerate(steps, start=1):
            conn.execute(
                """INSERT INTO sequence_steps (org_id, sequence_id, step_no, day_offset,
                                               channel, title, guidance)
                   VALUES (?,?,?,?,?,?,?)""",
                (org_id, sid, i, offset, channel, title, guidance))
        ids.append(sid)
    return ids


# --------------------------------------------------------- simulated history --

def _simulate_history(conn, org_id, rep_ids, days=60, quiet=False):
    """
    Run sixty business days of calls through the real simulator.

    The point is that /intelligence shows lift numbers nobody typed in. Targets
    are picked the way a rep would pick them — highest score first — so the
    resulting sample carries exactly the selection bias a real deployment has,
    which is why `learned_weights` clamps how far evidence may move a weight.
    """
    org = conn.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
    provider = telephony.SimulatedProvider()
    made = 0

    rep_names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM reps WHERE org_id = ?", (org_id,)).fetchall()}

    now_hour = datetime.now().hour

    for back in range(days, -1, -1):
        day = TODAY - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        partial = back == 0
        scored = signal_engine.load_and_score(
            conn, org, statuses=("new", "working"), today=day)
        scored = [s for s in scored if not s["blockers"] and s["points"]]
        if not scored:
            break
        # Reps work the top of their own list, with some slop — nobody dials in
        # perfect score order.
        for rep_id in rep_ids:
            mine = [s for s in scored if s["target"]["rep_id"] == rep_id][:40]
            if not mine:
                continue
            pool = mine[:min(len(mine), 34)]
            if partial:
                # Today is only as far through as the clock says.
                fraction = max(0.0, min((now_hour - 8) / 9.0, 1.0))
                k = int(RNG.randint(9, 18) * fraction)
            else:
                k = RNG.randint(9, 18)
            k = min(len(pool), k)
            if k <= 0:
                continue
            picks = RNG.sample(pool, k)
            for row in picks:
                made += _place_simulated_call(conn, org, provider, row, rep_id, day,
                                              rep_names.get(rep_id, ""))
        conn.commit()

    if not quiet:
        print(f"  simulated {made} calls over {days} days")
    return made


def _place_simulated_call(conn, org, provider, row, rep_id, day, rep_name=""):
    target, company, contact = row["target"], row["company"], row["contact"]
    points = [p for p in row["points"] if p["kind"] in ("direct_dial", "mobile", "switchboard")]
    if not points:
        return 0
    point = max(points, key=lambda p: p["confidence"])

    hours = [8, 9, 10, 11, 13, 14, 15, 16, 17]
    weights = [2, 5, 6, 4, 3, 4, 5, 5, 2]
    if day == TODAY:
        # Never log a call from later this afternoon.
        cutoff = datetime.now().hour
        allowed = [(h, w) for h, w in zip(hours, weights) if h <= cutoff]
        if allowed:
            hours, weights = [h for h, _ in allowed], [w for _, w in allowed]
    hour = RNG.choices(hours, weights=weights)[0]
    started = datetime.combine(day, datetime.min.time()).replace(
        hour=hour, minute=RNG.randint(0, 59))

    context = {
        "company_name": company["name"], "contact_name": contact["name"] if contact else "",
        "rep_name": rep_name, "org_name": org["name"],
        "loads_per_month": company["loads_per_month"], "modes": company["modes"],
        "incumbents": company["incumbents"], "lanes": company["lanes"],
        "city": company["city"], "state": company["state"],
        "shipper_type": company["shipper_type"],
        "point_status": enrichment.effective_status(point, day),
        "point_kind": point["kind"],
        "role_type": contact["role_type"] if contact else "unknown",
        "local_hour": hour, "score": row["score"],
        "why_now": bool(row["why_now"]),
        "why_now_headline": row["why_now"]["detail"].split(" — ")[0] if row["why_now"] else "",
        "prior_connects": target["connects"] or 0,
    }
    result = provider.simulate(call_ref=f"{target['id']}-{day}", context=context)

    ended = started + timedelta(seconds=result["duration_sec"])
    conn.execute(
        """INSERT INTO calls (org_id, target_id, rep_id, contact_id, direction,
                              to_number, provider, provider_call_id, status,
                              disposition, started_at, ended_at, duration_sec,
                              recorded, transcript, signals_at_call, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (org["id"], target["id"], rep_id, target["contact_id"], "outbound",
         point["value"], "simulated", f"sim-{target['id']}-{day}", result["status"],
         result["disposition"], started.isoformat(timespec="seconds"),
         ended.isoformat(timespec="seconds"), result["duration_sec"], 0,
         result["transcript"], json.dumps(row["keys"]), started.isoformat(timespec="seconds")))

    connected = result["disposition"] in ("connected", "meeting_set", "callback",
                                          "not_interested")
    conn.execute(
        """UPDATE targets SET attempts = attempts + 1, connects = connects + ?,
                  last_touch_at = ?, last_connect_at = ?,
                  status = CASE WHEN ? = 'meeting_set' THEN 'meeting_set'
                                WHEN status = 'new' THEN 'working' ELSE status END
            WHERE id = ?""",
        (1 if connected else 0, _iso(day),
         _iso(day) if connected else target["last_connect_at"],
         result["disposition"], target["id"]))

    # The timeline only carries the recent past. Writing an activity row for all
    # sixty days would be honest and unreadable — a record with forty entries
    # buries the three that matter.
    if (TODAY - day).days <= 30:
        conn.execute(
            """INSERT INTO activities (org_id, target_id, rep_id, kind, summary,
                                       detail, actor, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (org["id"], target["id"], rep_id, "call",
             f"Call — {result['disposition'].replace('_', ' ')}"
             + (f" ({result['duration_sec']}s)" if result["duration_sec"] else ""),
             "", "rep", started.isoformat(timespec="seconds")))

    signal_engine.record_outcome(conn, org["id"], row["keys"], result["disposition"],
                                 commit=False)
    enrichment.record_dial_result(conn, org["id"], point["value"],
                                  result["disposition"], today=day, commit=False)
    return 1


# ----------------------------------------------------------------- pipeline --

def _seed_pipeline(conn, org_id, rep_ids, targets):
    """Turn the meetings the simulation produced into a real pipeline."""
    # Vantage Aerospace is held back deliberately. It is the record the
    # walkthrough opens on, and a target that has already become an opportunity
    # is correctly filtered off the daily call list — so the seed leaves it in
    # 'working' rather than making the list bend around one demo record.
    meetings = conn.execute(
        """SELECT t.*, c.name AS company_name, c.loads_per_month, c.modes
             FROM targets t JOIN companies c ON c.id = t.company_id
            WHERE t.org_id = ? AND t.status = 'meeting_set'
              AND c.name != 'Vantage Aerospace'""", (org_id,)).fetchall()

    stages = ["discovery", "discovery", "pricing", "pricing", "pilot", "contract",
              "won", "lost"]
    for i, t in enumerate(meetings):
        stage = stages[i % len(stages)]
        loads = t["loads_per_month"] or 20
        modes = [m.strip() for m in (t["modes"] or "ftl").split(",") if m.strip()]
        margin = sum({"ltl": 55, "ftl": 260, "flatbed": 330, "reefer": 340,
                      "expedited": 520, "intermodal": 210}.get(m, 250)
                     for m in modes) / max(len(modes), 1)
        value = int(loads * margin * 12 * RNG.uniform(0.12, 0.35))

        signals = conn.execute(
            "SELECT signals_at_call FROM calls WHERE org_id = ? AND target_id = ? "
            "AND disposition = 'meeting_set' ORDER BY id DESC LIMIT 1",
            (org_id, t["id"])).fetchone()
        origin = ""
        if signals and signals["signals_at_call"]:
            keys = json.loads(signals["signals_at_call"])
            origin = keys[0] if keys else ""

        opened = TODAY - timedelta(days=RNG.randint(5, 55))
        close = opened + timedelta(days=RNG.randint(30, 110))
        deal_id = conn.execute(
            """INSERT INTO deals (org_id, company_id, target_id, rep_id, name, stage,
                                  value_annual, loads_per_month, close_date,
                                  close_date_original, next_step, origin_signal,
                                  created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, t["company_id"], t["id"], t["rep_id"],
             f"{t['company_name']} — {modes[0].upper()} lanes", stage, value,
             int(loads * RNG.uniform(0.15, 0.4)), _iso(close), _iso(close),
             RNG.choice(["Send capability sheet", "Pricing review with procurement",
                         "Pilot lane award", "Contract redlines back from legal",
                         "Confirm insurance certificate"]),
             origin, _iso(opened), NOW)).lastrowid

        conn.execute("UPDATE targets SET status = ? WHERE id = ?",
                     ({"won": "won", "lost": "lost"}.get(stage, "opportunity"), t["id"]))
        if origin:
            signal_engine.record_deal_outcome(
                conn, org_id, origin, "won" if stage == "won" else "open",
                dollars=value if stage == "won" else 0, commit=False)


def _seed_tasks(conn, org_id, rep_ids, targets, seq_ids):
    """A believable amount of open work, including some that is overdue."""
    import sequences

    working = conn.execute(
        """SELECT * FROM targets WHERE org_id = ? AND status IN ('working', 'opportunity')
            ORDER BY score DESC LIMIT 60""", (org_id,)).fetchall()

    for t in working[:26]:
        due = TODAY + timedelta(days=RNG.choice([-4, -2, -1, 0, 0, 1, 2, 3, 5]))
        kind = RNG.choice(["call", "email", "follow_up", "meeting"])
        conn.execute(
            """INSERT INTO tasks (org_id, target_id, rep_id, kind, title, detail,
                                  due_at, status, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (org_id, t["id"], t["rep_id"], kind,
             RNG.choice(["Send the capability sheet", "Call back as agreed",
                         "Confirm the meeting in writing",
                         "Send MC and insurance certificate",
                         "Follow up on the lane quote"]),
             "", _iso(due), "open", "rep", NOW))

    # Enrol every target whose last call went unanswered, exactly as the live
    # path would. Then run the cadence so some steps have already materialized.
    org = conn.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
    unanswered = conn.execute(
        """SELECT t.id, k.disposition FROM targets t
             JOIN calls k ON k.id = (SELECT MAX(id) FROM calls WHERE target_id = t.id)
            WHERE t.org_id = ? AND k.disposition IN ('no_answer', 'voicemail', 'gatekeeper')
            LIMIT 45""", (org_id,)).fetchall()
    earliest = 12
    for row in unanswered:
        sequences.on_disposition(conn, org, row["id"], row["disposition"],
                                 today=TODAY - timedelta(days=RNG.randint(1, earliest)),
                                 commit=False)
    conn.commit()

    # Walk the cadence forward one day at a time rather than calling run() once
    # for today. run() materializes a single step per enrollment, by design — a
    # cadence that fired four steps at once because nobody ran the cron on
    # Friday is the bug that behavior prevents. Replaying the days is what gives
    # the demo a realistic spread of cadences at different steps.
    for back in range(earliest, -1, -1):
        sequences.run(conn, org_id, today=TODAY - timedelta(days=back))


def _rescore(conn, org_id):
    org = conn.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
    scored = signal_engine.load_and_score(conn, org)
    signal_engine.persist_scores(conn, scored, commit=False)


def _seed_second_tenant(conn):
    """
    A second org with one company, one contact, one target.

    It exists so the smoke test can assert that tenant A cannot read tenant B's
    rows by id. Multi-tenancy that is never tested across a real second tenant
    is multi-tenancy nobody has checked.
    """
    import auth
    org_id = conn.execute(
        """INSERT INTO orgs (name, slug, plan, status, pitch, modes, home_markets,
                             recording_policy, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("Northgate Freight", "northgate", "trial", "active",
         "Flatbed and heavy haul.", "flatbed,ftl", "TX", "off", NOW)).lastrowid
    rep_id = conn.execute(
        """INSERT INTO reps (org_id, name, email, territory, quota_quarter, status,
                             created_at) VALUES (?,?,?,?,?,?,?)""",
        (org_id, "Other Tenant Rep", "rep@northgate.example", "TX", 100_000,
         "active", NOW)).lastrowid
    conn.execute(
        """INSERT INTO users (org_id, email, password_hash, name, role, status,
                              rep_id, created_at) VALUES (?,?,?,?,?,?,?,?)""",
        (org_id, "owner@northgate.example", auth.hash_password(DEMO_PASSWORD),
         "Northgate Owner", "owner", "active", rep_id, NOW))
    company_id = _company_row(
        conn, org_id, name="Other Tenant Steel", domain="othertenant.example",
        industry="Steel fabrication", city="Waco", state="TX",
        timezone="America/Chicago", employees=80, revenue_est=22_000_000,
        modes="flatbed", loads_per_month=30, shipper_type="manufacturer",
        source="manual", confidence=60, external_id="ng-00001")
    contact_id = _contact(conn, org_id, company_id, "Other Tenant Contact",
                          "Logistics Manager", "decision_maker", "manager",
                          role_started=_ago(400))
    _point(conn, org_id, contact_id, "direct_dial", "+12545550199", "verified", 90,
           "manual", "manual entry", 10, primary=1)
    conn.execute(
        """INSERT INTO targets (org_id, company_id, contact_id, rep_id, status, created_at)
           VALUES (?,?,?,?,?,?)""",
        (org_id, company_id, contact_id, rep_id, "new", NOW))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    empty = "--empty" in sys.argv
    print("Building schema...")
    create_tables()
    if empty:
        print(f"Done. Empty database at {DB_FILE}. Create the first tenant at /signup.")
        return

    conn = get_connection()
    print("Seeding the demo brokerage...")
    org_id, calls = seed(conn)

    counts = {}
    for table in ("companies", "contacts", "contact_points", "triggers", "targets",
                  "calls", "deals", "tasks", "enrollments", "signal_outcomes"):
        counts[table] = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE org_id = ?", (org_id,)
        ).fetchone()["n"]
    top = conn.execute(
        """SELECT c.name, t.score FROM targets t JOIN companies c ON c.id = t.company_id
            WHERE t.org_id = ? ORDER BY t.score DESC LIMIT 3""", (org_id,)).fetchall()
    conn.close()

    print(f"\nDone. Database at {DB_FILE}")
    print("  " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    print("  top of the list: " + ", ".join(f"{r['name']} ({r['score']})" for r in top))
    print(f"""
Sign in at http://localhost:5005 with any of:

    owner@royalgrind.example     owner    — everything, including telephony
    manager@royalgrind.example   manager  — the team's lists and coaching
    rep@royalgrind.example       member   — one rep's own list

Password for all three: {DEMO_PASSWORD}

Start on Today, open Vantage Aerospace, and press Call.""")


if __name__ == "__main__":
    main()
