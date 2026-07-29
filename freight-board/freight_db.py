"""
FreightFlip — reverse load board database.

Builds the SQLite database from schema.sql, seeds it with sample data,
and demos the core query: "show a driver every load that matches
their posted capacity, best-paying first."

Run it:
    python3 freight_db.py
"""

import sqlite3
from pathlib import Path

DB_FILE = Path(__file__).parent / "freightflip.db"
SCHEMA_FILE = Path(__file__).parent / "schema.sql"


def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row          # lets us access columns by name
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables(conn):
    conn.executescript(SCHEMA_FILE.read_text())
    conn.commit()
    print(f"Tables created in {DB_FILE.name}")


def seed_sample_data(conn):
    """Insert a few drivers, brokers, loads, and posts so we can query."""
    if conn.execute("SELECT COUNT(*) FROM drivers").fetchone()[0] > 0:
        print("Database already seeded — skipping.")
        return

    cur = conn.cursor()

    cur.execute(
        "INSERT INTO drivers (name, email, cdl_number, home_city, home_state) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Anthony R.", "anthony@example.com", "GA1234567", "Atlanta", "GA"),
    )
    driver_id = cur.lastrowid

    cur.execute(
        "INSERT INTO trucks (driver_id, equipment_type, max_weight_lbs, length_ft) "
        "VALUES (?, ?, ?, ?)",
        (driver_id, "dry_van", 45000, 53),
    )
    truck_id = cur.lastrowid

    # Anthony posts his truck: empty in Atlanta on Aug 1, wants $2.50/mi minimum.
    cur.execute(
        "INSERT INTO capacity_posts "
        "(driver_id, truck_id, origin_city, origin_state, available_date, "
        " min_rate_per_mile, max_deadhead_mi) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (driver_id, truck_id, "Atlanta", "GA", "2026-08-01", 2.50, 150),
    )

    brokers = [
        ("Peach State Logistics", "ops@peachstate.example", "MC-111222", 1),
        ("BlueLine Freight", "dispatch@blueline.example", "MC-333444", 1),
    ]
    broker_ids = []
    for company, email, mc, bonded in brokers:
        cur.execute(
            "INSERT INTO brokers (company_name, email, mc_number, bond_verified) "
            "VALUES (?, ?, ?, ?)",
            (company, email, mc, bonded),
        )
        broker_ids.append(cur.lastrowid)

    # Loads out of (or near) Georgia around Aug 1 — some pay, some don't.
    loads = [
        # broker,        o_city,     o_st, d_city,      d_st, pickup,       equip,     lbs,   miles, rate
        (broker_ids[0], "Atlanta",   "GA", "Nashville", "TN", "2026-08-01", "dry_van", 38000, 250,  750.0),
        (broker_ids[0], "Atlanta",   "GA", "Chicago",   "IL", "2026-08-01", "dry_van", 42000, 715, 2145.0),
        (broker_ids[1], "Macon",     "GA", "Dallas",    "TX", "2026-08-02", "dry_van", 40000, 830, 1660.0),
        (broker_ids[1], "Atlanta",   "GA", "Miami",     "FL", "2026-08-01", "reefer",  36000, 660, 2300.0),
    ]
    cur.executemany(
        "INSERT INTO loads (broker_id, origin_city, origin_state, dest_city, "
        "dest_state, pickup_date, equipment_type, weight_lbs, distance_miles, offered_rate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        loads,
    )

    conn.commit()
    print("Sample data seeded.")


def matching_loads_for_post(conn, post_id):
    """
    THE core query of the reverse board: given a driver's capacity post,
    find every available load that matches their equipment, timing,
    and rate floor — sorted by rate per mile, best first.
    """
    return conn.execute(
        """
        SELECT l.origin_city || ', ' || l.origin_state AS origin,
               l.dest_city   || ', ' || l.dest_state   AS destination,
               l.pickup_date,
               l.distance_miles,
               l.offered_rate,
               ROUND(l.offered_rate / l.distance_miles, 2) AS rate_per_mile,
               b.company_name,
               b.bond_verified
        FROM capacity_posts cp
        JOIN trucks  t ON t.id = cp.truck_id
        JOIN loads   l ON l.status = 'available'
                      AND l.equipment_type = t.equipment_type
                      AND l.weight_lbs <= t.max_weight_lbs
                      AND l.origin_state = cp.origin_state
                      AND l.pickup_date >= cp.available_date
                      AND (l.offered_rate / l.distance_miles) >= cp.min_rate_per_mile
                      AND (cp.dest_pref_state IS NULL
                           OR l.dest_state = cp.dest_pref_state)
        JOIN brokers b ON b.id = l.broker_id
        WHERE cp.id = ?
        ORDER BY rate_per_mile DESC
        """,
        (post_id,),
    ).fetchall()


def main():
    conn = get_connection()
    create_tables(conn)
    seed_sample_data(conn)

    print("\n=== Loads matching capacity post #1 (best rate first) ===")
    rows = matching_loads_for_post(conn, 1)
    if not rows:
        print("No matching loads.")
    for r in rows:
        bonded = "bonded" if r["bond_verified"] else "NOT bonded"
        print(
            f"  {r['origin']} -> {r['destination']}  "
            f"{r['distance_miles']} mi  ${r['offered_rate']:,.0f}  "
            f"(${r['rate_per_mile']}/mi)  {r['company_name']} [{bonded}]"
        )

    conn.close()


if __name__ == "__main__":
    main()
