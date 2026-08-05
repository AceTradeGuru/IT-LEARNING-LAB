"""
CarrierGuard — database setup and demo seed.

Builds the SQLite database from schema.sql and seeds it with seven carriers
that between them cover every way a carrier goes bad:

    MC-284561  Redline Carriers          clean, 15 years, boring — the control
    MC-471902  Great Lakes Freight       clean — and the impersonation TARGET
    MC-901347  Summit Haul Transport     brand-new authority, insurance lapsing
    MC-338215  Ironclad Logistics        authority revoked, insurance expired
    MC-660188  Cascade Motor Lines       dormant shell, suddenly booking freight
    MC-775410  Bayou Express Carriers    clean on paper — rotating identities
    MC-512889  Trans-Nation Hauling      clean on paper — confirmed double-broker

The last two matter most. They are why a SAFER lookup is not a product: the
government record for both is spotless. Only the network knows.

Run it:
    python3 guard_db.py
"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from risk_engine import run_check

DB_FILE = Path(__file__).parent / "carrierguard.db"
SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# The demo broker: whoever is running the pitch.
DEMO_BROKER_ID = 1


def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row          # lets us access columns by name
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables(conn):
    conn.executescript(SCHEMA_FILE.read_text())
    conn.commit()


# Seed dates are always relative to today so the demo never goes stale.
def day(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


def stamp(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def seed_sample_data(conn):
    if conn.execute("SELECT COUNT(*) FROM carriers").fetchone()[0] > 0:
        print("Database already seeded — skipping.")
        return

    cur = conn.cursor()

    # ---------------------------------------------------------- subscribers
    cur.executemany(
        "INSERT INTO brokers (company_name, mc_number, contact_email, plan) "
        "VALUES (?, ?, ?, ?)",
        [
            ("Peach State Logistics",       "MC-111222", "ops@peachstate.example",        "pro"),
            ("BlueLine Freight",            "MC-333444", "dispatch@blueline.example",     "starter"),
            ("Continental Shippers Group",  "MC-555666", "traffic@continentalsg.example", "enterprise"),
            ("Gulf Coast Brokerage",        "MC-777888", "desk@gulfcoast.example",        "pro"),
        ])

    # ---------------------------------------------------------- FMCSA snapshot
    carriers = [
        # (mc, dot, legal_name, domain, email, phone, address, addr_type, phone_type,
        #  auth_status, granted, reinstatements, liab_exp, cargo_exp, coverage,
        #  power_units, oos_rate, last_inspection)

        # The control: everything a good carrier looks like.
        ("MC-284561", "1902445", "Redline Carriers LLC",
         "redlinecarriers.com", "dispatch@redlinecarriers.com", "4045550142",
         "1180 Fulton Industrial Blvd, Atlanta, GA", "commercial", "landline",
         "active", day(-5800), 0, day(240), day(240), 250000, 42, 0.08, day(-22)),

        # Real, clean, well-known — which is exactly why someone stole its identity.
        ("MC-471902", "2214870", "Great Lakes Freight Systems Inc",
         "greatlakesfreightsys.com", "dispatch@greatlakesfreightsys.com", "3135550188",
         "4400 W Jefferson Ave, Detroit, MI", "commercial", "landline",
         "active", day(-4300), 0, day(310), day(310), 250000, 68, 0.06, day(-11)),

        # Not fraud. Just genuinely risky: 90 days old, insurance about to lapse,
        # and the "terminal" is a mailbox store.
        ("MC-901347", "3908812", "Summit Haul Transport LLC",
         "summithaultransport.com", "dispatch@summithaultransport.com", "6025550117",
         "2201 E Camelback Rd Ste 405, Phoenix, AZ", "cmra", "mobile",
         "active", day(-90), 0, day(18), day(18), 100000, 3, 0.15, day(-40)),

        # Openly dead. Anyone tendering to this one wasn't checking at all.
        ("MC-338215", "1544091", "Ironclad Logistics Inc",
         "ironcladlogistics.net", "ops@ironcladlogistics.net", "7135550233",
         "8890 Wallisville Rd, Houston, TX", "commercial", "landline",
         "revoked", day(-2200), 3, day(-60), day(-60), 100000, 6, 0.31, day(-400)),

        # Dormant shell being revived — the MC number almost certainly changed hands.
        ("MC-660188", "1731266", "Cascade Motor Lines",
         "cascademotorlines.com", "cascade.dispatch@cascademotorlines.com", "5035550166",
         "3127 SE Harrison St, Portland, OR", "residential", "landline",
         "active", day(-3300), 1, day(90), day(-15), 100000, 1, 0.0, day(-820)),

        # Spotless government record. Wearing a different face for every broker.
        ("MC-775410", "2087733", "Bayou Express Carriers LLC",
         "bayouexpresscarriers.com", "dispatch@bayouexpresscarriers.com", "5045550129",
         "6120 Chef Menteur Hwy, New Orleans, LA", "commercial", "landline",
         "active", day(-2600), 0, day(150), day(150), 100000, 12, 0.11, day(-35)),

        # Spotless government record. Already stole from two brokers on this network.
        ("MC-512889", "3122604", "Trans-Nation Hauling Co",
         "transnationhauling.com", "dispatch@transnationhauling.com", "2145550171",
         "2510 S Good Latimer Expy, Dallas, TX", "commercial", "landline",
         "active", day(-1500), 0, day(200), day(200), 100000, 9, 0.09, day(-18)),
    ]
    cur.executemany(
        """INSERT INTO carriers
             (mc_number, dot_number, legal_name, registered_domain, registered_email,
              registered_phone, registered_address, address_type, registered_phone_type,
              authority_status, authority_granted, reinstatement_count,
              liability_expires, cargo_expires, cargo_coverage_usd,
              power_units, oos_rate, last_inspection)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        carriers)

    # ------------------------------------------------- phone line intelligence
    # Stub for a Twilio Lookup / Ekata cache. VOIP is the tell that matters.
    cur.executemany(
        "INSERT INTO phone_intel (phone, line_type, carrier_name) VALUES (?, ?, ?)",
        [
            ("4045550142", "landline", "AT&T Southeast"),
            ("3135550188", "landline", "Ameritech"),
            ("6025550117", "mobile",   "T-Mobile US"),
            ("7135550233", "landline", "AT&T Texas"),
            ("5035550166", "landline", "CenturyLink"),
            ("5045550129", "landline", "AT&T Louisiana"),
            ("2145550171", "landline", "AT&T Texas"),
            # Disposable numbers used by the bad actors below.
            ("8725550417", "voip", "Bandwidth.com"),
            ("9295550388", "voip", "Twilio"),
            ("4705550912", "voip", "Telnyx"),
        ])

    conn.commit()

    # ------------------------------------------------------ history: real checks
    # Seeded by actually running the engine, backdated. This builds genuine
    # network memory rather than faking it — and self-tests the rules.
    history = [
        # (days_ago, broker, mc, email, phone, name, load_value)
        (45, 1, "MC-471902", "dispatch@greatlakesfreightsys.com", "3135550188", "Karen Vaughn", 62000),
        (30, 1, "MC-471902", "dispatch@greatlakesfreightsys.com", "3135550188", "Karen Vaughn", 41000),
        (25, 1, "MC-284561", "dispatch@redlinecarriers.com",      "4045550142", "Mike Delgado", 55000),
        (20, 2, "MC-471902", "dispatch@greatlakesfreightsys.com", "3135550188", "Karen Vaughn", 38000),
        (12, 1, "MC-471902", "dispatch@greatlakesfreightsys.com", "3135550188", "Karen Vaughn", 71000),
        (9,  1, "MC-284561", "dispatch@redlinecarriers.com",      "4045550142", "Mike Delgado", 47000),
        (4,  1, "MC-471902", "dispatch@greatlakesfreightsys.com", "3135550188", "Karen Vaughn", 58000),

        # Trans-Nation, checked before anyone knew. Came back clean. It wasn't.
        (33, 3, "MC-512889", "dispatch@transnationhauling.com",   "2145550171", "Rob Sandoval", 48200),

        # Bayou Express: one MC number, four dispatch identities, four brokers,
        # nineteen days. No single broker could have seen this.
        (19, 1, "MC-775410", "dispatch@bayouexpresscarriers.com",  "5045550129", "T. Boudreaux", 33000),
        (14, 2, "MC-775410", "bayouexp.dispatch@gmail.com",        "9295550388", "Terry B.",     44000),
        (9,  3, "MC-775410", "ops@bayou-express.net",              "4705550912", "Tee Boudreau", 51000),
        (5,  4, "MC-775410", "bayouexpress.logistics@outlook.com", "9295550388", "T Boudreaux",  39000),
    ]
    for days_ago, broker_id, mc, email, phone, name, value in history:
        run_check(conn, broker_id, mc, email, phone, name, value, at=stamp(days_ago))

    # ------------------------------------------------------------ what it cost
    # Filed after the fact — which is the whole point. The network learns from
    # the broker who paid for the lesson so nobody else has to.
    cur.executemany(
        """INSERT INTO fraud_reports
             (broker_id, mc_number, reported_email, reported_phone, incident_type,
              amount_lost_usd, notes, verified, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            (3, "MC-512889", "dispatch@transnationhauling.com", "2145550171",
             "double_brokered", 48200,
             "Accepted the tender, then re-brokered to an unvetted carrier. Freight "
             "delivered 6 days late; the hauling carrier was never paid and filed a "
             "lien against our customer.", 1, stamp(21)),
            (4, "MC-512889", "billing@transnationhauling.com", "2145550171",
             "payment_redirect", 12400,
             "Emailed updated remittance instructions mid-haul. Funds went to an "
             "account with no connection to the carrier.", 1, stamp(15)),
            (2, "MC-338215", "ops@ironcladlogistics.net", "7135550233",
             "no_show", 0,
             "Booked the load, never showed at the shipper, stopped answering. "
             "Authority turned out to be revoked.", 0, stamp(52)),
        ])

    conn.commit()
    print(f"Seeded {len(carriers)} carriers, {len(history)} historical checks, "
          f"3 fraud reports.")


def network_stats(conn):
    """Headline numbers for the landing page and the network feed."""
    row = conn.execute(
        """SELECT COUNT(*) AS checks,
                  SUM(verdict = 'block')  AS blocked,
                  SUM(CASE WHEN verdict = 'block' THEN COALESCE(load_value_usd, 0) END)
                      AS value_protected
           FROM checks""").fetchone()
    return {
        "checks": row["checks"] or 0,
        "blocked": row["blocked"] or 0,
        "value_protected": row["value_protected"] or 0,
        "carriers": conn.execute("SELECT COUNT(*) FROM carriers").fetchone()[0],
        "brokers": conn.execute("SELECT COUNT(*) FROM brokers").fetchone()[0],
        "reports": conn.execute("SELECT COUNT(*) FROM fraud_reports").fetchone()[0],
        "losses": conn.execute(
            "SELECT COALESCE(SUM(amount_lost_usd), 0) FROM fraud_reports "
            "WHERE verified = 1").fetchone()[0],
    }


def main():
    conn = get_connection()
    create_tables(conn)
    seed_sample_data(conn)

    print("\n=== Every seeded carrier, checked with its own contact of record ===")
    from risk_engine import evaluate
    for c in conn.execute("SELECT * FROM carriers ORDER BY mc_number").fetchall():
        r = evaluate(conn, DEMO_BROKER_ID, c["mc_number"],
                     c["registered_email"], c["registered_phone"], load_value=75000)
        top = r["signals"][0].label if r["signals"] else "nothing to report"
        print(f"  {c['mc_number']}  {r['verdict'].upper():<6} {r['score']:>3}  "
              f"{c['legal_name'][:34]:<36} {top}")

    print("\n=== The impersonation: same MC number, different human ===")
    r = evaluate(conn, DEMO_BROKER_ID, "MC-471902",
                 "dispatch@greatlakesfreight-sys.com", "(872) 555-0417", load_value=75000)
    print(f"  MC-471902  {r['verdict'].upper()}  score {r['score']}")
    for s in r["signals"]:
        print(f"    [{s.severity:<8}] {s.weight:+3d}  {s.label}")

    conn.close()


if __name__ == "__main__":
    main()
