"""
CarrierGuard — ingest regression tests.

    python3 test_ingest.py     (exits non-zero on failure)

Runs against the local development database. Every source is faked, so no
network calls and no FMCSA credentials are needed.

The cases that matter most are 1, 2, and 6. Collapsing "we could not reach
FMCSA" into "this authority does not exist" would turn our own outage into a
fraud accusation and BLOCK legitimate carriers — so those three pin down that
an outage yields REVIEW, a genuine miss yields BLOCK, and a stale fallback
discloses its age at zero weight.
"""
import sys

from datetime import date, timedelta
import db, ingest
from ingest import CarrierSource, SourceUnavailable, resolve_carrier, run_ingest
from risk_engine import evaluate, run_check

class FakeGood(CarrierSource):
    name = "fake_good"
    def __init__(self): self.calls = []
    def fetch(self, mc):
        self.calls.append(mc)
        return {"mc_number": mc, "dot_number": "9998887", "legal_name": "Ridgeway Bulk Lines",
                "registered_email": "dispatch@ridgewaybulk.com",
                "registered_domain": "ridgewaybulk.com", "registered_phone": "6155550143",
                "registered_address": "700 Polk Ave, Nashville, TN",
                "authority_status": "active", "authority_granted": date.today()-timedelta(days=3000),
                "cargo_expires": date.today()+timedelta(days=200),
                "liability_expires": date.today()+timedelta(days=200),
                "cargo_coverage_usd": 100000, "power_units": 15, "oos_rate": 0.07,
                "last_inspection": date.today()-timedelta(days=30)}

class FakeMissing(CarrierSource):
    name = "fake_missing"
    def fetch(self, mc): return None

class FakeDown(CarrierSource):
    name = "fake_down"
    def fetch(self, mc): raise SourceUnavailable("upstream 503")

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok: fails.append(label)

UNKNOWN = "MC-909090"

print("=== 1. source down + unknown MC -> 'unavailable', REVIEW not BLOCK ===")
with db.connection() as conn:
    carrier, status = resolve_carrier(conn, UNKNOWN, source=FakeDown())
    check("status", status, "unavailable")
    r = evaluate(conn, 1, UNKNOWN, carrier=carrier, carrier_status=status)
    check("verdict", r["verdict"], "review")
    check("score", r["score"], 30)
    check("signal", r["signals"][0].code, "AUTHORITY_UNVERIFIED")

print("=== 2. source reachable, no such authority -> 'not_found', BLOCK ===")
with db.connection() as conn:
    carrier, status = resolve_carrier(conn, UNKNOWN, source=FakeMissing())
    check("status", status, "not_found")
    r = evaluate(conn, 1, UNKNOWN, carrier=carrier, carrier_status=status)
    check("verdict", r["verdict"], "block")
    check("signal", r["signals"][0].code, "AUTHORITY_NOT_FOUND")

print("=== 3. on-demand fetch creates the carrier ===")
with db.connection() as conn:
    conn.execute("DELETE FROM carriers WHERE mc_number = %s", (UNKNOWN,))
    src = FakeGood()
    carrier, status = resolve_carrier(conn, UNKNOWN, source=src)
    check("status", status, "found")
    check("legal_name", carrier["legal_name"], "Ridgeway Bulk Lines")
    check("source recorded", carrier["source"], "fake_good")
    check("upstream called once", len(src.calls), 1)

print("=== 4. fresh record is NOT refetched (cache honored) ===")
with db.connection() as conn:
    src = FakeGood()
    carrier, status = resolve_carrier(conn, UNKNOWN, source=src)
    check("status", status, "found")
    check("upstream not called", len(src.calls), 0)

print("=== 5. stale record IS refetched ===")
with db.connection() as conn:
    conn.execute("UPDATE carriers SET fetched_at = now() - interval '10 days' WHERE mc_number=%s", (UNKNOWN,))
    src = FakeGood()
    carrier, status = resolve_carrier(conn, UNKNOWN, source=src)
    check("upstream called", len(src.calls), 1)

print("=== 6. outage falls back to the stale copy rather than crying fraud ===")
with db.connection() as conn:
    conn.execute("UPDATE carriers SET fetched_at = now() - interval '10 days' WHERE mc_number=%s", (UNKNOWN,))
    carrier, status = resolve_carrier(conn, UNKNOWN, source=FakeDown())
    check("status", status, "found")
    r = evaluate(conn, 1, UNKNOWN, carrier=carrier, carrier_status=status)
    codes = [s.code for s in r["signals"]]
    check("staleness disclosed", "RECORD_STALE" in codes, True)
    check("stale weight is zero", [s.weight for s in r["signals"] if s.code=="RECORD_STALE"][0], 0)

print("=== 7. run_ingest logs the run ===")
s = run_ingest(mode="single", mc_numbers=[UNKNOWN], source=FakeGood())
check("status", s["status"], "ok")
check("updated", s["carriers_updated"], 1)
s2 = run_ingest(mode="single", mc_numbers=[UNKNOWN], source=FakeDown())
check("outage recorded as failed", s2["status"], "failed")
check("outage message kept", bool(s2["message"]), True)

print("=== 8. partial payload does not blank existing data ===")
class FakePartial(CarrierSource):
    name = "fake_partial"
    def fetch(self, mc):
        return {"mc_number": mc, "legal_name": "Ridgeway Bulk Lines (renamed)"}
with db.connection() as conn:
    before = conn.execute("SELECT cargo_expires, registered_phone FROM carriers WHERE mc_number=%s", (UNKNOWN,)).fetchone()
    ingest.upsert_carrier(conn, FakePartial().fetch(UNKNOWN), "fake_partial")
    after = conn.execute("SELECT legal_name, cargo_expires, registered_phone FROM carriers WHERE mc_number=%s", (UNKNOWN,)).fetchone()
    check("name updated", after["legal_name"], "Ridgeway Bulk Lines (renamed)")
    check("insurance preserved", after["cargo_expires"], before["cargo_expires"])
    check("phone preserved", after["registered_phone"], before["registered_phone"])

with db.connection() as conn:
    conn.execute("DELETE FROM carriers WHERE mc_number = %s", (UNKNOWN,))
db.close_pool()
print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
