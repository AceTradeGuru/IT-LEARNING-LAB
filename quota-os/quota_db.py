"""
QuotaOS — database build and demo seed.

Rebuild the database from scratch:

    python3 quota_db.py

The seed is a sales org with a story on every seat. Each rep is an archetype that
exercises a different path through the health engine, because a demo where
everyone is fine proves nothing:

    Alicia Trent      crushing it — the control
    Ben Okafor        boring and on track — the other control
    Sana Malik        45 days into a 120-day ramp — must NOT read as at-risk
    Greg Nowak        good last year, pipeline gone this year, one whale deal left
    Dev Patel         5x coverage, none of it touched in a month — happy ears
    Rachel Kim        on track, clean hygiene
    Omar Haddad       selling well with a lapsed mandatory certification
    Lena Fischer      decent pipeline, half the team's activity
    Chris Boone       SMB volume machine, quota already met
    Maya Lindqvist    behind, and no next step on most of the pipeline
    Tyler Grant       eight months in, fully ramped, zero open pipeline
    Nadia Rahman      steady, but never started a mandatory certification

Greg and Dev are the two that matter most. Both have a green-looking number
somewhere on their record, and both are in trouble for reasons a quota-attainment
column cannot show you.

Everything is dated relative to today, so the demo never goes stale.
"""

import json
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from ai_engine import _offline_interview, _offline_score

DB_FILE = Path(__file__).parent / "quotaos.db"
SCHEMA_FILE = Path(__file__).parent / "schema.sql"

TODAY = date.today()
RNG = random.Random(1104)   # fixed seed: the demo looks the same every rebuild


def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables():
    conn = get_connection()
    conn.executescript(SCHEMA_FILE.read_text())
    conn.commit()
    conn.close()


def _iso(d):
    return d.isoformat()


def _quarter_bounds(today):
    q = (today.month - 1) // 3
    start = date(today.year, q * 3 + 1, 1)
    end = date(today.year + (1 if q == 3 else 0), (q * 3 + 4) if q < 3 else 1, 1) - timedelta(days=1)
    return start, end


Q_START, Q_END = _quarter_bounds(TODAY)

# ---------------------------------------------------------------- accounts --

_PREFIX = ["Northwind", "Cedar Ridge", "Halcyon", "Brightline", "Meridian", "Fairhaven",
           "Ironwood", "Blue Harbor", "Sandpiper", "Kestrel", "Longview", "Talon",
           "Orchard", "Vantage", "Copperfield", "Rosewood", "Grayson", "Pinnacle",
           "Foxglove", "Wexford", "Ashfield", "Marlowe", "Dunmore", "Salt Creek",
           "Brambleton", "Highgate", "Cranbrook", "Elderfield", "Norwood", "Thistle"]
_SUFFIX = {
    "Enterprise": ["Industries", "Health Systems", "Financial Group", "Logistics",
                   "Manufacturing", "Energy", "Capital Partners", "Bancorp"],
    "Mid-Market": ["Software", "Analytics", "Distribution", "Medical", "Retail Group",
                   "Networks", "Labs", "Systems"],
    "SMB": ["Design Co", "Consulting", "Studio", "Services", "Supply", "Partners",
            "Agency", "Works"],
}
_used_accounts = set()


def _account(segment):
    for _ in range(200):
        name = f"{RNG.choice(_PREFIX)} {RNG.choice(_SUFFIX[segment])}"
        if name not in _used_accounts:
            _used_accounts.add(name)
            return name
    return f"Account {RNG.randint(1000, 9999)}"


_DEAL_BAND = {
    "Enterprise": (85_000, 340_000),
    "Mid-Market": (24_000, 95_000),
    "SMB": (5_500, 24_000),
}

_NEXT_STEPS = [
    "Security review call Thursday",
    "Redlines back from legal",
    "Exec sponsor intro scheduled",
    "Pricing approval pending",
    "Technical validation in progress",
    "Procurement kickoff booked",
    "Reference call with existing customer",
    "Mutual close plan review",
]

_SOURCES = ["Outbound", "Inbound", "Partner", "Expansion", "Event", "Referral"]


# --------------------------------------------------------------------- org --

