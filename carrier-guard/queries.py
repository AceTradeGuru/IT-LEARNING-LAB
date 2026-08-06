"""
CarrierGuard — shared read queries.

Read-side queries used by more than one route live here so the routes stay
about HTTP and the SQL stays reviewable in one place.
"""


def network_stats(conn):
    """
    Headline numbers for the landing page and the network feed.

    One round trip rather than seven: these render on the busiest page in the
    app, and seven sequential queries is seven times the latency for no benefit.
    """
    return conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM checks)                                   AS checks,
          (SELECT COUNT(*) FROM checks WHERE verdict = 'block')           AS blocked,
          (SELECT COALESCE(SUM(load_value_usd), 0) FROM checks
             WHERE verdict = 'block')                                     AS value_protected,
          (SELECT COUNT(*) FROM carriers)                                 AS carriers,
          (SELECT COUNT(*) FROM organizations WHERE status = 'active')    AS brokers,
          (SELECT COUNT(*) FROM fraud_reports)                            AS reports,
          (SELECT COALESCE(SUM(amount_lost_usd), 0) FROM fraud_reports
             WHERE verified)                                              AS losses
    """).fetchone()


def carrier_staleness(conn, stale_days):
    """
    How many carrier records the ingest job has not refreshed recently.

    A fraud tool serving month-old authority data is worse than no tool, because
    the broker trusts it. Surfacing this is what keeps a silent ingest failure
    from becoming a wrong CLEAR verdict.
    """
    return conn.execute("""
        SELECT COUNT(*) FILTER (WHERE fetched_at < now() - make_interval(days => %s))
                 AS stale,
               COUNT(*) AS total,
               MAX(fetched_at) AS last_refresh
        FROM carriers
    """, (stale_days,)).fetchone()
