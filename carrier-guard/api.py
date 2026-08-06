"""
CarrierGuard — JSON API.

The web UI is how a broker tries the product. This is how they actually use it:
a TMS fires a check automatically at tender time, because nobody remembers to
visit a website at 4:50pm on a Friday.

Authenticated by API key. Every response is scoped to the key's organization.
"""

import logging

from flask import Blueprint, g, jsonify, request

from auth import api_key_required, consume_check, quota_state
from ingest import resolve_carrier
from risk_engine import normalize_phone, run_check

log = logging.getLogger("carrierguard.api")

api = Blueprint("api", __name__, url_prefix="/api/v1")

MAX_FIELD = 200
MAX_LOAD_VALUE = 100_000_000


def _text(raw, limit=MAX_FIELD):
    return str(raw or "").strip()[:limit]


def _signal_json(s):
    d = s._asdict() if hasattr(s, "_asdict") else dict(s)
    return {"code": d["code"], "label": d["label"], "detail": d["detail"],
            "weight": d["weight"], "severity": d["severity"], "source": d["source"]}


def _carrier_json(c):
    if c is None:
        return None
    return {
        "mc_number": c["mc_number"],
        "dot_number": c["dot_number"],
        "legal_name": c["legal_name"],
        "authority_status": c["authority_status"],
        "authority_granted": c["authority_granted"].isoformat()
                             if c["authority_granted"] else None,
        "cargo_expires": c["cargo_expires"].isoformat() if c["cargo_expires"] else None,
        "liability_expires": c["liability_expires"].isoformat()
                             if c["liability_expires"] else None,
        "cargo_coverage_usd": c["cargo_coverage_usd"],
        "power_units": c["power_units"],
        "oos_rate": float(c["oos_rate"]),
        "registered_email": c["registered_email"],
        "registered_phone": c["registered_phone"],
        "record_fetched_at": c["fetched_at"].isoformat(),
    }


@api.post("/checks")
@api_key_required
def create_check():
    """
    Run a check.

        curl -X POST https://…/api/v1/checks \\
          -H "Authorization: Bearer cg_live_…" \\
          -H "Content-Type: application/json" \\
          -d '{"mc_number":"MC-471902",
               "contact_email":"dispatch@greatlakesfreight-sys.com",
               "contact_phone":"872-555-0417",
               "load_value_usd":78000}'
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="invalid_json",
                       message="Send a JSON object with at least mc_number."), 400

    mc_number = _text(payload.get("mc_number")).upper()
    if not mc_number:
        return jsonify(error="missing_field", message="mc_number is required."), 400

    load_value = payload.get("load_value_usd")
    if load_value is not None:
        try:
            load_value = int(load_value)
        except (TypeError, ValueError):
            return jsonify(error="invalid_field",
                           message="load_value_usd must be a whole number."), 400
        if not 0 <= load_value <= MAX_LOAD_VALUE:
            return jsonify(error="invalid_field",
                           message="load_value_usd is out of range."), 400

    conn, org = g.api_conn, g.org

    used, limit, exceeded = quota_state(org)
    if exceeded:
        return jsonify(error="quota_exceeded",
                       message=f"This account has used {used} of {limit} checks for "
                               f"the current period.",
                       checks_used=used, checks_limit=limit), 429

    check_id, result = run_check(
        conn, org["id"], mc_number,
        _text(payload.get("contact_email")).lower(),
        normalize_phone(payload.get("contact_phone")),
        _text(payload.get("contact_name")), load_value, resolver=resolve_carrier)
    consume_check(conn, org["id"])

    log.info("api check id=%s org=%s mc=%s verdict=%s",
             check_id, org["id"], mc_number, result["verdict"])

    return jsonify(
        check_id=check_id,
        mc_number=mc_number,
        verdict=result["verdict"],
        risk_score=result["score"],
        carrier=_carrier_json(result["carrier"]),
        signals=[_signal_json(s) for s in result["signals"]],
    ), 201


@api.get("/checks/<int:check_id>")
@api_key_required
def get_check(check_id):
    """Retrieve a past check. Scoped to the key's organization."""
    conn, org = g.api_conn, g.org

    row = conn.execute(
        "SELECT * FROM checks WHERE id = %s AND org_id = %s",
        (check_id, org["id"])).fetchone()
    # Deliberately identical to the not-found response: telling a caller that a
    # check exists but belongs to someone else leaks that another subscriber
    # vetted a carrier, which is exactly the thing tenancy is protecting.
    if row is None:
        return jsonify(error="not_found", message="No such check."), 404

    signals = conn.execute(
        "SELECT * FROM check_signals WHERE check_id = %s ORDER BY id",
        (check_id,)).fetchall()
    carrier = conn.execute(
        "SELECT * FROM carriers WHERE mc_number = %s", (row["mc_number"],)).fetchone()

    return jsonify(
        check_id=row["id"],
        mc_number=row["mc_number"],
        verdict=row["verdict"],
        risk_score=row["risk_score"],
        created_at=row["created_at"].isoformat(),
        carrier=_carrier_json(carrier),
        signals=[_signal_json(s) for s in signals],
    )


@api.get("/usage")
@api_key_required
def usage():
    """Current period usage, so a customer can watch their own allowance."""
    used, limit, exceeded = quota_state(g.org)
    return jsonify(plan=g.org["plan"], checks_used=used, checks_limit=limit,
                   quota_exceeded=exceeded,
                   period_start=g.org["period_start"].isoformat())