# Each rep spec drives deal generation. The knobs are the ones the health engine
# reads, so an archetype description and the signals it produces stay in sync.
REPS = [
    # (name, team, segment, region, quota, months_tenure, ramp_days, spec)
    ("Alicia Trent", "Enterprise West", "Enterprise", "West", 1_600_000, 41, 0, dict(
        attain=1.10, coverage=4.3, stale=0.05, nostep=0.05, slip=0.10, activity=1.35,
        note="Two years running as the top enterprise seat. Closes early in the quarter.")),
    ("Ben Okafor", "Enterprise West", "Enterprise", "West", 1_600_000, 27, 0, dict(
        attain=0.44, coverage=3.5, stale=0.12, nostep=0.10, slip=0.15, activity=1.05,
        note="Metronomic. Never the top rep, never a surprise.")),
    ("Sana Malik", "Enterprise West", "Enterprise", "West", 1_600_000, 1, 120, dict(
        attain=0.04, coverage=2.4, stale=0.10, nostep=0.15, slip=0.05, activity=0.95,
        note="45 days in. Building a territory map before she builds pipeline.")),
    ("Greg Nowak", "Enterprise West", "Enterprise", "West", 1_600_000, 63, 0, dict(
        attain=0.11, coverage=1.15, stale=0.30, nostep=0.20, slip=0.35, activity=0.75,
        whale=True,
        note="President's Club last year. This year the pipeline never got rebuilt.")),

    ("Dev Patel", "Mid-Market East", "Mid-Market", "East", 900_000, 19, 0, dict(
        attain=0.19, coverage=5.6, stale=0.65, nostep=0.35, slip=0.55, activity=0.85,
        note="Biggest pipeline on the team. Almost none of it has been touched in a month.")),
    ("Rachel Kim", "Mid-Market East", "Mid-Market", "East", 900_000, 33, 0, dict(
        attain=0.41, coverage=3.2, stale=0.10, nostep=0.08, slip=0.12, activity=1.10,
        note="Clean forecast. Her commit number has been within 5% for six quarters.")),
    ("Omar Haddad", "Mid-Market East", "Mid-Market", "East", 900_000, 22, 0, dict(
        attain=0.52, coverage=3.4, stale=0.10, nostep=0.10, slip=0.10, activity=1.15,
        note="Selling well — and his mandatory security certification lapsed last month.")),
    ("Lena Fischer", "Mid-Market East", "Mid-Market", "East", 900_000, 14, 0, dict(
        attain=0.24, coverage=3.1, stale=0.20, nostep=0.15, slip=0.20, activity=0.40,
        note="Pipeline looks adequate. She is running less than half the team's meetings.")),

    ("Chris Boone", "SMB Velocity", "SMB", "Central", 520_000, 36, 0, dict(
        attain=1.02, coverage=2.8, stale=0.08, nostep=0.05, slip=0.08, activity=1.30,
        note="Highest volume in the org. Already through the quarterly number.")),
    ("Maya Lindqvist", "SMB Velocity", "SMB", "Central", 520_000, 11, 0, dict(
        attain=0.23, coverage=3.0, stale=0.25, nostep=0.55, slip=0.25, activity=0.95,
        note="Works hard. Her CRM has no next step on most of the pipeline.")),
    ("Tyler Grant", "SMB Velocity", "SMB", "Central", 520_000, 8, 0, dict(
        attain=0.11, coverage=0.0, stale=0.0, nostep=0.0, slip=0.0, activity=0.55,
        empty=True,
        note="Ramped out four months ago. Nothing open in the pipeline at all.")),
    ("Nadia Rahman", "SMB Velocity", "SMB", "Central", 520_000, 17, 0, dict(
        attain=0.38, coverage=3.3, stale=0.12, nostep=0.10, slip=0.15, activity=1.00,
        note="Steady producer who never started the mandatory contracting certification.")),
]

MANAGERS = [
    ("Marcus Bell", "Enterprise West", "Enterprise", "West"),
    ("Priya Raghunathan", "Mid-Market East", "Mid-Market", "East"),
    ("Tomás Herrera", "SMB Velocity", "SMB", "Central"),
]


def _email(name):
    first, last = name.lower().split(" ", 1)
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        first, last = first.replace(a, b), last.replace(a, b)
    return f"{first[0]}{last.replace(' ', '')}@quotaos.example"


