"""
FreightFlip — reverse load board web app (demo prototype).

Drivers post their truck; brokers compete for it.

Run:
    python3 app.py
Then open http://localhost:5001
"""

from flask import Flask, render_template, request, redirect, url_for, flash
from freight_db import get_connection, create_tables, seed_sample_data, matching_loads_for_post

app = Flask(__name__)
app.secret_key = "freightflip-demo"

# Demo mode: we act as driver #1 (Anthony) or a chosen broker.
DEMO_DRIVER_ID = 1


def db():
    return get_connection()


# ---------- Landing ----------

@app.route("/")
def index():
    conn = db()
    stats = {
        "drivers": conn.execute("SELECT COUNT(*) FROM drivers").fetchone()[0],
        "brokers": conn.execute("SELECT COUNT(*) FROM brokers").fetchone()[0],
        "open_posts": conn.execute(
            "SELECT COUNT(*) FROM capacity_posts WHERE status='open'").fetchone()[0],
        "loads": conn.execute(
            "SELECT COUNT(*) FROM loads WHERE status='available'").fetchone()[0],
    }
    conn.close()
    return render_template("index.html", stats=stats)


# ---------- Driver side ----------

@app.route("/driver")
def driver_dashboard():
    conn = db()
    driver = conn.execute("SELECT * FROM drivers WHERE id=?", (DEMO_DRIVER_ID,)).fetchone()
    trucks = conn.execute("SELECT * FROM trucks WHERE driver_id=?", (DEMO_DRIVER_ID,)).fetchall()
    posts = conn.execute(
        """SELECT cp.*, t.equipment_type FROM capacity_posts cp
           JOIN trucks t ON t.id = cp.truck_id
           WHERE cp.driver_id=? ORDER BY cp.created_at DESC""",
        (DEMO_DRIVER_ID,)).fetchall()

    posts_data = []
    for p in posts:
        matches = matching_loads_for_post(conn, p["id"]) if p["status"] == "open" else []
        bids = conn.execute(
            """SELECT bd.*, b.company_name, b.bond_verified, b.rating,
                      l.origin_city, l.origin_state, l.dest_city, l.dest_state,
                      l.distance_miles, l.pickup_date
               FROM bids bd
               JOIN brokers b ON b.id = bd.broker_id
               JOIN loads l   ON l.id = bd.load_id
               WHERE bd.capacity_post_id=? ORDER BY bd.offered_rate DESC""",
            (p["id"],)).fetchall()
        posts_data.append({"post": p, "matches": matches, "bids": bids})

    conn.close()
    return render_template("driver.html", driver=driver, trucks=trucks, posts_data=posts_data)


@app.route("/driver/post", methods=["POST"])
def create_capacity_post():
    f = request.form
    conn = db()
    conn.execute(
        """INSERT INTO capacity_posts
           (driver_id, truck_id, origin_city, origin_state, available_date,
            dest_pref_state, min_rate_per_mile, max_deadhead_mi)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (DEMO_DRIVER_ID, f["truck_id"], f["origin_city"].strip(),
         f["origin_state"].strip().upper(), f["available_date"],
         f["dest_pref_state"].strip().upper() or None,
         float(f["min_rate_per_mile"]), int(f["max_deadhead_mi"] or 100)))
    conn.commit()
    conn.close()
    flash("Truck posted. Brokers can now find and bid on your capacity.")
    return redirect(url_for("driver_dashboard"))


@app.route("/bid/<int:bid_id>/<action>", methods=["POST"])
def respond_to_bid(bid_id, action):
    if action not in ("accepted", "declined"):
        return "Bad action", 400
    conn = db()
    bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    if bid:
        conn.execute("UPDATE bids SET status=? WHERE id=?", (action, bid_id))
        if action == "accepted":
            conn.execute("UPDATE loads SET status='booked' WHERE id=?", (bid["load_id"],))
            conn.execute("UPDATE capacity_posts SET status='booked' WHERE id=?",
                         (bid["capacity_post_id"],))
            conn.execute(
                "UPDATE bids SET status='declined' WHERE capacity_post_id=? AND id!=? "
                "AND status='pending'", (bid["capacity_post_id"], bid_id))
            flash("Load booked! Rate confirmed at your terms.")
        else:
            flash("Bid declined.")
    conn.commit()
    conn.close()
    return redirect(url_for("driver_dashboard"))


# ---------- Broker side ----------

@app.route("/broker")
def broker_dashboard():
    conn = db()
    brokers = conn.execute("SELECT * FROM brokers ORDER BY company_name").fetchall()
    broker_id = int(request.args.get("broker_id", brokers[0]["id"]))
    broker = conn.execute("SELECT * FROM brokers WHERE id=?", (broker_id,)).fetchone()

    loads = conn.execute(
        "SELECT * FROM loads WHERE broker_id=? ORDER BY created_at DESC", (broker_id,)).fetchall()

    # The marketplace flip: brokers browse available TRUCKS.
    open_posts = conn.execute(
        """SELECT cp.*, d.name AS driver_name, d.rating, t.equipment_type,
                  t.max_weight_lbs, t.length_ft
           FROM capacity_posts cp
           JOIN drivers d ON d.id = cp.driver_id
           JOIN trucks t  ON t.id = cp.truck_id
           WHERE cp.status='open' ORDER BY cp.available_date""").fetchall()

    my_available_loads = [l for l in loads if l["status"] in ("available", "bid_out")]
    conn.close()
    return render_template("broker.html", broker=broker, brokers=brokers,
                           loads=loads, open_posts=open_posts,
                           my_available_loads=my_available_loads)


@app.route("/broker/load", methods=["POST"])
def create_load():
    f = request.form
    conn = db()
    conn.execute(
        """INSERT INTO loads (broker_id, origin_city, origin_state, dest_city,
             dest_state, pickup_date, equipment_type, weight_lbs,
             distance_miles, offered_rate, commodity)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (f["broker_id"], f["origin_city"].strip(), f["origin_state"].strip().upper(),
         f["dest_city"].strip(), f["dest_state"].strip().upper(), f["pickup_date"],
         f["equipment_type"], int(f["weight_lbs"]), int(f["distance_miles"]),
         float(f["offered_rate"]), f.get("commodity", "").strip()))
    conn.commit()
    conn.close()
    flash("Load posted.")
    return redirect(url_for("broker_dashboard", broker_id=f["broker_id"]))


@app.route("/broker/bid", methods=["POST"])
def place_bid():
    f = request.form
    conn = db()
    try:
        conn.execute(
            """INSERT INTO bids (load_id, capacity_post_id, broker_id, offered_rate, message)
               VALUES (?, ?, ?, ?, ?)""",
            (f["load_id"], f["capacity_post_id"], f["broker_id"],
             float(f["offered_rate"]), f.get("message", "").strip()))
        conn.execute("UPDATE loads SET status='bid_out' WHERE id=? AND status='available'",
                     (f["load_id"],))
        conn.commit()
        flash("Bid sent to the driver.")
    except Exception:
        flash("You already bid this load on that truck.")
    conn.close()
    return redirect(url_for("broker_dashboard", broker_id=f["broker_id"]))


if __name__ == "__main__":
    conn = get_connection()
    create_tables(conn)
    seed_sample_data(conn)
    conn.close()
    app.run(debug=True, port=5001)
