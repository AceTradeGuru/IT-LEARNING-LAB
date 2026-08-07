"""
CarrierGuard — security regression tests.

    python3 test_security.py     (exits non-zero on failure)

Runs against the local development database and the Flask test client.

These cover the controls that are invisible when they work and catastrophic
when they silently stop working — which is exactly the combination that makes
them worth pinning down in tests rather than checking by hand once.
"""

import concurrent.futures as cf
import sys

import db
import security
from app import app

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        FAILS.append(label)


def clear_buckets(prefix):
    with db.connection() as conn:
        conn.execute("DELETE FROM rate_limits WHERE bucket LIKE %s", (f"{prefix}%",))


def token_from(html):
    marker = 'name="_csrf" value="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


# ---------------------------------------------------------------- CSRF

def test_csrf():
    print("=== CSRF ===")
    with app.test_client() as client:
        page = client.get("/login").get_data(as_text=True)
        token = token_from(page)
        check("token minted into the form", len(token) > 20, True)

        r = client.post("/login", data={"email": "ops@peachstate.example",
                                        "password": "carrierguard-demo"})
        check("POST without token rejected", r.status_code, 400)

        r = client.post("/login", data={"email": "ops@peachstate.example",
                                        "password": "carrierguard-demo",
                                        "_csrf": "forged-value"})
        check("POST with forged token rejected", r.status_code, 400)

        r = client.post("/login", data={"email": "ops@peachstate.example",
                                        "password": "carrierguard-demo",
                                        "_csrf": token})
        check("POST with valid token accepted", r.status_code, 302)

    with app.test_client() as client:
        # The API authenticates by bearer token, which browsers never attach on
        # their own — so it must NOT be gated on a cookie-bound CSRF token, or
        # every legitimate integration would break.
        r = client.post("/api/v1/checks", json={"mc_number": "MC-471902"})
        check("API not blocked by CSRF (401 not 400)", r.status_code, 401)


# ---------------------------------------------------------------- headers

def test_headers():
    print("=== Response headers ===")
    with app.test_client() as client:
        h = client.get("/").headers
        csp = h.get("Content-Security-Policy", "")
        check("script-src 'none'", "script-src 'none'" in csp, True)
        check("frame-ancestors 'none'", "frame-ancestors 'none'" in csp, True)
        check("object-src 'none'", "object-src 'none'" in csp, True)
        check("nosniff", h.get("X-Content-Type-Options"), "nosniff")
        check("X-Frame-Options", h.get("X-Frame-Options"), "DENY")
        check("Referrer-Policy", h.get("Referrer-Policy"),
              "strict-origin-when-cross-origin")
        # Verdict pages name a carrier and reveal a broker's intent; a shared
        # proxy caching one and serving it to another subscriber would be both
        # a tenancy breach and a wrong answer.
        check("HTML is not cacheable", "no-store" in h.get("Cache-Control", ""), True)


# ---------------------------------------------------------------- rate limits

def test_rate_limit_atomicity():
    print("=== Rate limiter ===")
    bucket, limit = "test:atomic", 10
    clear_buckets("test:")

    def one(_):
        with db.connection() as conn:
            allowed, _, _ = security.hit(conn, bucket, limit, 60)
            return allowed

    with cf.ThreadPoolExecutor(max_workers=10) as pool:
        admitted = sum(pool.map(one, range(50)))

    # Read-then-write would let concurrent requests both see a count under the
    # limit and both proceed — precisely the race a credential-stuffing script
    # creates on purpose. One atomic upsert is what prevents over-admission.
    check("50 concurrent, exactly the limit admitted", admitted, limit)
    clear_buckets("test:")


def test_rate_limit_window_resets():
    bucket = "test:window"
    clear_buckets("test:")
    with db.connection() as conn:
        for _ in range(3):
            security.hit(conn, bucket, 2, 60)
        allowed, count, _ = security.hit(conn, bucket, 2, 60)
        check("blocked while window is open", allowed, False)

        # Age the window rather than sleeping through it.
        conn.execute("UPDATE rate_limits SET window_start = now() - interval '2 hours' "
                     "WHERE bucket = %s", (bucket,))
        allowed, count, _ = security.hit(conn, bucket, 2, 60)
        check("counter resets in a new window", (allowed, count), (True, 1))
    clear_buckets("test:")


def test_login_throttle():
    clear_buckets("login_")
    with app.test_client() as client:
        token = token_from(client.get("/login").get_data(as_text=True))
        codes = [client.post("/login", data={"email": "throttle@example.test",
                                             "password": f"wrong{i}",
                                             "_csrf": token}).status_code
                 for i in range(1, 15)]
    limit = security.LIMITS["login_email"][0]
    check("wrong passwords rejected at first", codes[0], 401)
    check("throttled before 14 attempts", 429 in codes, True)
    # A limit of N means N attempts are allowed and the N+1th is refused, so
    # the first 429 lands on attempt N+1. Asserting N here would be asserting
    # that the limiter is off by one in the strict direction.
    check("first refusal is one past the limit",
          codes.index(429) + 1, limit + 1)
    check("exactly the limit got through", codes.count(401), limit)
    clear_buckets("login_")


# ---------------------------------------------------------------- audit

def test_audit_trail():
    print("=== Audit trail ===")
    clear_buckets("login_")
    with db.connection() as conn:
        before = conn.execute(
            "SELECT count(*) AS n FROM audit_events WHERE action = 'login'").fetchone()["n"]

    with app.test_client() as client:
        token = token_from(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "ops@peachstate.example",
                                    "password": "definitely-wrong", "_csrf": token})

    with db.connection() as conn:
        row = conn.execute(
            """SELECT action, outcome, target, ip FROM audit_events
               WHERE action = 'login' ORDER BY id DESC LIMIT 1""").fetchone()
        after = conn.execute(
            "SELECT count(*) AS n FROM audit_events WHERE action = 'login'").fetchone()["n"]

    check("failed login recorded", after, before + 1)
    check("outcome", row["outcome"], "failed")
    check("target captured", row["target"], "ops@peachstate.example")
    check("source address captured", row["ip"] is not None, True)
    clear_buckets("login_")


def main():
    app.config["TESTING"] = True
    for fn in (test_csrf, test_headers, test_rate_limit_atomicity,
               test_rate_limit_window_resets, test_login_throttle, test_audit_trail):
        fn()

    db.close_pool()
    if FAILS:
        print(f"\n{len(FAILS)} FAILURES: {', '.join(FAILS)}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
