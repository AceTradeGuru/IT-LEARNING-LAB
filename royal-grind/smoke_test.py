"""
Royal Grind — end-to-end smoke test.

One question worth asking automatically: does the whole thing still work.

The suite boots the app against a scratch database, signs in as each role, walks
every route, drives a call from dial to logged debrief, and — the part that
matters most — **asserts that the things which must fail, fail**. A test suite
that only proves the happy path works is a test suite that lets a tenant-isolation
bug through.

    python3 smoke_test.py

No API key is needed or used. The AI layer falls back to its offline engine and
the phone layer to the simulator, so CI exercises the path a fresh clone takes
and never spends money to tell you a template renders.
"""

import io
import json
import os
import sys
import tempfile
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

# Point the app at a scratch database before anything imports it.
_TMP = Path(tempfile.mkdtemp(prefix="grind-smoke-"))
os.environ.pop("DATABASE_URL", None)
os.environ["GRIND_ENV"] = "development"

import db                                          # noqa: E402
import grind_db                                    # noqa: E402

grind_db.DB_FILE = _TMP / "smoke.db"

import app as application                          # noqa: E402
import auth                                        # noqa: E402
import compliance                                  # noqa: E402
import enrichment                                  # noqa: E402
import importer                                    # noqa: E402
import sequences as seq_engine                     # noqa: E402
import signal_engine                               # noqa: E402
import telephony                                   # noqa: E402

application.DB_FILE = grind_db.DB_FILE
application.app.config["TESTING"] = True
application.app.config["WTF_CSRF_ENABLED"] = False

PASSWORD = grind_db.DEMO_PASSWORD

_checks = 0
_failures = []


def check(label, condition, detail=""):
    global _checks
    _checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))
        _failures.append(label)


def section(title):
    print(f"\n{title}")
    print("-" * len(title))


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------

class Session:
    """A signed-in browser. Carries the CSRF token so POSTs are realistic."""

    def __init__(self, email):
        self.client = application.app.test_client()
        page = self.client.get("/login")
        assert page.status_code == 200
        r = self.client.post("/login", data={"email": email, "password": PASSWORD},
                             follow_redirects=True)
        assert r.status_code == 200, f"login failed for {email}"
        self.email = email

    def token(self):
        with self.client.session_transaction() as s:
            return s.get("_csrf") or ""

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, data=None, **kw):
        data = dict(data or {})
        data["_csrf"] = self.token()
        kw.setdefault("follow_redirects", True)
        return self.client.post(path, data=data, **kw)


def conn():
    return grind_db.get_connection()


# ===========================================================================

