"""
CarrierGuard — carrier data ingest.

Pulls authority, insurance, and safety data into the `carriers` table, which is
what every FMCSA-sourced signal in the risk engine reads.

    python3 ingest.py --stale          # nightly: refresh what's gone stale
    python3 ingest.py --mc MC-471902   # one carrier
    python3 ingest.py --stale --dry-run

Design notes:

**Everything goes through a source adapter.** `FmcsaQcMobileSource` talks to the
real API; `UnavailableSource` is what you get with no credentials configured. The
adapter interface is two methods, and the field mapping lives in exactly one
function, because that mapping is the part most likely to be wrong until it has
been run against a live payload.

**"Not found" and "could not check" are different answers.** This matters more
than it sounds. In the seeded prototype, an MC number missing from the table
meant the authority didn't exist — a 55-point critical finding. Against real
data that inference is invalid: a perfectly legitimate carrier is missing simply
because nobody has looked it up yet. Reporting an outage as a fraud signal would
BLOCK real carriers and destroy trust in the product on day one. So an
unreachable source produces a *review*, not a *block*, and says which it is.
"""

import argparse
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

import db
from config import load
from security import purge_rate_limits

log = logging.getLogger("carrierguard.ingest")

cfg = load()

HTTP_TIMEOUT = 15
HTTP_RETRIES = 3
USER_AGENT = "CarrierGuard/1.0 (+carrier vetting)"

# Refresh anything older than this when running --stale.
DEFAULT_STALE_HOURS = 24


class SourceUnavailable(Exception):
    """The upstream could not be reached or refused us. Not the same as 'no such carrier'."""


# ---------------------------------------------------------------- adapters

class CarrierSource:
    """Interface every source implements."""

    name = "base"

    def fetch(self, mc_number):
        """
        Return a normalized carrier dict, or None if the authority genuinely
        does not exist upstream. Raise SourceUnavailable if we could not ask.
        """
        raise NotImplementedError


class UnavailableSource(CarrierSource):
    """
    What you get with no FMCSA credentials configured.

    Deliberately raises rather than returning None. Returning None would tell
    the risk engine "this authority does not exist", which is a fabrication —
    we never asked. Callers must be able to tell the difference.
    """

    name = "unavailable"

    def fetch(self, mc_number):
        raise SourceUnavailable(
            "No FMCSA credentials configured (set FMCSA_WEBKEY).")


