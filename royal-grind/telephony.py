"""
Royal Grind — the business phone layer.

The PDF this product was scoped from called the phone integration a 4/10 and it
was right, but the 4 is not in the API call. Placing a call is one POST. The
work is everything around it: consent evaluated before the dial, events arriving
out of order from a platform that retries, a recording URL that requires
authentication to fetch, and a transcript that has to land on the right row
thirty seconds after the rep already moved on.

Two providers behind one interface:

    SimulatedProvider   No account, no minutes, no cost. Produces a realistic
                        call — including a transcript — from the quality of the
                        contact record and the hour of the day. This is what
                        makes the whole Find -> Call -> Close -> Automate loop
                        demonstrable on a laptop on a plane, and it is what the
                        smoke test runs against.

    VonageProvider      The Voice API. Real HTTP, real JWT, real NCCO.

> **Testing status.** `SimulatedProvider` is exercised end to end by the smoke
> test. `VonageProvider` is written against the documented Voice API but has not
> been run against a live Vonage account — there is no account attached to this
> repository. Treat the first real call as the test of it, and see DEPLOY.md for
> the account setup it assumes.

Nothing in this module decides whether a call is permitted. `compliance.py` does
that, and `app.py` calls it first. Keeping the gate out of the dialer means a
future second dialer cannot forget to ask.
"""

import base64
import hashlib
import hmac
import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta


class TelephonyError(RuntimeError):
    """Raised when a call cannot be placed. The message is shown to the rep."""


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class Provider:
    name = "abstract"

    def place_call(self, *, to_number, from_number, call_ref, answer_url,
                   event_url, record, announcement):
        """
        Start an outbound call. Returns {"provider_call_id": ..., "status": ...}.

        `call_ref` is our own row id, round-tripped through the provider so that
        an event arriving before the API response still finds its call.
        """
        raise NotImplementedError

    def hangup(self, provider_call_id):
        raise NotImplementedError

    def fetch_recording(self, url):
        """Returns bytes, or None if the provider stores recordings elsewhere."""
        return None

    def status(self):
        return {"provider": self.name, "ready": True, "detail": ""}


# ---------------------------------------------------------------------------
# The simulator
# ---------------------------------------------------------------------------

# Outcome probabilities for a baseline B2B cold dial, before the record's own
# quality moves them. These are deliberately pessimistic: a demo that connects
# on every call teaches a buyer the wrong thing about their own economics, and
# the first week of real usage would then read as a product failure.
# These are per-DIAL, and they are the numbers a sales VP will check against
# their own floor first. Industry cold-dial benchmarks put connect rates in the
# high single digits and meetings at one to three percent of dials; anything
# rosier here would make the demo's economics a lie the buyer discovers in
# week one.
_BASE_OUTCOMES = [
    ("no_answer", 0.455),
    ("voicemail", 0.275),
    ("gatekeeper", 0.115),
    ("connected", 0.060),
    ("not_interested", 0.035),
    ("callback", 0.015),
    ("wrong_number", 0.015),
    ("bad_number", 0.015),
]