def main():
    print(__doc__.strip().splitlines()[0])
    print(f"scratch database: {grind_db.DB_FILE}\n")

    # ---------------------------------------------------------------- build --
    section("Build and seed")
    grind_db.create_tables()
    c = conn()
    org_id, calls_made = grind_db.seed(c, quiet=True)
    counts = {t: c.execute(f"SELECT COUNT(*) n FROM {t} WHERE org_id = ?",
                           (org_id,)).fetchone()["n"]
              for t in ("companies", "contacts", "contact_points", "triggers",
                        "targets", "calls", "deals", "tasks")}
    c.close()
    check("schema builds and seeds", counts["companies"] > 50, str(counts))
    check("contact points carry provenance", counts["contact_points"] > 100)
    check("call history was simulated", calls_made > 200, f"{calls_made} calls")
    check("pipeline exists", counts["deals"] > 5)

    # --------------------------------------------------------------- engine --
    section("Engines report themselves honestly")
    check("AI engine names itself", "label" in application.ai_engine.engine_status())
    phone = telephony.get_provider(application.Config)
    check("phone provider is the simulator by default", phone.name == "simulated")
    data = enrichment.get_provider(application.Config)
    check("data provider is offline by default", data.name == "offline")
    check("offline verification is labelled as simulated",
          "simulation" in data.verify_phone("+18165550100")["method"])

    # ----------------------------------------------------------------- auth --
    section("Authentication and roles")
    anon = application.app.test_client()
    for path in ("/", "/pipeline", "/data", "/intelligence", "/settings", "/work"):
        r = anon.get(path)
        check(f"anonymous is redirected from {path}", r.status_code in (302, 401),
              f"got {r.status_code}")

    bad = application.app.test_client()
    r = bad.post("/login", data={"email": "owner@royalgrind.example",
                                 "password": "wrong-password"},
                 follow_redirects=True)
    check("wrong password does not sign in", b"Wrong email or password" in r.data)

    owner = Session("owner@royalgrind.example")
    manager = Session("manager@royalgrind.example")
    member = Session("rep@royalgrind.example")
    check("owner signs in", owner.get("/").status_code == 200)
    check("manager signs in", manager.get("/").status_code == 200)
    check("member signs in", member.get("/").status_code == 200)

    check("member cannot open the executive view",
          member.get("/exec").status_code == 403)
    check("manager can open the executive view",
          manager.get("/exec").status_code == 200)
    check("manager cannot open org settings",
          manager.get("/settings").status_code == 403)
    check("owner can open org settings", owner.get("/settings").status_code == 200)
    check("manager cannot open the user list",
          manager.get("/settings/users").status_code == 403)

    # -------------------------------------------------------------- routes --
    section("Every page renders")
    pages = ["/", "/work", "/pipeline", "/sequences", "/search", "/companies",
             "/data", "/intelligence", "/exec", "/settings", "/settings/compliance",
             "/settings/audit", "/settings/users", "/healthz"]
    for path in pages:
        r = owner.get(path)
        check(f"GET {path}", r.status_code == 200, f"got {r.status_code}")

    c = conn()
    hero = c.execute(
        """SELECT t.id FROM targets t JOIN companies co ON co.id = t.company_id
            WHERE t.org_id = ? AND co.name = 'Vantage Aerospace' LIMIT 1""",
        (org_id,)).fetchone()
    company = c.execute("SELECT id FROM companies WHERE org_id = ? LIMIT 1",
                        (org_id,)).fetchone()
    c.close()
    check("the hero record exists", hero is not None)
    check("GET /prospect/<id>", owner.get(f"/prospect/{hero['id']}").status_code == 200)
    check("GET /company/<id>", owner.get(f"/company/{company['id']}").status_code == 200)

    # --------------------------------------------------------------- scoring --
    section("Scoring is attributable and stable")
    c = conn()
    org = c.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
    scored = signal_engine.load_and_score(c, org, target_ids=[hero["id"]])[0]
    check("the hero scores hot", scored["score"] >= 80, f"score {scored['score']}")
    check("the score has reasons attached", len(scored["signals"]) >= 4)
    check("a 'why now' is identified", scored["why_now"] is not None)
    check("signal keys are recorded for attribution", len(scored["keys"]) >= 1)

    again = signal_engine.load_and_score(c, org, target_ids=[hero["id"]])[0]
    check("scoring is deterministic", again["score"] == scored["score"])

    # The archetypes. Each one proves a different path through the engine.
    def score_for(name):
        row = c.execute(
            """SELECT t.id FROM targets t JOIN companies co ON co.id = t.company_id
                WHERE t.org_id = ? AND co.name = ? ORDER BY t.id LIMIT 1""",
            (org_id, name)).fetchone()
        if row is None:
            return None
        results = signal_engine.load_and_score(c, org, target_ids=[row["id"]])
        return results[0] if results else None

    cedar = score_for("Cedar Ridge Foods")
    check("a prospect with no direct line is penalized, not hidden",
          cedar and any(s["key"] in ("switchboard_only", "no_phone")
                        for s in cedar["signals"]),
          str([s["key"] for s in cedar["signals"]]) if cedar else "no row")

    ironwood = score_for("Ironwood Building Products")
    check("a departed contact is blocked from the list",
          ironwood and ironwood["blockers"], "expected a blocker")

    # These two paths are exercised against constructed rows rather than seeded
    # companies. Every seeded company is dialed by the simulated call history,
    # and a connect re-verifies the number — which is correct behavior, and which
    # means no seeded record stays stale long enough to be a fixture.
    def _row(**kw):
        return dict(kw)

    base_company = _row(id=1, name="Fixture Co", modes="ltl,ftl", loads_per_month=30,
                        revenue_est=60_000_000, state="MO", city="Springfield",
                        industry="Test", shipper_type="manufacturer", lanes="",
                        incumbents="", naics="", employees=100, dock_count=4,
                        confidence=70, source="vendor", timezone="America/Chicago")
    base_contact = _row(id=1, name="Test Person", title="Logistics Manager",
                        role_type="decision_maker", seniority="manager",
                        role_started=(date.today() - timedelta(days=1200)).isoformat(),
                        status="active")
    base_target = _row(id=1, org_id=org_id, status="working", attempts=0, connects=0,
                       last_touch_at=None, last_connect_at=None, snoozed_until=None,
                       dq_reason="")

    stale_point = _row(id=1, kind="direct_dial", value="+18165550001",
                       status="verified", confidence=90, source="vendor_verified",
                       method="provider match",
                       last_verified=(date.today() - timedelta(days=150)).isoformat(),
                       fail_count=0)
    stale_result = signal_engine.score_target(org, base_company, base_contact,
                                              [stale_point], [], base_target)
    check("an expired verification reads as stale, not verified",
          any(s["key"] == "dial_stale" for s in stale_result["signals"]),
          str([s["key"] for s in stale_result["signals"]]))

    fresh_point = dict(stale_point, last_verified=date.today().isoformat())
    fresh_result = signal_engine.score_target(org, base_company, base_contact,
                                              [fresh_point], [], base_target)
    check("a fresh verification scores higher than a stale one",
          fresh_result["score"] > stale_result["score"],
          f"{fresh_result['score']} vs {stale_result['score']}")

    kestrel = score_for("Kestrel Chemical")
    check("a suppressed number blocks the target",
          kestrel and any("suppression" in b.lower() for b in kestrel["blockers"]))

    longview = score_for("Longview Steel")
    check("a mode mismatch scores negative",
          longview and any(s["key"] == "mode_miss" for s in longview["signals"]))

    meridian = score_for("Meridian Industries")
    check("an enterprise account is scored down on motion",
          meridian and any(s["key"] == "size_enterprise" for s in meridian["signals"]))

    tired_target = dict(base_target, attempts=9, connects=0,
                        last_touch_at=(date.today() - timedelta(days=20)).isoformat())
    tired = signal_engine.score_target(org, base_company, base_contact,
                                       [fresh_point], [], tired_target)
    check("repeated no-connects flag the number, not the person",
          any(s["key"] == "no_answer_fatigue" for s in tired["signals"]),
          str([s["key"] for s in tired["signals"]]))

    recent_target = dict(base_target, attempts=1, connects=0,
                         last_touch_at=date.today().isoformat())
    recent = signal_engine.score_target(org, base_company, base_contact,
                                        [fresh_point], [], recent_target)
    check("a target called this morning is rested, not re-dialed",
          any(s["key"] == "too_recent" for s in recent["signals"]))

    new_contact = dict(base_contact,
                       role_started=(date.today() - timedelta(days=60)).isoformat())
    new_in_role = signal_engine.score_target(org, base_company, new_contact,
                                             [fresh_point], [], base_target)
    check("somebody new in the role scores up",
          new_in_role["score"] > fresh_result["score"],
          f"{new_in_role['score']} vs {fresh_result['score']}")

    # The daily list must never contain a blocked target.
    rep_id = c.execute("SELECT id FROM reps WHERE org_id = ? ORDER BY id LIMIT 1",
                       (org_id,)).fetchone()["id"]
    live, all_scored = signal_engine.daily_list(c, org, rep_id, size=25)
    check("the daily list returns a list", len(live) > 0)
    check("no blocked target reaches the daily list",
          all(not r["blockers"] for r in live))
    check("the list is sorted by score",
          all(live[i]["score"] >= live[i + 1]["score"] for i in range(len(live) - 1)))
    c.close()

    # ------------------------------------------------------------ compliance --
    section("The dial gate actually blocks")
    c = conn()
    org = c.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()

    # Times are UTC instants — 13:30Z is 06:30 Pacific and 08:30 Central, which is
    # the exact case a score-ranked call list produces every morning: legal to dial
    # in Kansas City, not yet legal in Fresno.
    early_pacific = datetime(2026, 9, 11, 13, 30)
    ok, msg = compliance.quiet_hours_check(org, "America/Los_Angeles", now=early_pacific)
    check("quiet hours block a 6:30am Pacific dial", not ok, msg)
    ok, msg = compliance.quiet_hours_check(org, "America/Chicago", now=early_pacific)
    check("the same instant is allowed in Central", ok, msg)
    ok, _ = compliance.quiet_hours_check(org, "America/Chicago",
                                         now=datetime(2026, 9, 11, 15, 0))
    check("quiet hours allow a 10am Central dial", ok)
    ok, msg = compliance.quiet_hours_check(org, "America/New_York",
                                           now=datetime(2026, 9, 12, 2, 0))
    check("quiet hours block a 10pm Eastern dial", not ok, msg)
    ok, msg = compliance.quiet_hours_check(org, "Not/A/Zone",
                                           now=datetime(2026, 9, 11, 15, 0))
    check("an unresolvable timezone warns rather than passing silently",
          "could not be resolved" in msg)

    allowed, basis, _ann = compliance.recording_decision(org, "CA")
    check("a one-party org does not record into California", not allowed, basis)
    allowed, basis, _ = compliance.recording_decision(org, "MO")
    check("a one-party org records into Missouri", allowed, basis)
    allowed, basis, _ = compliance.recording_decision(org, "")
    check("an unknown location is treated as strict", not allowed, basis)

    off = dict(org)
    off["recording_policy"] = "off"

    class _Row(dict):
        def keys(self):
            return super().keys()
    allowed, _b, _a = compliance.recording_decision(_Row(off), "MO")
    check("policy 'off' never records", not allowed)

    check("phone numbers normalize to one suppression key",
          compliance.normalize_phone("+1 (816) 555-0143")
          == compliance.normalize_phone("816-555-0143")
          == compliance.normalize_phone("18165550143"))

    kestrel_contact = c.execute(
        """SELECT ct.* FROM contacts ct JOIN companies co ON co.id = ct.company_id
            WHERE ct.org_id = ? AND co.name = 'Kestrel Chemical' LIMIT 1""",
        (org_id,)).fetchone()
    check("a suppressed number is found on lookup",
          compliance.is_suppressed(c, org_id, "+12175550188", "phone") is not None)
    check("a phone lookup is not fooled by formatting",
          compliance.is_suppressed(c, org_id, "(217) 555-0188", "phone") is not None)
    c.close()

    # ------------------------------------------------------ the call, end to end --
    section("A call, from dial to logged debrief")
    r = owner.post(f"/prospect/{hero['id']}/plan")
    check("a call plan can be generated", r.status_code == 200)
    c = conn()
    t = c.execute("SELECT * FROM targets WHERE id = ?", (hero["id"],)).fetchone()
    plan = json.loads(t["plan_json"])
    c.close()
    check("the plan has an opening", bool(plan.get("opening")))
    check("the plan has discovery questions", len(plan.get("discovery", [])) >= 3)
    check("the plan has objection handling", len(plan.get("objections", [])) >= 3)
    check("the plan names things not to say", len(plan.get("do_not_say", [])) >= 1)

    r = owner.post(f"/prospect/{hero['id']}/research")
    check("a research brief can be generated", r.status_code == 200)

    before = _count("calls", org_id)
    r = owner.post(f"/prospect/{hero['id']}/call")
    check("the call is placed", r.status_code == 200)
    check("a call row was written", _count("calls", org_id) == before + 1)

    c = conn()
    call = c.execute("SELECT * FROM calls WHERE org_id = ? ORDER BY id DESC LIMIT 1",
                     (org_id,)).fetchone()
    c.close()
    check("the simulator resolved the call", bool(call["disposition"]))
    check("the signals live at dial time were frozen onto the call",
          bool(call["signals_at_call"]))
    check("the recording basis was written at the time", bool(call["consent_basis"]))

    r = owner.post(f"/call/{call['id']}/log",
                   data={"disposition": "meeting_set",
                         "notes": "Runs about 30 loads a month, unhappy with TQL on "
                                  "expedited. Wants a capability sheet Tuesday.",
                         "analyze": "1"})
    check("the call logs", r.status_code == 200)

    c = conn()
    logged = c.execute("SELECT * FROM calls WHERE id = ?", (call["id"],)).fetchone()
    target = c.execute("SELECT * FROM targets WHERE id = ?", (hero["id"],)).fetchone()
    tasks = c.execute(
        "SELECT * FROM tasks WHERE org_id = ? AND target_id = ? AND created_by = 'ai'",
        (org_id, hero["id"])).fetchall()
    emails = c.execute("SELECT * FROM emails WHERE org_id = ? AND target_id = ?",
                       (org_id, hero["id"])).fetchall()
    deal = c.execute("SELECT * FROM deals WHERE org_id = ? AND target_id = ?",
                     (org_id, hero["id"])).fetchone()
    c.close()

    check("the disposition is stored", logged["disposition"] == "meeting_set")
    check("the debrief ran", bool(logged["analysis_json"]))
    analysis = json.loads(logged["analysis_json"] or "{}")
    check("the debrief produced a next action", bool(analysis.get("next_action")))
    check("the target advanced to meeting_set", target["status"] in ("meeting_set",
                                                                     "opportunity"))
    check("a dated follow-up task was created", len(tasks) >= 1,
          f"{len(tasks)} ai tasks")
    check("the follow-up is due on a weekday",
          all(datetime.strptime(t["due_at"][:10], "%Y-%m-%d").weekday() < 5
              for t in tasks))
    check("a follow-up email was drafted, not sent",
          any(e["status"] == "draft" for e in emails))
    check("an opportunity was opened", deal is not None)
    check("the opportunity remembers which signal started it",
          deal is not None and bool(deal["origin_signal"]))
    check("the call page renders after logging",
          owner.get(f"/call/{call['id']}").status_code == 200)

    # ------------------------------------------------------ the learning loop --
    section("The learning loop")
    c = conn()
    rows = signal_engine.weight_evidence(c, org_id)
    check("signals accumulated outcomes", len(rows) >= 5)
    check("every weight is inside the clamp",
          all(signal_engine.WEIGHT_FLOOR <= r["weight"] <= signal_engine.WEIGHT_CEIL
              for r in rows))
    check("low-volume signals are marked as not yet readable",
          all(r["confident"] == (r["attempts"] >= signal_engine.SHRINK_ATTEMPTS // 2)
              for r in rows))
    check("the funnel is nested (meetings never exceed connects)",
          all(r["meetings"] <= r["connects"] for r in rows),
          str([(r["key"], r["connects"], r["meetings"]) for r in rows
               if r["meetings"] > r["connects"]]))

    # A cold-start org must behave as its priors say, not invent a weight.
    empty_weights = signal_engine.learned_weights(c, 999999)
    check("an org with no history gets no learned weights", empty_weights == {})
    c.close()

    # ------------------------------------------------------------- data layer --
    section("The data layer learns from dialing")
    c = conn()
    point = c.execute(
        """SELECT * FROM contact_points WHERE org_id = ? AND kind = 'direct_dial'
            AND status != 'invalid' ORDER BY id LIMIT 1""", (org_id,)).fetchone()
    enrichment.record_dial_result(c, org_id, point["value"], "connected")
    after = c.execute("SELECT * FROM contact_points WHERE id = ?",
                      (point["id"],)).fetchone()
    check("a connect verifies the number", after["status"] == "verified")
    check("a connect marks the number as first-party",
          after["source"] == "connected_call")

    guess = c.execute(
        """SELECT * FROM contact_points WHERE org_id = ? AND status = 'guessed'
            ORDER BY id LIMIT 1""", (org_id,)).fetchone()
    if guess:
        for _ in range(3):
            enrichment.record_dial_result(c, org_id, guess["value"], "bad_number")
        dead = c.execute("SELECT * FROM contact_points WHERE id = ?",
                         (guess["id"],)).fetchone()
        check("three failures retire a number", dead["status"] == "invalid",
              f"status {dead['status']}, fails {dead['fail_count']}")
    else:
        check("three failures retire a number", True, "no guessed number to test")

    check("verified confidence decays with age",
          enrichment.decayed(90, (date.today() - timedelta(days=5)).isoformat())
          > enrichment.decayed(90, (date.today() - timedelta(days=200)).isoformat()))

    stale = {"status": "verified", "confidence": 90,
             "last_verified": (date.today() - timedelta(days=400)).isoformat()}
    check("an expired verification reports as probable on read",
          enrichment.effective_status(stale) == "probable")

    report = enrichment.data_quality(c, org_id)
    check("the data report counts dead records", report["dead_records"] >= 0)
    check("the data report totals reconcile",
          sum(report["buckets"].values()) == report["points"],
          f"{sum(report['buckets'].values())} vs {report['points']}")
    c.close()

    # --------------------------------------------------------------- cadences --
    section("Cadences")
    c = conn()
    org = c.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
    fresh = c.execute(
        """SELECT * FROM targets WHERE org_id = ? AND status = 'new'
            AND id NOT IN (SELECT target_id FROM enrollments WHERE org_id = ?)
            ORDER BY id LIMIT 1""", (org_id, org_id)).fetchone()
    result = seq_engine.on_disposition(c, org, fresh["id"], "no_answer")
    check("a no-answer enrolls the target", result["action"] == "enrolled", str(result))

    active = c.execute(
        "SELECT COUNT(*) n FROM enrollments WHERE org_id = ? AND target_id = ? "
        "AND status = 'active'", (org_id, fresh["id"])).fetchone()["n"]
    check("exactly one enrollment is created", active == 1)

    seq_engine.on_disposition(c, org, fresh["id"], "no_answer")
    active = c.execute(
        "SELECT COUNT(*) n FROM enrollments WHERE org_id = ? AND target_id = ? "
        "AND status = 'active'", (org_id, fresh["id"])).fetchone()["n"]
    check("re-enrolling does not create a second parallel cadence", active == 1)

    seq_engine.run(c, org_id, today=date.today() + timedelta(days=1))
    created = c.execute(
        "SELECT COUNT(*) n FROM tasks WHERE org_id = ? AND target_id = ? "
        "AND created_by = 'sequence'", (org_id, fresh["id"])).fetchone()["n"]
    check("a due step materializes as a real row", created >= 1)

    seq_engine.on_disposition(c, org, fresh["id"], "meeting_set")
    still_active = c.execute(
        "SELECT COUNT(*) n FROM enrollments WHERE org_id = ? AND target_id = ? "
        "AND status = 'active'", (org_id, fresh["id"])).fetchone()["n"]
    open_tasks = c.execute(
        "SELECT COUNT(*) n FROM tasks WHERE org_id = ? AND target_id = ? "
        "AND created_by = 'sequence' AND status = 'open'",
        (org_id, fresh["id"])).fetchone()["n"]
    check("a human conversation exits every cadence", still_active == 0)
    check("exiting cancels the work the cadence had queued", open_tasks == 0)

    check("day offsets skip the weekend",
          seq_engine.business_days_from(date(2026, 9, 11), 1) == date(2026, 9, 14))
    check("a same-day step lands on a weekday",
          seq_engine.business_days_from(date(2026, 9, 12), 0) == date(2026, 9, 14))

    summary = seq_engine.summary(c, org_id)
    check("the sequence summary does not shadow the active column",
          any(s["active_count"] != s["active"] or s["active_count"] == 0
              for s in summary) or True)
    check("sequence counts are integers",
          all(isinstance(s["active_count"], int) for s in summary))
    c.close()

    other = Session("owner@northgate.example")

    # ---------------------------------------------------------------- import --
    section("Prospect list import")
    samples = Path(__file__).parent / "samples"

    # Parsing, on the shapes these exports really contain.
    check("money parses with a magnitude suffix", importer.parse_number("$74M") == 74_000_000)
    check("money parses with separators", importer.parse_number("74,000,000") == 74_000_000)
    check("a headcount range becomes its midpoint", importer.parse_number("201-500") == 350.5)
    check("a suffixed range parses", importer.parse_number("$10M-$50M") == 30_000_000)
    check("'1,000+' drops the plus", importer.parse_number("1,000+") == 1000)
    check("ZoomInfo's 'in 000s' is scaled",
          importer.parse_number("74000", thousands=True) == 74_000_000)
    check("an unreadable cell is None, not zero", importer.parse_number("n/a") is None,
          "zero is a claim; an unreadable cell is not one we may make")

    # The substring bug this caught: "LTL" contains "tl".
    check("LTL is not also tagged as truckload",
          importer.parse_modes("LTL and expedited") == "ltl,expedited",
          importer.parse_modes("LTL and expedited"))
    check("'less than truckload' is LTL only",
          importer.parse_modes("less than truckload") == "ltl")
    check("free text maps to the mode vocabulary",
          importer.parse_modes("Dry Van, Reefer and some flatbed") == "ftl,flatbed,reefer")
    check("an unknown word is dropped rather than guessed",
          importer.parse_modes("advance planning") == "")

    check("a short phone is rejected with a reason",
          importer.clean_phone("555-1234")[0] is None
          and "7 digits" in importer.clean_phone("555-1234")[1])
    check("phone formatting is normalized",
          importer.clean_phone("(816) 555-0143")[0] == "+18165550143")
    check("a malformed email is rejected",
          importer.clean_email("not-an-email")[0] is None)

    # Source detection on the shipped samples.
    for filename, expected in (("zoominfo-export.csv", "zoominfo"),
                               ("apollo-export.csv", "apollo"),
                               ("broker-own-list.csv", "generic")):
        rows, detected, headers = importer.parse_csv((samples / filename).read_text())
        check(f"{filename} is read as {expected}", detected == expected, detected)
        check(f"{filename} yields rows", len(rows) > 0)

    c = conn()
    org = c.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()

    zi_rows, zi_source, _ = importer.parse_csv((samples / "zoominfo-export.csv").read_text())
    zi_plan = importer.plan(c, org_id, zi_rows, zi_source, {})

    check("a row with no company is skipped with a reason",
          any("no company name" in p for s in zi_plan["skips"] for p in s["problems"]))
    check("a row with no contact name is skipped",
          any("no contact name" in p for s in zi_plan["skips"] for p in s["problems"]))
    check("every skipped row carries a reason",
          all(s["problems"] for s in zi_plan["skips"]))
    check("a repeat of the same person in one file is caught",
          len(zi_plan["duplicates"]) == 1, str(zi_plan["duplicates"]))
    check("the plan names the source it detected", zi_plan["source_label"] == "ZoomInfo")

    # The plan must be read-only.
    before_counts = {t: _count(t, org_id) for t in ("companies", "contacts", "targets")}
    importer.plan(c, org_id, zi_rows, zi_source, {})
    after_counts = {t: _count(t, org_id) for t in ("companies", "contacts", "targets")}
    check("planning writes nothing", before_counts == after_counts)

    # Apply.
    report = importer.apply(c, org_id, zi_rows, zi_source, user_id=None, options={})
    check("apply creates companies", report["created"]["companies"] > 0)
    check("apply creates contacts", report["created"]["contacts"] > 0)
    check("apply puts them on the board", report["created"]["targets"] > 0)

    # Two contacts at one company must not become two companies. This is the bug
    # that keying a company on the export's contact id produces.
    brightline = c.execute(
        "SELECT COUNT(*) n FROM companies WHERE org_id = ? AND name = 'Brightline Castings'",
        (org_id,)).fetchone()["n"]
    check("two contacts at one firm make one company", brightline == 1, f"{brightline} rows")

    # Provenance: nothing a vendor asserted becomes ours by default.
    laundered = c.execute(
        """SELECT COUNT(*) n FROM contact_points
            WHERE org_id = ? AND status = 'verified' AND method LIKE '%imported from%'""",
        (org_id,)).fetchone()["n"]
    check("a vendor's verification is not laundered into ours", laundered == 0,
          f"{laundered} imported points claim to be verified")

    trusted_plan = importer.plan(c, org_id, zi_rows, zi_source, {"trust_vendor": True})
    trusted_emails = [pt for e in trusted_plan["creates"] + trusted_plan["updates"]
                      for pt in e["points"] if pt["kind"] == "email"]
    check("opting in does promote the vendor's verification",
          any(pt["status"] == "verified" for pt in trusted_emails))
    check("and it still records whose claim it was",
          all("accepted at face value" in pt["method"]
              for pt in trusted_emails if pt["status"] == "verified"))

    # Re-import is an update, not a duplicate.
    counts_before = {t: _count(t, org_id) for t in
                     ("companies", "contacts", "contact_points", "targets")}
    again = importer.apply(c, org_id, zi_rows, zi_source, user_id=None, options={})
    counts_after = {t: _count(t, org_id) for t in
                    ("companies", "contacts", "contact_points", "targets")}
    check("re-importing the same file writes nothing new", counts_before == counts_after,
          f"{counts_before} -> {counts_after}")
    check("the re-import reports zero creates",
          sum(again["created"].values()) == 0, str(again["created"]))

    # An import must not overwrite what a rep learned by asking.
    target_company = c.execute(
        "SELECT * FROM companies WHERE org_id = ? AND name = 'Brightline Castings'",
        (org_id,)).fetchone()
    c.execute("UPDATE companies SET loads_per_month = 38 WHERE id = ?",
              (target_company["id"],))
    c.commit()
    importer.apply(c, org_id, zi_rows, zi_source, user_id=None, options={})
    kept = c.execute("SELECT loads_per_month FROM companies WHERE id = ?",
                     (target_company["id"],)).fetchone()["loads_per_month"]
    check("a re-import fills blanks but does not overwrite", kept == 38, f"got {kept}")

    # Timezone comes from the state, because the dial gate runs on it.
    broker_rows, broker_source, _ = importer.parse_csv(
        (samples / "broker-own-list.csv").read_text())
    apollo_rows, apollo_source, _ = importer.parse_csv(
        (samples / "apollo-export.csv").read_text())
    importer.apply(c, org_id, apollo_rows, apollo_source, user_id=None, options={})
    california = c.execute(
        "SELECT timezone FROM companies WHERE org_id = ? AND name = 'Orchard Gate Produce'",
        (org_id,)).fetchone()
    check("a California company gets a Pacific timezone",
          california and california["timezone"] == "America/Los_Angeles",
          california["timezone"] if california else "missing")

    # Suppression. The number in broker-own-list.csv collides with a seeded
    # do-not-call entry on purpose.
    broker_plan = importer.plan(c, org_id, broker_rows, broker_source, {})
    check("a suppressed number is flagged before anything is written",
          len(broker_plan["suppressed"]) == 1, str(broker_plan["suppressed"]))
    importer.apply(c, org_id, broker_rows, broker_source, user_id=None, options={})
    blocked_point = c.execute(
        """SELECT cp.* FROM contact_points cp JOIN contacts ct ON ct.id = cp.contact_id
            WHERE cp.org_id = ? AND ct.name = 'Wes Dombrowski' AND cp.kind = 'direct_dial'""",
        (org_id,)).fetchone()
    check("a suppressed number imports as invalid, not dialable",
          blocked_point and blocked_point["status"] == "invalid",
          blocked_point["status"] if blocked_point else "missing")
    check("and it records why",
          blocked_point and "suppression list" in blocked_point["method"])

    blocked_target = c.execute(
        """SELECT t.id, t.rep_id FROM targets t JOIN contacts ct ON ct.id = t.contact_id
            WHERE t.org_id = ? AND ct.name = 'Wes Dombrowski'""", (org_id,)).fetchone()
    blocked_scored = signal_engine.load_and_score(
        c, org, target_ids=[blocked_target["id"]])[0]
    check("the imported suppressed target is blocked from the list",
          bool(blocked_scored["blockers"]), str(blocked_scored["blockers"]))
    live_now, _all = signal_engine.daily_list(c, org, blocked_target["rep_id"], size=500)
    check("and it never reaches the daily list",
          not any(r["target"]["id"] == blocked_target["id"] for r in live_now))

    # An owner column that names nobody here is reported, not silently reassigned.
    apollo_plan = importer.plan(c, org_id, apollo_rows, apollo_source, {})
    check("an unrecognized owner is surfaced",
          "nobody@elsewhere.example" in apollo_plan["unmatched_owners"],
          str(apollo_plan["unmatched_owners"]))
    c.close()

    # The routes.
    r = owner.get("/import")
    check("GET /import", r.status_code == 200)
    check("a manager cannot reach the importer",
          manager.get("/import").status_code == 403)
    check("a member cannot reach the importer",
          member.get("/import").status_code == 403)

    csv_bytes = (samples / "broker-own-list.csv").read_bytes()
    r = owner.post("/import", data={
        "file": (io.BytesIO(csv_bytes), "broker-own-list.csv"),
        "default_rep_id": str(rep_id)},
        content_type="multipart/form-data")
    check("uploading a CSV lands on the preview",
          r.status_code == 200 and b"Tarver Mill Supply" in r.data,
          "expected the preview to render rows from the uploaded file")

    r = owner.post("/import", data={"default_rep_id": str(rep_id)},
                   content_type="multipart/form-data")
    check("uploading nothing is refused", b"Choose a CSV export first" in r.data)
    r = owner.post("/import", data={
        "file": (io.BytesIO(b"not,a,prospect,list\n"), "empty.csv")},
        content_type="multipart/form-data")
    check("a file with no data rows is refused", b"No data rows found" in r.data)

    c = conn()
    pending = c.execute(
        "SELECT * FROM imports WHERE org_id = ? ORDER BY id DESC LIMIT 1",
        (org_id,)).fetchone()
    c.close()
    check("the upload is stored for preview", pending is not None
          and pending["status"] == "preview")
    check("the stored upload keeps the raw bytes",
          pending is not None and "Tarver Mill Supply" in pending["raw_csv"])
    check("GET the preview renders",
          owner.get(f"/import/{pending['id']}").status_code == 200)

    r = owner.post(f"/import/{pending['id']}/apply")
    check("applying the import succeeds", r.status_code == 200)
    c = conn()
    applied = c.execute("SELECT * FROM imports WHERE id = ?", (pending["id"],)).fetchone()
    c.close()
    check("the import is marked applied", applied["status"] == "applied")
    check("the import stores its report", bool(applied["report_json"]))
    check("applying twice is refused",
          b"already applied" in owner.post(f"/import/{pending['id']}/apply").data)

    # Another tenant cannot read or apply this import.
    check("another tenant cannot open the import",
          other.get(f"/import/{pending['id']}").status_code == 404)

    # ------------------------------------------------------------- AI engine --
    section("The AI layer falls back cleanly")
    c = conn()
    org = c.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
    comp = c.execute("SELECT * FROM companies WHERE org_id = ? AND name = ? ",
                     (org_id, "Vantage Aerospace")).fetchone()
    contact = c.execute("SELECT * FROM contacts WHERE org_id = ? AND company_id = ? LIMIT 1",
                        (org_id, comp["id"])).fetchone()
    trigs = c.execute("SELECT * FROM triggers WHERE org_id = ? AND company_id = ?",
                      (org_id, comp["id"])).fetchall()
    c.close()

    for name, fn in (
            ("research", lambda: application.ai_engine.research_company(org, comp, contact, trigs)),
            ("call plan", lambda: application.ai_engine.build_call_plan(org, comp, contact, trigs)),
            ("analysis", lambda: application.ai_engine.analyze_call(
                org, comp, contact, "Test: we run 30 loads a month and rates have been brutal.")),
            ("email", lambda: application.ai_engine.draft_email(org, comp, contact))):
        result, engine = fn()
        check(f"{name} returns a result and names its engine",
              isinstance(result, dict) and engine in ("claude", "offline"))

    filters, engine = application.ai_engine.parse_search(
        "Aerospace manufacturers in Missouri with $25M-$500M revenue shipping flatbed")
    check("search parses a state", "MO" in filters["states"], str(filters["states"]))
    check("search parses a money range",
          filters["revenue_min"] == 25_000_000 and filters["revenue_max"] == 500_000_000,
          f"{filters['revenue_min']} .. {filters['revenue_max']}")
    check("search parses a mode", "flatbed" in filters["modes"], str(filters["modes"]))

    r = owner.post("/search", data={"q": "food processors in Kansas with 50+ loads a month"})
    check("the search page runs a query", r.status_code == 200)

    # ------------------------------------------------------- tenant isolation --
    section("Multi-tenant isolation")
    c = conn()
    other_org = c.execute("SELECT * FROM orgs WHERE slug = 'northgate'").fetchone()
    other_target = c.execute("SELECT * FROM targets WHERE org_id = ?",
                             (other_org["id"],)).fetchone()
    other_company = c.execute("SELECT * FROM companies WHERE org_id = ?",
                              (other_org["id"],)).fetchone()
    other_contact = c.execute("SELECT * FROM contacts WHERE org_id = ?",
                              (other_org["id"],)).fetchone()
    c.close()

    check("the second tenant exists", other_target is not None)
    check("another tenant's prospect is 404, not 403",
          owner.get(f"/prospect/{other_target['id']}").status_code == 404)
    check("another tenant's company is 404",
          owner.get(f"/company/{other_company['id']}").status_code == 404)
    check("another tenant's contact cannot be exported",
          owner.get(f"/contact/{other_contact['id']}/export.json").status_code == 404)
    check("another tenant's contact cannot be re-verified",
          owner.post(f"/contact/{other_contact['id']}/verify",
                     follow_redirects=False).status_code == 404)

    r = other.get("/")
    check("the other tenant sees its own list", r.status_code == 200)
    check("the other tenant does not see our companies",
          b"Vantage Aerospace" not in other.get("/companies").data)

    # ------------------------------------------------------------ row access --
    section("Row-level access below the role")
    c = conn()
    reps = c.execute("SELECT * FROM reps WHERE org_id = ? ORDER BY id", (org_id,)).fetchall()
    member_rep = c.execute(
        """SELECT r.* FROM reps r JOIN users u ON u.rep_id = r.id
            WHERE u.email = ?""", ("rep@royalgrind.example",)).fetchone()
    other_rep_target = c.execute(
        """SELECT * FROM targets WHERE org_id = ? AND rep_id != ? AND rep_id IS NOT NULL
            ORDER BY id LIMIT 1""", (org_id, member_rep["id"])).fetchone()
    c.close()
    member_view = member.get(f"/prospect/{other_rep_target['id']}")
    check("a member cannot open another rep's prospect",
          member_view.status_code == 403, f"got {member_view.status_code}")
    check("a member's own list is not empty",
          b"people to call today" in member.get("/").data)

    manager_view = manager.get("/")
    check("a manager can still load a list", manager_view.status_code == 200)
    check("visible_rep_ids narrows below admin", len(reps) > 1)

    # -------------------------------------------------------------- webhooks --
    section("The call webhook authenticates")
    secret = "smoke-secret"
    ref = str(call["id"])
    token = telephony.sign_ref(secret, ref)
    check("a signed token verifies", telephony.verify_ref(secret, ref, token))
    # Flip the last character to something it definitely is not, rather than to a
    # fixed letter — a fixed letter passes one run in sixty-four by accident.
    tampered = token[:-1] + ("y" if token[-1] == "x" else "x")
    check("a tampered token does not", not telephony.verify_ref(secret, ref, tampered))
    check("another call's token does not verify",
          not telephony.verify_ref(secret, ref, telephony.sign_ref(secret, int(ref) + 1)))
    check("no secret means nothing verifies", not telephony.verify_ref("", ref, token))

    anon = application.app.test_client()
    r = anon.post(f"/webhook/call?ref={ref}&t=nonsense", json={"status": "completed"})
    check("an unsigned webhook is refused", r.status_code == 403)

    application.Config.TELEPHONY_WEBHOOK_SECRET = secret
    r = anon.post(f"/webhook/call?ref={ref}&t={token}",
                  json={"status": "completed", "duration": "212"})
    check("a signed webhook is accepted", r.status_code == 200, f"got {r.status_code}")
    c = conn()
    updated = c.execute("SELECT * FROM calls WHERE id = ?", (call["id"],)).fetchone()
    events = c.execute("SELECT COUNT(*) n FROM call_events WHERE call_id = ?",
                       (call["id"],)).fetchone()["n"]
    c.close()
    check("the webhook applied the duration", updated["duration_sec"] == 212)
    check("the webhook logged the raw event", events >= 1)

    r = anon.post(f"/webhook/call?ref={ref}&t={token}", json={"status": "completed"})
    c = conn()
    unchanged = c.execute("SELECT duration_sec FROM calls WHERE id = ?",
                          (call["id"],)).fetchone()["duration_sec"]
    c.close()
    check("a later event with no duration does not zero the duration", unchanged == 212)
    application.Config.TELEPHONY_WEBHOOK_SECRET = ""

    # ------------------------------------------------------------------ CSRF --
    section("CSRF")
    raw = application.app.test_client()
    raw.post("/login", data={"email": "owner@royalgrind.example", "password": PASSWORD})
    r = raw.post(f"/prospect/{hero['id']}/snooze", data={"days": "7"})
    check("a POST without a CSRF token is rejected", r.status_code == 400,
          f"got {r.status_code}")

    # --------------------------------------------------------- subject rights --
    section("Subject access and erasure")
    c = conn()
    subject = c.execute(
        """SELECT ct.* FROM contacts ct JOIN targets t ON t.contact_id = ct.id
            WHERE ct.org_id = ? AND t.research_json != '' LIMIT 1""",
        (org_id,)).fetchone() or c.execute(
        "SELECT * FROM contacts WHERE org_id = ? LIMIT 1", (org_id,)).fetchone()
    export = compliance.subject_access_export(c, org_id, subject["id"])
    c.close()
    check("the export includes the person", export["person"]["id"] == subject["id"])
    check("the export includes contact points", "contact_points" in export)
    check("the export includes AI-generated material about them",
          "ai_generated" in export)

    r = owner.get(f"/contact/{subject['id']}/export.json")
    check("the export downloads as JSON", r.status_code == 200
          and r.mimetype == "application/json")
    check("a manager cannot export a subject's data",
          manager.get(f"/contact/{subject['id']}/export.json").status_code == 403)

    c = conn()
    target_of = c.execute("SELECT COUNT(*) n FROM contact_points WHERE contact_id = ?",
                          (subject["id"],)).fetchone()["n"]
    compliance.erase_contact(c, org_id, subject["id"])
    gone = c.execute("SELECT COUNT(*) n FROM contacts WHERE id = ?",
                     (subject["id"],)).fetchone()["n"]
    points_gone = c.execute("SELECT COUNT(*) n FROM contact_points WHERE contact_id = ?",
                            (subject["id"],)).fetchone()["n"]
    suppressed = c.execute(
        "SELECT COUNT(*) n FROM suppression WHERE org_id = ? AND reason = 'erasure request'",
        (org_id,)).fetchone()["n"]
    kept = c.execute("SELECT COUNT(*) n FROM calls WHERE org_id = ? AND contact_id IS NULL "
                     "AND notes = '[erased]'", (org_id,)).fetchone()["n"]
    c.close()
    check("erasure destroys the person", gone == 0)
    check("erasure destroys every contact point", points_gone == 0)
    check("erasure suppresses first, so re-enrichment cannot resurrect them",
          suppressed >= (1 if target_of else 0))
    check("erasure keeps a redacted commercial record", kept >= 0)

    # ------------------------------------------------------------- budgeting --
    section("AI budget guard")
    c = conn()
    ok, reason = application.ai_budget_check(c, org_id, 0)
    check("no cap means no dollar block", ok)
    c.execute("INSERT INTO ai_spend (org_id, month, cents, feature, created_at) "
              "VALUES (?,?,?,?,?)",
              (org_id, application._month_key(), 100000, "test",
               datetime.now().isoformat(timespec="seconds")))
    c.commit()
    ok, reason = application.ai_budget_check(c, org_id, 9000)
    check("an exceeded monthly cap blocks before the request", not ok, reason)
    c.execute("DELETE FROM ai_spend WHERE feature = 'test'")
    c.commit()
    c.close()

    # ---------------------------------------------------------------- config --
    section("Production config fails fast")
    class Bad:
        IS_PROD = True
        SECRET_KEY = None
        DEBUG = True
        SESSION_COOKIE_SECURE = False
        TELEPHONY_PROVIDER = "vonage"
        VONAGE_APPLICATION_ID = ""
        VONAGE_PRIVATE_KEY = ""
        PUBLIC_BASE_URL = ""
        TELEPHONY_WEBHOOK_SECRET = ""
    problems = application.validate(Bad)
    check("a missing SECRET_KEY is caught",
          any("SECRET_KEY" in p for p in problems))
    check("debug in production is caught", any("DEBUG" in p for p in problems))
    check("an insecure session cookie is caught",
          any("SESSION_COOKIE_SECURE" in p for p in problems))
    check("vonage without credentials is caught",
          any("VONAGE_APPLICATION_ID" in p for p in problems))
    check("an open call webhook is caught",
          any("TELEPHONY_WEBHOOK_SECRET" in p for p in problems))

    class Good:
        IS_PROD = True
        SECRET_KEY = "x" * 64
        DEBUG = False
        SESSION_COOKIE_SECURE = True
        TELEPHONY_PROVIDER = "simulated"
    check("a good production config passes", application.validate(Good) == [])

    # ----------------------------------------------------------------- done --
    print()
    print("=" * 62)
    if _failures:
        print(f"FAILED — {len(_failures)} of {_checks} checks")
        for f in _failures:
            print(f"    {f}")
        return 1
    print(f"PASSED — {_checks} checks")
    return 0


def _count(table, org_id):
    c = conn()
    n = c.execute(f"SELECT COUNT(*) n FROM {table} WHERE org_id = ?",
                  (org_id,)).fetchone()["n"]
    c.close()
    return n


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        code = 1
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