class FmcsaQcMobileSource(CarrierSource):
    """
    FMCSA QCMobile API.

    Requires a web key from FMCSA's developer portal, passed as `webKey`.

    The field mapping in `_normalize` is the part to verify first against a real
    response — it is written defensively (every field optional, every conversion
    guarded) so an unexpected payload degrades to missing fields rather than a
    crashed nightly job, but the names themselves need confirming once you have
    a key. That is the only function that should need to change.
    """

    name = "fmcsa_qcmobile"
    BASE = "https://mobile.fmcsa.dot.gov/qc/services/carriers"

    def __init__(self, web_key):
        if not web_key:
            raise ValueError("FmcsaQcMobileSource requires a web key")
        self.web_key = web_key

    def fetch(self, mc_number):
        docket = _docket_digits(mc_number)
        if not docket:
            return None

        payload = self._get(f"{self.BASE}/docket-number/{docket}")
        record = _first_carrier(payload)
        if record is None:
            return None

        normalized = self._normalize(record, mc_number)

        # Authority and insurance live on separate sub-resources. A failure here
        # downgrades the record rather than discarding it: stale insurance data
        # is worse than none, so those fields stay absent and the carrier keeps
        # whatever it had, with fetched_at telling the truth about freshness.
        dot = normalized.get("dot_number")
        if dot:
            try:
                authority = self._get(f"{self.BASE}/{dot}/authority")
                normalized.update(_normalize_authority(authority))
            except SourceUnavailable:
                log.warning("authority sub-fetch failed for %s", mc_number)

        return normalized

    def _get(self, url):
        query = urllib.parse.urlencode({"webKey": self.web_key})
        request = urllib.request.Request(
            f"{url}?{query}", headers={"User-Agent": USER_AGENT,
                                       "Accept": "application/json"})

        last = None
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # 4xx other than rate limiting will not improve on retry.
                if e.code == 404:
                    return None
                if e.code not in (429, 500, 502, 503, 504):
                    raise SourceUnavailable(f"HTTP {e.code} from FMCSA") from e
                last = e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last = e
            log.warning("FMCSA fetch attempt %s/%s failed: %s",
                        attempt, HTTP_RETRIES, last)

        raise SourceUnavailable(f"FMCSA unreachable after {HTTP_RETRIES} attempts: {last}")

    @staticmethod
    def _normalize(record, mc_number):
        """
        Map an FMCSA payload onto our carrier columns.

        VERIFY THIS AGAINST A LIVE RESPONSE before trusting production output.
        Every lookup is a .get() and every conversion is guarded, so a wrong or
        renamed field yields a missing value rather than an exception — which
        keeps a schema change upstream from taking the nightly job down, but
        also means a silently wrong mapping shows up as sparse data, not an error.
        Check a known carrier's output by eye the first time this runs for real.
        """
        phone = _digits(record.get("telephone"))
        email = (record.get("emailAddress") or "").strip().lower() or None

        return {
            "mc_number": mc_number,
            "dot_number": _string(record.get("dotNumber")),
            "legal_name": _string(record.get("legalName")) or "(name not supplied)",
            "dba_name": _string(record.get("dbaName")),
            "registered_email": email,
            "registered_domain": email.split("@", 1)[1] if email and "@" in email else None,
            "registered_phone": phone or None,
            "registered_address": _address(record),
            "power_units": _int(record.get("totalPowerUnits"), default=1),
            "oos_rate": _rate(record.get("vehicleOosRate")),
            # 'allowedToOperate' is the field that decides whether this carrier
            # may legally haul today, which is the whole point of the check.
            "authority_status": ("active"
                                 if _string(record.get("allowedToOperate")) == "Y"
                                 else "inactive"),
        }


def _normalize_authority(payload):
    """Authority sub-resource -> our columns. Same caveat as _normalize."""
    record = _first_carrier(payload) or {}
    out = {}
    if (granted := _date(record.get("authorityGrantDate"))):
        out["authority_granted"] = granted
    if (status := _string(record.get("carrierOperation"))):
        out["_raw_authority"] = status
    return out


# ---------------------------------------------------------------- coercion