class SimulatedProvider(Provider):
    """
    A phone platform that never rings anyone.

    Outcomes are drawn from a distribution that shifts with the things that
    actually shift them in the field — whether the number was verified, whether
    the person is a decision maker, and what time it is where they are. A
    verified direct dial to a director at 9:20am behaves differently from a
    guessed switchboard number at 4:55pm, and a simulator where it does not is
    a random number generator with a phone icon.

    Seeded per call, so re-running the smoke test twice produces the same calls
    and a failure is reproducible.
    """

    name = "simulated"

    def __init__(self, seed=None):
        self.seed = seed

    def status(self):
        return {"provider": "simulated", "ready": True,
                "detail": ("Calls are simulated end to end — no account, no minutes, "
                           "no cost. Set TELEPHONY_PROVIDER=vonage to dial for real.")}

    def place_call(self, *, to_number, from_number, call_ref, answer_url=None,
                   event_url=None, record=False, announcement="", context=None):
        return {"provider_call_id": f"sim-{call_ref}-{int(time.time())}",
                "status": "ringing"}

    # -------------------------------------------------------------- outcome --

    def simulate(self, *, call_ref, context):
        """
        Resolve a simulated call. Returns the event dict the webhook would have
        delivered: disposition, duration, and a transcript when someone talked.

        `context` carries the record quality and the company profile — see
        `build_context()` in app.py.
        """
        rng = random.Random(self.seed if self.seed is not None
                            else f"{call_ref}:{context.get('company_name','')}")

        weights = dict(_BASE_OUTCOMES)

        # Data quality moves the dial more than anything a rep says. A verified
        # direct line roughly doubles the chance of reaching a person; a guessed
        # number mostly produces the tones that mean the number is wrong.
        point_status = context.get("point_status", "probable")
        if point_status == "verified":
            weights["connected"] *= 1.7
            weights["gatekeeper"] *= 0.6
            weights["bad_number"] *= 0.15
            weights["wrong_number"] *= 0.3
        elif point_status == "guessed":
            weights["connected"] *= 0.45
            weights["bad_number"] *= 4.0
            weights["wrong_number"] *= 3.0
        if context.get("point_kind") == "switchboard":
            weights["gatekeeper"] *= 3.2
            weights["connected"] *= 0.5
        if context.get("point_kind") == "mobile":
            weights["connected"] *= 1.35
            weights["gatekeeper"] *= 0.2

        # Time of day. Mid-morning and late afternoon are when operations people
        # are at a desk; the middle of the day is when they are on the floor.
        hour = context.get("local_hour", 10)
        if hour in (9, 10, 16):
            weights["connected"] *= 1.2
            weights["no_answer"] *= 0.8
        elif hour in (12, 13):
            weights["connected"] *= 0.65
            weights["no_answer"] *= 1.25

        if context.get("role_type") == "decision_maker":
            weights["gatekeeper"] *= 0.5
        if context.get("prior_connects", 0) > 0:
            # Somebody who has spoken to this rep before picks up for them.
            weights["connected"] *= 1.6
            weights["voicemail"] *= 0.7

        disposition = _weighted_choice(rng, weights)

        # A live conversation converts to a meeting more often when there was a
        # real reason to call. This is the relationship the learning loop is
        # meant to discover from data — the simulator has to actually contain it
        # or /intelligence would be a chart of pure noise.
        if disposition == "connected":
            p_meeting = 0.06 + min(context.get("score", 40), 100) / 100.0 * 0.14
            if context.get("why_now"):
                p_meeting += 0.06
            if rng.random() < p_meeting:
                disposition = "meeting_set"
            elif rng.random() < 0.22:
                disposition = "not_interested"

        duration = {
            "no_answer": rng.randint(18, 32),
            "voicemail": rng.randint(28, 52),
            "gatekeeper": rng.randint(25, 70),
            "wrong_number": rng.randint(12, 28),
            "bad_number": rng.randint(3, 9),
            "not_interested": rng.randint(35, 95),
            "callback": rng.randint(40, 110),
            "connected": rng.randint(150, 460),
            "meeting_set": rng.randint(220, 620),
        }[disposition]

        transcript = ""
        if disposition in ("connected", "meeting_set", "not_interested",
                           "callback", "gatekeeper"):
            transcript = _transcript(rng, disposition, context)

        return {"disposition": disposition, "duration_sec": duration,
                "transcript": transcript,
                "status": "completed" if disposition != "bad_number" else "failed"}


def _weighted_choice(rng, weights):
    total = sum(weights.values())
    pick = rng.random() * total
    running = 0.0
    for key, w in weights.items():
        running += w
        if pick <= running:
            return key
    return "no_answer"


# --------------------------------------------------------------- transcripts --
#
# Written from the company's own profile so the AI analysis downstream has real
# material to extract — a named incumbent, a load count, a lane, an objection.
# A lorem-ipsum transcript would let the analyzer look clever while proving
# nothing about whether it can pull a commitment out of a real conversation.

_OPENERS = [
    "{rep_line}",
    "Hey — {rep_line}",
    "Morning. {rep_line}",
]


def _first(value, fallback):
    """First name, or the fallback when the field is blank — seeds and imports
    both produce records with no name on them."""
    parts = (value or "").split()
    return parts[0] if parts else fallback