def seed_org(conn):
    """Insert the org chart, then generate deals and activity for each rep."""
    vp_id = conn.execute(
        """INSERT INTO reps (name, email, title, role, manager_id, team, region, segment,
                             hire_date, ramp_end_date, quota_annual, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("Dana Whitfield", "dwhitfield@quotaos.example", "VP of Sales", "vp", None,
         "Global", "All", "All", _iso(TODAY - timedelta(days=1500)), None, 0,
         "Runs the whole org. Opens this dashboard first thing every Monday."),
    ).lastrowid

    manager_ids = {}
    for name, team, segment, region in MANAGERS:
        manager_ids[team] = conn.execute(
            """INSERT INTO reps (name, email, title, role, manager_id, team, region, segment,
                                 hire_date, ramp_end_date, quota_annual, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, _email(name), f"Sales Manager, {team}", "manager", vp_id, team,
             region, segment, _iso(TODAY - timedelta(days=RNG.randint(700, 1400))),
             None, 0, f"Carries the {team} number through their team."),
        ).lastrowid

    rep_ids = {}
    for name, team, segment, region, quota, tenure_m, ramp_days, spec in REPS:
        hire = TODAY - timedelta(days=round(tenure_m * 30.4))
        ramp_end = _iso(hire + timedelta(days=ramp_days)) if ramp_days else None
        rid = conn.execute(
            """INSERT INTO reps (name, email, title, role, manager_id, team, region, segment,
                                 hire_date, ramp_end_date, quota_annual, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, _email(name), f"Account Executive, {segment}", "rep",
             manager_ids[team], team, region, segment, _iso(hire), ramp_end,
             quota, spec["note"]),
        ).lastrowid
        rep_ids[name] = rid
        _seed_deals(conn, rid, segment, quota, spec)
        _seed_activity(conn, rid, spec)

    conn.commit()
    return vp_id, manager_ids, rep_ids


def _seed_deals(conn, rep_id, segment, quota_annual, spec):
    lo, hi = _DEAL_BAND[segment]
    quota_q = quota_annual / 4

    def add(account, amount, stage, created, close, close_orig, last_act, next_step):
        prob = {"discovery": 15, "demo": 30, "proposal": 55, "negotiation": 75,
                "closed_won": 100, "closed_lost": 0}[stage]
        conn.execute(
            """INSERT INTO deals (rep_id, account, amount, stage, probability, created_date,
                                  close_date, close_date_original, last_activity_date,
                                  next_step, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (rep_id, account, amount, stage, prob, _iso(created), _iso(close),
             _iso(close_orig), _iso(last_act) if last_act else None, next_step,
             RNG.choice(_SOURCES)),
        )

    # --- closed/won this quarter, up to the attainment target -----------------
    # The last deal is allowed to be smaller than the segment's usual band so the
    # total lands on the target. Without that clamp a single enterprise deal can
    # overshoot a modest attainment target several times over.
    target = quota_q * spec["attain"]
    booked = 0
    while booked < target * 0.9:
        left = target - booked
        amt = RNG.randint(lo, hi)
        if amt > left:
            amt = max(int(left), int(lo * 0.35))
        close = Q_START + timedelta(days=RNG.randint(0, max((TODAY - Q_START).days, 1)))
        created = close - timedelta(days=RNG.randint(35, 150))
        add(_account(segment), amt, "closed_won", created, close, close, close, None)
        booked += amt

    # --- a couple of prior-quarter wins, so history isn't empty ---------------
    for _ in range(RNG.randint(2, 4)):
        close = Q_START - timedelta(days=RNG.randint(5, 88))
        add(_account(segment), RNG.randint(lo, hi), "closed_won",
            close - timedelta(days=RNG.randint(40, 140)), close, close, close, None)

    # --- losses ---------------------------------------------------------------
    for _ in range(RNG.randint(1, 3)):
        close = TODAY - timedelta(days=RNG.randint(5, 70))
        add(_account(segment), RNG.randint(lo, hi), "closed_lost",
            close - timedelta(days=RNG.randint(40, 130)), close, close, close, None)

    if spec.get("empty"):
        return

    # --- open pipeline, sized to the coverage target --------------------------
    remaining = max(quota_q - booked, quota_q * 0.1)
    pipeline_target = remaining * spec["coverage"]
    built = 0
    open_deals = []
    while built < pipeline_target * 0.9:
        left = pipeline_target - built
        amt = RNG.randint(lo, hi)
        if amt > left:                      # same last-deal clamp as the wins
            amt = max(int(left), int(lo * 0.35))
        open_deals.append(amt)
        built += amt
        if len(open_deals) > 40:
            break

    # Greg's quarter comes down to a single deal — the concentration signal.
    if spec.get("whale") and open_deals:
        open_deals[0] = int(remaining * 0.85)

    for i, amt in enumerate(open_deals):
        # Most of the pipeline lands this quarter; some slides to next.
        if RNG.random() < 0.88:
            close = TODAY + timedelta(days=RNG.randint(3, max((Q_END - TODAY).days, 5)))
        else:
            close = Q_END + timedelta(days=RNG.randint(5, 75))
        created = TODAY - timedelta(days=RNG.randint(20, 160))

        # Later stages sit closer to their close date.
        days_out = (close - TODAY).days
        if days_out < 15:
            stage = RNG.choice(["negotiation", "proposal", "proposal"])
        elif days_out < 40:
            stage = RNG.choice(["proposal", "demo", "negotiation"])
        else:
            stage = RNG.choice(["discovery", "discovery", "demo"])

        stale = RNG.random() < spec["stale"]
        last_act = TODAY - timedelta(days=RNG.randint(16, 60) if stale else RNG.randint(0, 12))
        if last_act < created:
            last_act = created

        next_step = "" if RNG.random() < spec["nostep"] else RNG.choice(_NEXT_STEPS)

        close_orig = close
        if RNG.random() < spec["slip"]:
            close_orig = close - timedelta(days=RNG.choice([14, 21, 30, 45, 60]))
            if close_orig < created:
                close_orig = created + timedelta(days=1)

        add(_account(segment), amt, stage, created, close, close_orig, last_act, next_step)


def _seed_activity(conn, rep_id, spec):
    """Eight weeks of weekly activity, scaled by the rep's activity multiplier."""
    level = spec["activity"]
    monday = TODAY - timedelta(days=TODAY.weekday())
    for w in range(8):
        week = monday - timedelta(weeks=w)
        jitter = RNG.uniform(0.8, 1.2)
        conn.execute(
            """INSERT INTO activities (rep_id, week_start, calls, emails, meetings, demos)
               VALUES (?,?,?,?,?,?)""",
            (rep_id, _iso(week),
             round(48 * level * jitter), round(120 * level * jitter),
             round(9 * level * jitter), round(4 * level * jitter)),
        )


# ---------------------------------------------------------------- training --

COURSES = [
    # code, title, category, hours, summary, fixes_signal
    ("PROD-101", "Platform Fundamentals", "Product", 4.0,
     "The product, the architecture, and the six questions buyers always ask about it.", None),
    ("PROD-201", "Advanced Integrations & Architecture", "Product", 6.0,
     "How the platform lands in a customer's existing stack. Required for enterprise cycles.", None),
    ("METH-110", "Discovery That Earns the Next Meeting", "Method", 3.0,
     "Running discovery that surfaces a business problem instead of a feature checklist.",
     "stale_deals"),
    ("METH-120", "Pipeline Generation Sprint", "Method", 5.0,
     "Two weeks of structured outbound: list build, sequencing, and a daily block that holds.",
     "thin_pipeline"),
    ("METH-130", "MEDDPICC Qualification", "Method", 4.0,
     "Using qualification to find the gap in a deal rather than to fill in a form after the fact.",
     "slippage"),
    ("METH-140", "Forecast Discipline & Deal Hygiene", "Method", 3.0,
     "Next steps, close dates you'd defend, and a commit number that means something.",
     "no_next_step"),
    ("METH-150", "Multi-threading the Enterprise Account", "Method", 4.0,
     "Getting to three contacts before you need them, so one gone-dark champion isn't fatal.",
     "concentration"),
    ("METH-160", "Territory Planning & Activity Blocking", "Method", 2.5,
     "Segmenting a patch and defending the calendar blocks that keep it fed.",
     "low_activity"),
    ("METH-170", "Closing Plans for a Behind-Pace Quarter", "Method", 3.0,
     "Triage: which deals can actually land this quarter, and what to stop working today.",
     "behind_pace"),
    ("COMP-100", "Security & Data Handling", "Compliance", 2.0,
     "Customer data, retention, and what you may and may not put in writing. Annual renewal.",
     "cert_expired"),
    ("COMP-110", "Contracting, MSAs & Procurement", "Compliance", 2.0,
     "Standard terms, what needs legal, and how to move through procurement without stalling.",
     "cert_missing"),
    ("COMP-120", "Annual Compliance Refresh", "Compliance", 1.5,
     "The short annual refresh that keeps a certification from lapsing.", "cert_expiring"),
    ("IND-210", "Selling into Regulated Industries", "Industry", 3.0,
     "Healthcare and financial services: procurement patterns, review boards, and timelines.", None),
    ("IND-220", "Competitive Landscape", "Industry", 2.0,
     "The four vendors you'll run into and where each of them actually wins.", None),
]

TRACKS = [
    # name, summary, required_for, valid_months, mandatory, course codes
    ("Core Seller Certification",
     "The baseline every rep certifies on before carrying a full territory.",
     "rep", 12, 1, ["PROD-101", "METH-110", "METH-130"]),
    ("Security & Data Handling",
     "Mandatory annual certification. Lapses are a compliance exposure, not a training gap.",
     "all", 12, 1, ["COMP-100", "COMP-120"]),
    ("Contract & Procurement Certification",
     "Required before a rep can send paper without deal-desk review.",
     "rep", 24, 1, ["COMP-110"]),
    ("Enterprise Field Certification",
     "Optional depth track for reps carrying enterprise accounts.",
     "Enterprise", 24, 0, ["PROD-201", "METH-150", "IND-210"]),
]


def seed_training(conn, rep_ids):
    course_ids = {}
    for code, title, cat, hours, summary, fixes in COURSES:
        course_ids[code] = conn.execute(
            "INSERT INTO courses (code, title, category, hours, summary, fixes_signal) "
            "VALUES (?,?,?,?,?,?)",
            (code, title, cat, hours, summary, fixes),
        ).lastrowid

    track_ids = {}
    for name, summary, req_for, months, mandatory, codes in TRACKS:
        tid = conn.execute(
            "INSERT INTO cert_tracks (name, summary, required_for, valid_months, is_mandatory) "
            "VALUES (?,?,?,?,?)",
            (name, summary, req_for, months, mandatory),
        ).lastrowid
        track_ids[name] = tid
        for i, code in enumerate(codes):
            conn.execute(
                "INSERT INTO track_courses (track_id, course_id, sequence) VALUES (?,?,?)",
                (tid, course_ids[code], i + 1),
            )

    # --- certifications, with the exceptions that drive the compliance signals -
    for name, rid in rep_ids.items():
        rep = conn.execute("SELECT * FROM reps WHERE id = ?", (rid,)).fetchone()
        tenure = (TODAY - datetime.strptime(rep["hire_date"], "%Y-%m-%d").date()).days

        for track_name, tid in track_ids.items():
            track = conn.execute("SELECT * FROM cert_tracks WHERE id = ?", (tid,)).fetchone()

            # Nadia never started contracting — this is the cert_missing signal.
            if name == "Nadia Rahman" and track_name == "Contract & Procurement Certification":
                continue
            # The optional enterprise track only applies to enterprise reps.
            if track["required_for"] == "Enterprise" and rep["segment"] != "Enterprise":
                continue
            # A rep 45 days in hasn't finished the depth track yet.
            if tenure < 100 and not track["is_mandatory"]:
                conn.execute(
                    "INSERT INTO certifications (rep_id, track_id, status) VALUES (?,?,?)",
                    (rid, tid, "in_progress"))
                continue

            if name == "Omar Haddad" and track_name == "Security & Data Handling":
                # Lapsed five weeks ago — the cert_expired signal.
                certified = TODAY - timedelta(days=400)
                expires = certified + timedelta(days=365)
                status = "expired"
            elif name in ("Rachel Kim", "Chris Boone") and track_name == "Security & Data Handling":
                # Renewal due inside 45 days — the cert_expiring nudge.
                certified = TODAY - timedelta(days=335)
                expires = certified + timedelta(days=365)
                status = "certified"
            elif tenure < 60 and track["is_mandatory"]:
                conn.execute(
                    "INSERT INTO certifications (rep_id, track_id, status) VALUES (?,?,?)",
                    (rid, tid, "in_progress"))
                continue
            else:
                certified = TODAY - timedelta(days=RNG.randint(30, 300))
                expires = certified + timedelta(days=30 * track["valid_months"])
                status = "certified" if expires > TODAY else "expired"

            conn.execute(
                """INSERT INTO certifications (rep_id, track_id, status, certified_on, expires_on)
                   VALUES (?,?,?,?,?)""",
                (rid, tid, status, _iso(certified), _iso(expires)),
            )

            # Completed enrollments for the courses inside a finished track.
            for code in dict((t[0], t[5]) for t in TRACKS)[track_name]:
                conn.execute(
                    """INSERT OR IGNORE INTO enrollments
                       (rep_id, course_id, status, score, assigned_on, completed_on, assigned_reason)
                       VALUES (?,?,?,?,?,?,?)""",
                    (rid, course_ids[code], "complete", RNG.randint(78, 98),
                     _iso(certified - timedelta(days=20)), _iso(certified),
                     f"Required for {track_name}"),
                )

    # --- upcoming instructor-led sessions -------------------------------------
    instructors = ["Marcus Bell", "Priya Raghunathan", "Dana Whitfield",
                   "Tomás Herrera", "L&D — Jess Moreau"]
    locations = ["Zoom", "Zoom", "HQ — Room 4B", "Zoom", "Chicago Office"]
    scheduled = ["METH-120", "METH-140", "COMP-100", "COMP-120", "METH-110",
                 "METH-160", "METH-170", "COMP-110", "PROD-201", "METH-130"]
    for i, code in enumerate(scheduled):
        starts = TODAY + timedelta(days=3 + i * 4, hours=0)
        conn.execute(
            """INSERT INTO class_sessions (course_id, starts_at, duration_h, location,
                                           instructor, seats)
               VALUES (?,?,?,?,?,?)""",
            (course_ids[code], f"{_iso(starts)} {RNG.choice(['09:00', '11:00', '13:00', '15:00'])}",
             RNG.choice([1.5, 2.0, 3.0]), RNG.choice(locations), RNG.choice(instructors),
             RNG.choice([12, 16, 20, 25])),
        )

    conn.commit()
    return course_ids, track_ids


# ----------------------------------------------------------------- hiring ---

REQS = [
    dict(title="Enterprise Account Executive — West", level="Senior AE", team="Enterprise West",
         region="West", segment="Enterprise", quota_annual=1_600_000,
         comp_base=145_000, comp_ote=290_000, manager="Marcus Bell", status="open",
         must_haves="5+ years closing net-new B2B SaaS in the enterprise segment\n"
                    "Carried and hit a quota of $1M or more\n"
                    "Experience running 6+ month cycles with 5+ stakeholders\n"
                    "Track record of self-sourcing at least 40% of pipeline",
         nice_haves="Sold into healthcare or financial services\n"
                    "MEDDPICC or Command of the Message training\n"
                    "Experience selling alongside a partner channel"),
    dict(title="Mid-Market Account Executive — East", level="AE", team="Mid-Market East",
         region="East", segment="Mid-Market", quota_annual=900_000,
         comp_base=105_000, comp_ote=210_000, manager="Priya Raghunathan", status="open",
         must_haves="3+ years full-cycle B2B SaaS closing experience\n"
                    "Comfortable with a 60-90 day sales cycle and high deal volume\n"
                    "Demonstrated pipeline generation motion",
         nice_haves="Mid-market to enterprise transition experience\n"
                    "Familiarity with a formal qualification framework"),
    dict(title="SMB Account Executive", level="AE", team="SMB Velocity",
         region="Central", segment="SMB", quota_annual=520_000,
         comp_base=75_000, comp_ote=150_000, manager="Tomás Herrera", status="draft",
         must_haves="1-3 years quota-carrying experience, SDR-to-AE promotions welcome\n"
                    "High-velocity mindset: 20+ live opportunities at once\n"
                    "Strong written outbound",
         nice_haves="Experience with a product-led motion\n"
                    "Multilingual"),
]

CANDIDATES = [
    dict(name="Marisol Vega", email="marisol.vega@example.com", phone="(415) 555-0142",
         location="San Francisco, CA", years_exp=8, current_title="Senior Enterprise AE",
         current_company="Lattice Grid Software", source="LinkedIn",
         resume=[
             "Senior Enterprise Account Executive, Lattice Grid Software (2021-present)",
             "Carried a $1.4M annual quota against healthcare and life sciences accounts; finished at 118%, 104%, and 127% over the last three years.",
             "Closed a $610K three-year agreement with a 14-hospital health system across an 11-month cycle involving legal, InfoSec, clinical IT, and the CFO's office.",
             "Self-sourced 52% of pipeline through a named-account outbound motion built with a partner SE.",
             "MEDDPICC certified; ran deal reviews for the two AEs who joined after me.",
             "Prior: Mid-Market AE, Corvid Analytics (2018-2021), 6 quarters over quota.",
         ]),
    dict(name="Devon Ashcroft", email="devon.ashcroft@example.com", phone="(312) 555-0188",
         location="Chicago, IL", years_exp=6, current_title="Enterprise AE",
         current_company="Ferrous Data", source="Referral — Marcus Bell",
         resume=[
             "Enterprise Account Executive, Ferrous Data (2022-present)",
             "$1.1M quota selling data infrastructure into financial services. 96% and 112% attainment.",
             "Longest cycle closed: 9 months, $340K, involving a bank's model risk committee.",
             "Roughly 30% self-sourced; the rest came from an SDR pod and field marketing.",
             "Ran the competitive teardown deck the whole team used against two named rivals.",
             "Prior: AE, Sablefish Networks (2019-2022), mid-market, 88%-115% attainment.",
         ]),
    dict(name="Priyanka Bhatt", email="priyanka.bhatt@example.com", phone="(646) 555-0117",
         location="New York, NY", years_exp=4, current_title="Mid-Market AE",
         current_company="Northgate Cloud", source="Inbound",
         resume=[
             "Mid-Market Account Executive, Northgate Cloud (2023-present)",
             "$820K quota, 70-day average cycle, 106% attainment in the last full year.",
             "Averaged 22 open opportunities; highest deal volume on a team of nine.",
             "Built the outbound sequence library now used across the mid-market team; 45% of pipeline self-sourced.",
             "Promoted from SDR in 14 months, the fastest on record at Northgate.",
         ]),
    dict(name="Curtis Whelan", email="curtis.whelan@example.com", phone="(206) 555-0164",
         location="Seattle, WA", years_exp=11, current_title="Regional Sales Director",
         current_company="Ambervale Systems", source="LinkedIn",
         resume=[
             "Regional Sales Director, Ambervale Systems (2020-present)",
             "Manage six AEs against a $9M regional number; team hit 101% and 94%.",
             "Individually closed $2.1M in a player-coach year before the team grew.",
             "Prior: Enterprise AE, Ambervale (2016-2020), $1.5M quota, 3 of 4 years above 100%.",
             "Looking to return to an individual contributor seat.",
         ]),
    dict(name="Aisha Nwosu", email="aisha.nwosu@example.com", phone="(404) 555-0193",
         location="Atlanta, GA", years_exp=7, current_title="Enterprise AE",
         current_company="Quillfeather Health", source="Event — SaaStr",
         resume=[
             "Enterprise Account Executive, Quillfeather Health (2021-present)",
             "$1.25M quota selling clinical workflow software to hospital systems and payers.",
             "Attainment: 134%, 91%, 108%. The 91% year was a single $400K deal that slipped one quarter.",
             "Every deal ran through a security review and a clinical review board; average cycle 8 months.",
             "Self-sourced 61% of pipeline; built the health-system account map still in use.",
             "Command of the Message and MEDDPICC trained.",
         ]),
    dict(name="Ryan Delacroix", email="ryan.delacroix@example.com", phone="(720) 555-0125",
         location="Denver, CO", years_exp=3, current_title="Account Executive",
         current_company="Pinebox Retail Tech", source="Inbound",
         resume=[
             "Account Executive, Pinebox Retail Tech (2023-present)",
             "$540K quota selling POS software to independent retailers. 97% attainment.",
             "35-day average cycle, mostly single-decision-maker deals under $20K.",
             "Handled 30+ live opportunities at a time out of a shared inbound queue.",
             "Prior: SDR, Pinebox (2022-2023), 140% of meeting target.",
         ]),
    dict(name="Hana Sørensen", email="hana.sorensen@example.com", phone="(617) 555-0179",
         location="Boston, MA", years_exp=9, current_title="Strategic AE",
         current_company="Cindermark Financial Software", source="LinkedIn",
         resume=[
             "Strategic Account Executive, Cindermark Financial Software (2019-present)",
             "$1.8M quota selling into tier-one banks and asset managers. 112%, 87%, 121%, 103%.",
             "Closed a $900K enterprise agreement over 16 months with seven named stakeholders.",
             "Partner-led motion: two thirds of pipeline sourced with system integrators.",
             "Runs the firm's regulated-industry procurement playbook.",
         ]),
    dict(name="Marcus Oyelaran", email="marcus.oyelaran@example.com", phone="(773) 555-0151",
         location="Chicago, IL", years_exp=5, current_title="Mid-Market AE",
         current_company="Thornbury Logistics Cloud", source="Referral",
         resume=[
             "Mid-Market Account Executive, Thornbury Logistics Cloud (2021-present)",
             "$760K quota, 100% and 109% the last two years.",
             "80-day cycle into operations and finance buyers at 3PLs and distributors.",
             "Self-sourced 48% of pipeline; strongest written outbound on the team.",
             "Prior: BDR team lead (2020-2021).",
         ]),
    dict(name="Elena Petrova", email="elena.petrova@example.com", phone="(858) 555-0136",
         location="San Diego, CA", years_exp=2, current_title="Junior AE",
         current_company="Wrenfield Design Tools", source="Inbound",
         resume=[
             "Junior Account Executive, Wrenfield Design Tools (2024-present)",
             "$340K quota against a product-led signup base. 103% attainment in year one.",
             "Converts self-serve trials to team plans; average deal size $9K, 21-day cycle.",
             "Fluent in Russian and Spanish; runs the EMEA-timezone trial queue.",
         ]),
    dict(name="Todd Bramwell", email="todd.bramwell@example.com", phone="(214) 555-0108",
         location="Dallas, TX", years_exp=14, current_title="Enterprise AE",
         current_company="Halberd Industrial Software", source="LinkedIn",
         resume=[
             "Enterprise Account Executive, Halberd Industrial Software (2014-present)",
             "$1.2M quota selling maintenance software to manufacturers and utilities.",
             "Attainment over the last four years: 74%, 81%, 69%, 78%.",
             "Deals average 10 months; largest was $280K.",
             "Almost all pipeline is inbound or account-manager referral; no outbound motion.",
         ]),
]


def seed_hiring(conn, rep_ids, course_ids):
    """Reqs, candidates, applications, and one interview loop already completed."""
    req_ids = {}
    for r in REQS:
        req_ids[r["title"]] = conn.execute(
            """INSERT INTO reqs (title, level, team, region, segment, quota_annual, comp_base,
                                 comp_ote, must_haves, nice_haves, hiring_manager_id, status,
                                 created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["title"], r["level"], r["team"], r["region"], r["segment"], r["quota_annual"],
             r["comp_base"], r["comp_ote"], r["must_haves"], r["nice_haves"],
             conn.execute("SELECT id FROM reps WHERE name = ?", (r["manager"],)).fetchone()["id"],
             r["status"], _iso(TODAY - timedelta(days=RNG.randint(8, 40)))),
        ).lastrowid

    cand_ids = {}
    for c in CANDIDATES:
        cand_ids[c["name"]] = conn.execute(
            """INSERT INTO candidates (name, email, phone, location, years_exp, current_title,
                                       current_company, resume_text, source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (c["name"], c["email"], c["phone"], c["location"], c["years_exp"],
             c["current_title"], c["current_company"], "\n".join(c["resume"]), c["source"],
             _iso(TODAY - timedelta(days=RNG.randint(2, 30)))),
        ).lastrowid

    ent = req_ids["Enterprise Account Executive — West"]
    mm = req_ids["Mid-Market Account Executive — East"]

    # A funnel with candidates at four different depths, so the pipeline board
    # has something in every column on first load.
    pipeline = [
        (ent, "Marisol Vega", "panel", 91, "Enterprise healthcare closer with $1M+ attainment three years running."),
        (ent, "Aisha Nwosu", "ai_interview", 88, "Hospital-system cycles with security and clinical review; 61% self-sourced."),
        (ent, "Hana Sørensen", "screened", 84, "Tier-one financial services, $1.8M quota, long regulated cycles."),
        (ent, "Devon Ashcroft", "screened", 72, "Financial services enterprise experience; self-sourcing below the bar."),
        (ent, "Curtis Whelan", "sourced", 63, "Strong numbers but currently managing; wants to return to an IC seat."),
        (ent, "Todd Bramwell", "rejected", 34, "Four consecutive years under quota and no outbound motion."),
        (mm, "Priyanka Bhatt", "ai_interview", 89, "High-volume mid-market with a real self-sourced motion."),
        (mm, "Marcus Oyelaran", "screened", 81, "Mid-market logistics, 48% self-sourced, consistent attainment."),
        (mm, "Ryan Delacroix", "sourced", 58, "SMB velocity profile; deal size well below mid-market."),
    ]

    app_ids = {}
    for req_id, cand_name, stage, score, reason in pipeline:
        created = TODAY - timedelta(days=RNG.randint(3, 25))
        aid = conn.execute(
            """INSERT INTO applications (req_id, candidate_id, stage, match_score, match_reason,
                                         created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (req_id, cand_ids[cand_name], stage, score, reason, _iso(created), _iso(TODAY)),
        ).lastrowid
        app_ids[(req_id, cand_name)] = aid

        # Backfill the stage history so the timeline isn't empty.
        ladder = ["sourced", "screened", "ai_interview", "panel", "offer", "hired"]
        path = ladder[:ladder.index(stage) + 1] if stage in ladder else ["sourced", "rejected"]
        for i, s in enumerate(path):
            conn.execute(
                """INSERT INTO stage_events (application_id, from_stage, to_stage, note, created_at)
                   VALUES (?,?,?,?,?)""",
                (aid, path[i - 1] if i else None, s,
                 "Seeded demo history", _iso(created + timedelta(days=i * 2))),
            )

    # One completed interview loop, scored at seed time by the offline engine so
    # the scorecard exists on a laptop with no API key.
    _seed_completed_interview(conn, req_ids, app_ids, ent)

    conn.commit()
    return req_ids, cand_ids


_DEMO_ANSWERS = {
    1: "Last four quarters: 134% on a $312K number, 91% the quarter a $400K health-system "
       "deal slipped, then 108% and 121%. The 91% was my fault — I took the CIO's verbal "
       "as a commit and never confirmed the board met in December, so it landed in Q1.",
    2: "61% self-sourced. My motion is account-based: I map the health system's IT org from "
       "public org charts and LinkedIn, pick the two service lines with a documented backlog, "
       "and lead with a peer story from a comparable system. Roughly 12 touches over 3 weeks.",
    3: "Largest was $410K with a 9-hospital system, 11 months. Seven stakeholders: clinical "
       "informatics, InfoSec, procurement, the CMIO, two service-line directors, and the CFO's "
       "analyst. Closed by getting the CMIO to present the business case internally instead of me.",
    4: "I lost a $250K payer deal I had in commit. I had a great champion in IT and never met "
       "anyone in the business. When their budget got reallocated my champion had no standing to "
       "fight for it. Now I don't forecast anything where I haven't spoken to the budget owner.",
    5: "I've sold clinical software for 6 years and want to keep selling to that buyer with a "
       "product that has a real security story. Of the roughly 60 health systems in your West "
       "patch I already know 40 by name and have closed business in 12 of them, so the ramp is "
       "product, not relationships.",
    6: "First week is triage: every open opportunity gets a call to confirm a real compelling "
       "event, and I kill what doesn't have one — last time I inherited a patch that removed 30% "
       "of the pipeline in 5 days. Second week I segment down to the 20 accounts with a "
       "documented initiative and start the stakeholder mapping motion on those.",
}


def _seed_completed_interview(conn, req_ids, app_ids, ent_req_id):
    req = conn.execute("SELECT * FROM reqs WHERE id = ?", (ent_req_id,)).fetchone()
    aid = app_ids[(ent_req_id, "Marisol Vega")]
    questions = _offline_interview(req, "screen", 6)
    answers = {str(k): v for k, v in _DEMO_ANSWERS.items()}
    result = _offline_score(questions, answers)
    conn.execute(
        """INSERT INTO interviews (application_id, kind, questions_json, answers_json,
                                   scores_json, overall_score, recommendation, summary,
                                   engine, created_at, completed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (aid, "screen", json.dumps(questions), json.dumps(answers), json.dumps(result),
         result["overall_score"], result["recommendation"], result["summary"], "offline",
         _iso(TODAY - timedelta(days=6)), _iso(TODAY - timedelta(days=6))),
    )


# ------------------------------------------------------------------- build --

def seed_sample_data():
    conn = get_connection()
    if conn.execute("SELECT COUNT(*) n FROM reps").fetchone()["n"]:
        conn.close()
        return
    vp_id, manager_ids, rep_ids = seed_org(conn)
    course_ids, track_ids = seed_training(conn, rep_ids)
    seed_hiring(conn, rep_ids, course_ids)
    conn.close()


def rebuild():
    if DB_FILE.exists():
        DB_FILE.unlink()
    create_tables()
    seed_sample_data()
    conn = get_connection()
    counts = {t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
              for t in ("reps", "deals", "activities", "courses", "cert_tracks",
                        "certifications", "enrollments", "class_sessions",
                        "reqs", "candidates", "applications", "interviews")}
    conn.close()
    print(f"Built {DB_FILE.name}")
    for t, n in counts.items():
        print(f"  {t:<16} {n}")


if __name__ == "__main__":
    rebuild()