def _string(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _digits(value):
    return "".join(c for c in str(value or "") if c.isdigit())


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _rate(value):
    """Accepts 12.5 (percent) or 0.125 (fraction); stores a 0-1 fraction."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1:
        number /= 100
    return max(0.0, min(1.0, number))


def _date(value):
    text = _string(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    return None


def _address(record):
    parts = [record.get("phyStreet"), record.get("phyCity"),
             record.get("phyState"), record.get("phyZipcode")]
    joined = ", ".join(p.strip() for p in parts if p and str(p).strip())
    return joined or None


def _docket_digits(mc_number):
    """'MC-471902' -> '471902'."""
    return _digits(mc_number).lstrip("0") or None


def _first_carrier(payload):
    """
    QCMobile wraps results inconsistently across endpoints — sometimes
    {"content": {...}}, sometimes {"content": [{"carrier": {...}}]}. Unwrap
    whatever shape came back rather than assuming one.
    """
    if not payload:
        return None
    content = payload.get("content", payload) if isinstance(payload, dict) else payload
    if isinstance(content, list):
        content = content[0] if content else None
    if isinstance(content, dict) and "carrier" in content:
        content = content["carrier"]
    return content if isinstance(content, dict) else None


# ---------------------------------------------------------------- writing

# Only columns the source actually supplies get overwritten. A partial payload
# must never blank out insurance data we already had.
UPSERTABLE = (
    "dot_number", "legal_name", "dba_name", "registered_domain",
    "registered_email", "registered_phone", "registered_address",
    "address_type", "registered_phone_type", "authority_status",
    "authority_granted", "reinstatement_count", "liability_expires",
    "cargo_expires", "cargo_coverage_usd", "power_units", "oos_rate",
    "last_inspection",
)


def upsert_carrier(conn, record, source_name):
    """Insert or update one carrier. Returns True if a row was written."""
    mc_number = record.get("mc_number")
    if not mc_number:
        return False

    fields = {k: v for k, v in record.items()
              if k in UPSERTABLE and v is not None}

    columns = ["mc_number", "source", "fetched_at", "updated_at",
               "last_verified_at", *fields]
    values = [mc_number, source_name, datetime.now(UTC), datetime.now(UTC),
              datetime.now(UTC), *fields.values()]

    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns[1:])
    placeholders = ", ".join(["%s"] * len(columns))

    conn.execute(
        f"""INSERT INTO carriers ({', '.join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (mc_number) DO UPDATE SET {assignments}""", values)
    return True


# ---------------------------------------------------------------- orchestration

def build_source():
    """Pick a source from configuration. Never silently fabricates data."""
    if cfg.FMCSA_WEBKEY:
        return FmcsaQcMobileSource(cfg.FMCSA_WEBKEY)
    return UnavailableSource()


def resolve_carrier(conn, mc_number, source=None, max_age_hours=DEFAULT_STALE_HOURS):
    """
    Get a carrier for a check, refreshing on demand when the cached copy is
    stale or absent.

    Returns (carrier_row_or_None, status) where status is one of:
        'found'       — we have a record
        'not_found'   — the source was reached and has no such authority
        'unavailable' — we could not ask; the caller must not treat this
                        as evidence of anything about the carrier

    The distinction is the point. Treating 'unavailable' as 'not_found' would
    turn an outage on our side into a fraud accusation against a real carrier.
    """
    source = source or build_source()

    row = conn.execute(
        """SELECT *, fetched_at < now() - make_interval(hours => %s) AS is_stale
           FROM carriers WHERE mc_number = %s""",
        (max_age_hours, mc_number)).fetchone()

    if row and not row["is_stale"]:
        return row, "found"

    try:
        fresh = source.fetch(mc_number)
    except SourceUnavailable as e:
        log.warning("on-demand lookup unavailable for %s: %s", mc_number, e)
        # A stale record still beats nothing — the engine sees how old it is.
        return (row, "found") if row else (None, "unavailable")

    if fresh is None:
        if row:
            # Upstream now says this authority is gone. Keep the row, record
            # that we asked, and let the authority_status signal do its work.
            conn.execute(
                "UPDATE carriers SET last_verified_at = now() WHERE mc_number = %s",
                (mc_number,))
            return row, "found"
        return None, "not_found"

    upsert_carrier(conn, fresh, source.name)
    return conn.execute("SELECT * FROM carriers WHERE mc_number = %s",
                        (mc_number,)).fetchone(), "found"


def run_ingest(mode="stale", mc_numbers=None, stale_hours=DEFAULT_STALE_HOURS,
               dry_run=False, source=None):
    """Refresh carriers and record the run. Returns the run summary row."""
    source = source or build_source()
    seen = updated = errors = 0
    message = None

    with db.connection() as conn:
        run = conn.execute(
            "INSERT INTO ingest_runs (source, mode) VALUES (%s, %s) RETURNING id",
            (source.name, mode)).fetchone()
        run_id = run["id"]

        # A run with no usable source is a FAILED run, not a clean no-op. Left
        # as 'ok' it would look identical in monitoring to a successful refresh,
        # and the carriers table would quietly rot while every dashboard stayed
        # green — precisely the failure this table exists to make visible.
        if isinstance(source, UnavailableSource):
            summary = conn.execute(
                """UPDATE ingest_runs
                      SET status = 'failed', message = %s, finished_at = now()
                    WHERE id = %s RETURNING *""",
                ("No carrier data source configured; nothing was refreshed.",
                 run_id)).fetchone()
            log.error("ingest run=%s has no usable source — no data refreshed", run_id)
            return summary

        if mc_numbers:
            targets = list(mc_numbers)
        elif mode == "all":
            targets = [r["mc_number"] for r in conn.execute(
                "SELECT mc_number FROM carriers ORDER BY mc_number").fetchall()]
        else:
            # Stale first, and only carriers someone has actually checked.
            # Refreshing the entire FMCSA census nightly would be enormous and
            # pointless; refreshing what your customers vet is neither.
            targets = [r["mc_number"] for r in conn.execute(
                """SELECT c.mc_number FROM carriers c
                   WHERE c.fetched_at < now() - make_interval(hours => %s)
                     AND EXISTS (SELECT 1 FROM checks k
                                 WHERE k.mc_number = c.mc_number
                                   AND k.created_at > now() - INTERVAL '90 days')
                   ORDER BY c.fetched_at""", (stale_hours,)).fetchall()]

        log.info("ingest run=%s source=%s mode=%s targets=%s dry_run=%s",
                 run_id, source.name, mode, len(targets), dry_run)

        for mc_number in targets:
            seen += 1
            try:
                record = source.fetch(mc_number)
            except SourceUnavailable as e:
                errors += 1
                message = str(e)
                log.error("ingest aborted at %s: %s", mc_number, e)
                break
            except Exception as e:                       # noqa: BLE001
                errors += 1
                log.exception("ingest error on %s: %s", mc_number, e)
                continue

            if record is None:
                log.info("no upstream record for %s", mc_number)
                continue
            if not dry_run and upsert_carrier(conn, record, source.name):
                updated += 1

        status = ("failed" if errors and not updated
                  else "partial" if errors else "ok")
        summary = conn.execute(
            """UPDATE ingest_runs
                  SET status = %s, carriers_seen = %s, carriers_updated = %s,
                      errors = %s, message = %s, finished_at = now()
                WHERE id = %s RETURNING *""",
            (status, seen, updated, errors, message, run_id)).fetchone()

    return summary


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description="Refresh CarrierGuard carrier data.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stale", action="store_true",
                       help="refresh stale, recently-checked carriers (default)")
    group.add_argument("--all", action="store_true", help="refresh every known carrier")
    group.add_argument("--mc", metavar="MC-NUMBER", nargs="+", help="refresh specific carriers")
    parser.add_argument("--stale-hours", type=int, default=DEFAULT_STALE_HOURS)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and report, write nothing")
    args = parser.parse_args()

    mode = "all" if args.all else "single" if args.mc else "stale"
    source = build_source()

    if isinstance(source, UnavailableSource):
        print("No FMCSA_WEBKEY configured — nothing to ingest.\n"
              "Existing carrier data is left untouched and is NOT refreshed.",
              file=sys.stderr)

    summary = run_ingest(mode=mode, mc_numbers=args.mc,
                         stale_hours=args.stale_hours, dry_run=args.dry_run,
                         source=source)

    # Expired rate-limit buckets, swept here rather than per request — there is
    # no reason to pay for cleanup on the hot path.
    with db.connection() as conn:
        purged = purge_rate_limits(conn)
    if purged:
        log.info("purged %s expired rate-limit buckets", purged)

    print(f"run #{summary['id']}  source={summary['source']}  mode={summary['mode']}  "
          f"status={summary['status'].upper()}")
    print(f"  seen={summary['carriers_seen']}  updated={summary['carriers_updated']}  "
          f"errors={summary['errors']}")
    if summary["message"]:
        print(f"  {summary['message']}")

    db.close_pool()
    sys.exit(0 if summary["status"] in ("ok", "partial") else 1)


if __name__ == "__main__":
    main()
