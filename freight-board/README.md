# FreightFlip — the reverse load board

**Drivers post the truck. Brokers compete for it.**

Today's load boards make drivers hunt through thousands of loads and negotiate
from a position of weakness. FreightFlip flips the marketplace: a driver posts
where their truck will be empty, when, and their rate floor — and verified
brokers bring their best freight *to the driver*.

## Run the demo

```bash
python3 app.py
```

Open http://localhost:5001

## The 2-minute pitch demo (for Friday)

1. **Home page** — the concept in one screen, with live marketplace stats.
2. **Driver dashboard** (`/driver`) — post a truck: city, date, rate floor,
   max deadhead. Below it: every load on the market that already meets the
   driver's terms, ranked best $/mile first. Bad-paying and wrong-equipment
   freight never appears.
3. **Broker dashboard** (`/broker`) — brokers see real empty trucks before
   competitors do, and bid a load directly onto one (they can offer ABOVE
   list rate to win the truck).
4. **Back to driver** — the bid appears with the broker's bond-verification
   badge. One click: **Accept** → load booked, all other bids auto-declined.

That accept click is the product: the driver never searched, never cold-called,
never took less than their floor.

## Stack

- Python 3 / Flask
- SQLite (`schema.sql` — drivers, trucks, capacity_posts, brokers, loads, bids)
- `freight_db.py` — database setup, seeding, and the core matching query
- Delete `freightflip.db` to reset the demo to fresh seed data.

## Roadmap (post-prototype)

- Real accounts and logins for drivers and brokers
- Live FMCSA SAFER API verification of MC numbers and broker bonds
- Zip-code geography: true deadhead-radius matching instead of state matching
- Notifications (SMS/email) when a bid lands
- Revenue: free for drivers; brokers pay per booked load or subscription