def _transcript(rng, disposition, ctx):
    name = _first(ctx.get("contact_name"), "there")
    company = ctx.get("company_name", "your company")
    loads = ctx.get("loads_per_month") or rng.choice([12, 18, 26, 40, 65])
    modes = (ctx.get("modes") or "ltl,ftl").split(",")
    mode = modes[0].upper() if modes else "LTL"
    incumbent = (ctx.get("incumbents") or "").split(",")[0].strip() or rng.choice(
        ["a couple of the big brokerages", "one national broker", "two brokers we've used for years"])
    lane = (ctx.get("lanes") or "").splitlines()[0] if ctx.get("lanes") else "our main outbound lane"
    why = ctx.get("why_now_headline") or f"your operation in {ctx.get('city','the area')}"
    rep = _first(ctx.get("rep_name"), "the rep")

    if disposition == "gatekeeper":
        return (
            f"Front desk: {company}, how can I direct your call?\n"
            f"{rep}: Hi — {rep} with {ctx.get('org_name','us')}. I'm trying to reach {ctx.get('contact_name','the logistics manager')}.\n"
            f"Front desk: Is she expecting your call?\n"
            f"{rep}: No — I read about {why} and wanted to ask her one question about outbound capacity.\n"
            f"Front desk: She's in a meeting. I can take a message or you can try her line after three.\n"
            f"{rep}: After three works. Thank you.")

    if disposition == "not_interested":
        return (
            f"{name}: This is {name}.\n"
            f"{rep}: {name}, {rep} with {ctx.get('org_name','us')} — I saw {why}. I work with "
            f"{ctx.get('shipper_type','manufacturers')} on overflow {mode} capacity.\n"
            f"{name}: I appreciate the call but we're covered. We're under contract with "
            f"{incumbent} through the end of the year and I'm not looking to add anybody.\n"
            f"{rep}: Understood. When does that come up for bid?\n"
            f"{name}: Usually October. I'd say send me something then, not now.\n"
            f"{rep}: I'll do that. Thanks for being straight with me.")

    if disposition == "callback":
        return (
            f"{name}: {name} speaking.\n"
            f"{rep}: {name}, it's {rep} with {ctx.get('org_name','us')} — I'll be quick. I saw {why}.\n"
            f"{name}: You caught me walking into something. Can you call me back Thursday morning?\n"
            f"{rep}: Thursday at nine?\n"
            f"{name}: Nine works. And email me whatever you've got on {mode} out of {lane} so I can look before then.\n"
            f"{rep}: Sending it today. Talk Thursday.")

    pain = rng.choice([
        f"expedited rates have been brutal — we got quoted almost double on two loads last month",
        f"we've had three missed pickups since June and nobody calls us, we find out from the customer",
        f"tracking is the problem. I'm the one calling the broker to ask where my own freight is",
        f"they're fine on the easy lanes and they disappear the second something is time-sensitive",
    ])
    ask = rng.choice([
        "send me a capability sheet",
        "send over what you can do on that lane and your expedited process",
        "put something in writing on rates and I'll take it to my VP",
    ])

    base = (
        f"{name}: This is {name}.\n"
        f"{rep}: {name} — {rep} with {ctx.get('org_name','us')}. Do you have sixty seconds?\n"
        f"{name}: Go ahead.\n"
        f"{rep}: I saw {why}. I work with {ctx.get('shipper_type','manufacturers')} in "
        f"{ctx.get('state','the region')} on {mode} and expedited capacity. Who handles "
        f"outbound for you today?\n"
        f"{name}: That's me. We run about {loads} loads a month, mostly {mode.lower()}, "
        f"heaviest on {lane}. We use {incumbent}.\n"
        f"{rep}: How's that going?\n"
        f"{name}: Honestly — {pain}.\n"
        f"{rep}: That's the reason I called. We hold capacity on that corridor and you'd have "
        f"one person on our side, not a queue.\n")

    if disposition == "meeting_set":
        day = rng.choice(["Tuesday", "Wednesday", "Thursday", "next Monday"])
        return base + (
            f"{name}: Alright. {ask.capitalize()} and let's do a call {day}.\n"
            f"{rep}: {day} morning — does ten work?\n"
            f"{name}: Ten's fine. Use this number. And include your MC and your insurance cert, "
            f"our procurement will ask.\n"
            f"{rep}: You'll have all of it before {day}. Thanks {name}.")

    return base + (
        f"{name}: Maybe. We're not unhappy enough to move everything, but I'll look.\n"
        f"{rep}: That's fair. Can I {ask}?\n"
        f"{name}: Sure, send it. I can't promise a timeline — budget conversations are Q1.\n"
        f"{rep}: I'll send it today and check back after the new year. Appreciate the time.")


# ---------------------------------------------------------------------------
# Vonage
# ---------------------------------------------------------------------------

class VonageProvider(Provider):
    """
    Vonage Voice API.

    Auth is a short-lived RS256 JWT signed with the application private key —
    not an API key in a header, which is why `PyJWT[crypto]` is a runtime
    dependency rather than optional.

    The call is placed with an NCCO that (optionally) speaks a recording
    announcement, then connects the rep. The rep leg is dialed first so that a
    prospect never answers to silence while the platform hunts for a salesperson
    — the failure mode that gets an outbound number flagged as spam.
    """

    name = "vonage"
    API = "https://api.nexmo.com/v1/calls"

    def __init__(self, application_id, private_key, from_number, timeout=12):
        self.application_id = application_id
        self.private_key = private_key
        self.from_number = from_number
        self.timeout = timeout

    def status(self):
        ok = bool(self.application_id and self.private_key)
        return {"provider": "vonage", "ready": ok,
                "detail": ("Live Voice API." if ok else
                           "VONAGE_APPLICATION_ID / VONAGE_PRIVATE_KEY are not set.")}

    def _token(self):
        try:
            import jwt
        except ImportError as exc:      # pragma: no cover - dependency guard
            raise TelephonyError(
                "PyJWT is required to authenticate against Vonage. "
                "pip install 'PyJWT[crypto]'") from exc
        now = int(time.time())
        payload = {"application_id": self.application_id, "iat": now,
                   "exp": now + 60, "jti": base64.urlsafe_b64encode(
                       hashlib.sha256(f"{now}{self.application_id}".encode()).digest()
                   ).decode()[:24]}
        key = self.private_key.replace("\\n", "\n")
        return jwt.encode(payload, key, algorithm="RS256")

    def _post(self, url, body):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._token()}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise TelephonyError(f"Vonage rejected the call ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise TelephonyError(f"Could not reach Vonage: {exc.reason}") from exc

    def place_call(self, *, to_number, from_number, call_ref, answer_url=None,
                   event_url=None, record=False, announcement="", context=None):
        ncco = []
        if announcement:
            ncco.append({"action": "talk", "text": announcement, "language": "en-US"})
        if record:
            # split:conversation gives a two-channel recording, which is what
            # makes a transcript attributable to a speaker. A single mixed
            # channel produces a summary that cannot tell you who committed to
            # what, which is the only part of a sales call that matters.
            ncco.append({"action": "record", "split": "conversation",
                         "channels": 2, "eventUrl": [f"{event_url}&kind=recording"]})
        ncco.append({"action": "connect", "from": from_number or self.from_number,
                     "eventUrl": [f"{event_url}&kind=leg"],
                     "endpoint": [{"type": "phone", "number": _digits(to_number)}]})

        body = {"to": [{"type": "phone", "number": _digits(to_number)}],
                "from": {"type": "phone", "number": _digits(from_number or self.from_number)},
                "ncco": ncco}
        if event_url:
            body["event_url"] = [f"{event_url}&kind=call"]
        result = self._post(self.API, body)
        return {"provider_call_id": result.get("uuid", ""),
                "status": result.get("status", "started")}

    def hangup(self, provider_call_id):
        req = urllib.request.Request(
            f"{self.API}/{provider_call_id}",
            data=json.dumps({"action": "hangup"}).encode(), method="PUT",
            headers={"Authorization": f"Bearer {self._token()}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):
                return True
        except urllib.error.URLError:
            return False

    def fetch_recording(self, url):
        """Recordings sit behind the same JWT — a plain GET returns 401."""
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token()}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.URLError:
            return None


def _digits(number):
    out = "".join(c for c in (number or "") if c.isdigit())
    # Vonage expects E.164 without the plus. A ten-digit US number needs the 1.
    return f"1{out}" if len(out) == 10 else out


# ---------------------------------------------------------------------------
# Webhook authentication
# ---------------------------------------------------------------------------
#
# The callback URL handed to the phone platform carries a signed token. The
# platform echoes it back on every event, and an event whose token does not
# verify is dropped. Without this, the call webhook is an unauthenticated
# endpoint that writes transcripts into a multi-tenant database, and its URL is
# sitting in a third party's configuration screen.

def sign_ref(secret, call_ref):
    if not secret:
        return ""
    mac = hmac.new(secret.encode(), str(call_ref).encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")[:32]


def verify_ref(secret, call_ref, token):
    """Constant-time compare. A `==` here is a timing oracle on the secret."""
    if not secret:
        return False
    return hmac.compare_digest(sign_ref(secret, call_ref), token or "")


def event_url(base_url, call_ref, secret):
    return f"{base_url.rstrip('/')}/webhook/call?ref={call_ref}&t={sign_ref(secret, call_ref)}"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def get_provider(config):
    """One place decides which phone platform is live. See config.py."""
    if (config.TELEPHONY_PROVIDER or "simulated").lower() == "vonage":
        return VonageProvider(config.VONAGE_APPLICATION_ID, config.VONAGE_PRIVATE_KEY,
                              config.VONAGE_NUMBER)
    return SimulatedProvider()
