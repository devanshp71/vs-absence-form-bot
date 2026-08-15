"""
VS Volunteer Absence -- Conversational Bot (v2)
================================================

Replaces the old "here's a link, go fill out a form" bot with an actual
conversation. Two submission modes live in the SAME bot, per Telegram user:

  MODE A -- "Prefilled link" (default, zero backend risk)
    The bot asks the same questions a person would answer on the form,
    then hands back a Microsoft Forms link with every answer already
    filled in -- side, name, phone, dates, part of day, reason. The
    volunteer taps the link, glances at it, and hits Submit. Nothing new
    touches the schedule: it is the exact same official form, exact same
    flow, exact same everything -- just pre-typed.

  MODE B -- "Direct submit" (needs FLOW_B_ENDPOINT configured)
    Same conversation, but instead of handing back a link, the bot POSTs
    the answers straight to an HTTP-triggered clone of the absence flow
    (a separate trigger on a *copy* of the flow -- the original
    Forms-triggered flow is completely untouched). One less tap for the
    volunteer, at the cost of one more integration point.

  A volunteer can flip between the two with the hidden /switch command.
  Nothing about which mode is "official" is decided here -- that's a
  management call. This bot just makes both possible so they can compare.

WHY BOTH MODES CAN SHIP TODAY
------------------------------
Mode A has no dependency on anything new -- it only needs the Microsoft
Form's own "pre-filled answers" setting turned on (done, 8/15/2026) and
knowing each question's field ID (also done -- see FIELD_IDS below,
confirmed against a live form + a live flow run, not guessed).

Mode B needs the HTTP-triggered flow clone to exist first. Until
FLOW_B_ENDPOINT is set, anyone who tries Mode B gets a friendly
"not turned on yet" message and stays on Mode A -- so this file can be
deployed right now without blocking on that follow-up step.

WHAT IS REMEMBERED, AND WHAT ISN'T
-----------------------------------
Phone, side, and name are remembered per Telegram user for as long as this
process stays warm. Railway's free tier puts the container to sleep when
idle and it can lose in-memory state on a cold start -- worst case, a
volunteer is asked their phone/side/name again after a long gap. Nothing
is lost or corrupted by that; it's just an extra question. If that turns
out to be annoying in practice, the fix is a few lines writing this same
dict to a small durable store -- flagged here rather than built now, since
speed mattered more than that polish for the first version.

HOW IDENTITY WORKS
-------------------
Telegram's "share contact" button hands back the phone number attached to
the person's own Telegram account -- they can't type someone else's number
in by accident, and it's the same phone-first matching the backend already
uses to find the right roster row. Side (E/I) and name are asked once and
then reused; name is offered as a guess from their Telegram profile first
("We have you as X -- right?") so most people just tap Yes.
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("vs-absence-bot-v2")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_DOMAIN = (
    os.environ.get("PUBLIC_DOMAIN") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or ""
).strip()

# Set this once the HTTP-triggered flow clone exists (Mode B). Until then,
# Mode B replies with a "not turned on yet" message instead of failing.
FLOW_B_ENDPOINT = os.environ.get("FLOW_B_ENDPOINT", "").strip()

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# The live Microsoft Form. "Enable pre-filled answers" was turned on for
# this form on 8/15/2026 specifically so Mode A can work -- that is the
# only setting on the form itself that changed.
FORM_BASE_URL = (
    "https://forms.cloud.microsoft/Pages/ResponsePage.aspx?"
    "id=vYPE0EyNF0uHS9KIombwfqWi2RwnglFNix88qhzOJ1tUMzIzSUZXRkVPVUo2RDFTVEtUVE9DRTBLVi4u"
)

# Confirmed 8/15/2026 two ways: (1) the "Get Pre-filled URL" tool inside
# Forms, question by question, and (2) a live flow run's raw trigger
# output (Get_response_details), which showed every one of these IDs with
# a real submitted value next to it. Not guessed.
FIELD_IDS = {
    "side": "r5ac1c0153f1448d2b413d0eeada3707e",
    "first_name": "r14e0025bed3b401a88cfe69183b5f226",
    "last_name": "r20798edeb1a34ca3b118fec8580aa1fa",
    "phone": "r3d424cccd9824ac7af4212498e59a73",
    "one_or_multiple": "rece5a395a04143b0b25f0c1cad8159a8",
    "single_day": "r78b4d212a371440fbb6d259029ce4392",
    "first_day": "ra8b3f26882ee48c38d4ece35de5c45d7",
    "last_day": "r956fdb7ae48c499ca44a68378de36560",
    "part_of_day": "r363582402137452f8505b9ac5e71bd4f",
    "time_text": "r2904d870cd6a40299935fc2abd4c2223",
    "reason": "r6db6981619cf401a97f10c7980a7451b",
}

SIDE_LABELS = {"e": "E-Side (Bhaiyo)", "i": "I-Side (Bheno)"}
PART_LABELS = {
    "full": "Full day",
    "am": "AM only",
    "pm": "PM only",
    "after": "Available after a time",
}


def _derived(purpose: str) -> str:
    return hmac.new(BOT_TOKEN.encode(), purpose.encode(), hashlib.sha256).hexdigest()[:32]


URL_PATH = "/tg/" + _derived("path-v2") if BOT_TOKEN else "/tg/unconfigured"
SECRET_TOKEN = _derived("secret-v2") if BOT_TOKEN else ""

# --------------------------------------------------------------------------
# Per-user state (in-memory -- see module docstring for the trade-off)
# --------------------------------------------------------------------------

# users[chat_id] = {
#   "mode": "A" | "B",
#   "phone": str, "side": "e"|"i", "first_name": str, "last_name": str,
#   "step": str,                # current question we're waiting on
#   "answers": {...},           # in-progress absence report
# }
users: dict = {}
_lock = threading.Lock()


def get_user(chat_id) -> dict:
    with _lock:
        return users.setdefault(chat_id, {"mode": "A"})


def reset_answers(u: dict) -> None:
    u["step"] = "one_or_multiple"
    u["answers"] = {}


def has_profile(u: dict) -> bool:
    return bool(u.get("phone") and u.get("side") and u.get("first_name"))


# --------------------------------------------------------------------------
# Telegram API helpers
# --------------------------------------------------------------------------


def call(method: str, payload: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        f"{API}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        logger.error("%s failed: HTTP %s %s", method, exc.code, exc.read()[:400])
    except Exception as exc:  # noqa: BLE001
        logger.error("%s failed: %s", method, exc)
    return {}


def ensure_webhook() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set.")
        return
    if not PUBLIC_DOMAIN:
        logger.error("No public domain -- generate one in Railway, then redeploy.")
        return
    wanted = f"https://{PUBLIC_DOMAIN}{URL_PATH}"
    info = call("getWebhookInfo", {}).get("result", {})
    if info.get("url") == wanted:
        logger.info("Webhook already correct: %s", wanted)
        return
    result = call(
        "setWebhook",
        {
            "url": wanted,
            "secret_token": SECRET_TOKEN,
            "allowed_updates": ["message", "callback_query"],
            "max_connections": 10,
        },
    )
    logger.info("setWebhook -> %s", result.get("ok"))


def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    call("sendMessage", payload)


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    call("answerCallbackQuery", payload)


def kb(rows):
    """rows: list of lists of (label, callback_data) tuples."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in rows
        ]
    }


def contact_kb():
    return {
        "keyboard": [[{"text": "📱 Share my phone number", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def remove_kb():
    return {"remove_keyboard": True}


# --------------------------------------------------------------------------
# Conversation
# --------------------------------------------------------------------------

GREETING = (
    "Jai Swaminarayan! I'll help you report an absence -- a few quick taps, "
    "no form to hunt down.\n\nFirst, can I grab your phone number? Tap the "
    "button below (it just shares the number already on your Telegram "
    "account -- nothing typed, nothing that can go to the wrong person)."
)


def start_profile(chat_id):
    send_message(chat_id, GREETING, contact_kb())


def ask_side(chat_id):
    send_message(
        chat_id,
        "Which side are you on?",
        kb([[("E-Side (Bhaiyo)", "side:e"), ("I-Side (Bheno)", "side:i")]]),
    )


def ask_name_confirm(chat_id, guess_first, guess_last):
    if guess_first:
        send_message(
            chat_id,
            f"We have you as <b>{guess_first} {guess_last}</b> from Telegram -- is that right?",
            kb([[("Yes, that's me", "name:yes"), ("No, let me type it", "name:no")]]),
        )
    else:
        send_message(chat_id, "What's your first name?")


def ask_absence_start(chat_id):
    send_message(
        chat_id,
        "Got it. Now the absence itself -- one day, or multiple days?",
        kb([[("Just one day", "days:one"), ("Multiple days", "days:multi")]]),
    )


def ask_single_day(chat_id):
    send_message(chat_id, "Which day will you be away? (e.g. 9/1/2026)")


def ask_first_day(chat_id):
    send_message(chat_id, "What's the first day you'll be away? (e.g. 9/1/2026)")


def ask_last_day(chat_id):
    send_message(chat_id, "And the last day? (e.g. 9/5/2026)")


def ask_part_of_day(chat_id):
    send_message(
        chat_id,
        "What part of the day?",
        kb(
            [
                [("Full day", "part:full"), ("AM only", "part:am")],
                [("PM only", "part:pm"), ("Available after a time", "part:after")],
            ]
        ),
    )


def ask_time_text(chat_id):
    send_message(chat_id, "What time will you be available after? (e.g. 6pm)")


def ask_reason(chat_id):
    send_message(
        chat_id,
        "Reason for the absence? (Optional -- tap Skip if you'd rather not say.)",
        kb([[("Skip", "reason:skip")]]),
    )


def summary_text(u: dict) -> str:
    a = u["answers"]
    side_label = SIDE_LABELS[u["side"]]
    if a["one_or_multiple"] == "Just one day":
        dates = a["single_day"]
    else:
        dates = f"{a['first_day']} to {a['last_day']}"
    part = a.get("part_of_day") or ""
    if part == PART_LABELS["after"]:
        part = f"{part} ({a.get('time_text', '')})"
    part_line = f"Part of day: {part}\n" if part else ""
    reason = a.get("reason") or "(none given)"
    return (
        f"<b>Please confirm</b>\n"
        f"{u['first_name']} {u['last_name']} · {side_label}\n"
        f"Dates: {dates}\n"
        f"{part_line}"
        f"Reason: {reason}\n\n"
        f"Mode: {'Prefilled link' if u['mode'] == 'A' else 'Direct submit'}"
    )


def ask_confirm(chat_id, u):
    send_message(
        chat_id,
        summary_text(u),
        kb([[("✅ Looks good", "confirm:yes"), ("Start over", "confirm:no")]]),
    )


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------


def _choice(value: str) -> str:
    """
    Confirmed 8/15/2026 against the live form: Microsoft Forms prefill
    only accepts a plain string for text/date questions, but a
    CHOICE question (radio buttons -- Side, One/Multiple days, Part of
    day) needs its value wrapped as a JSON string, i.e. literal quote
    characters around it, before URL-encoding. Skip this and the radio
    button silently stays unselected -- checked by generating a link with
    Microsoft's own "Get Pre-filled Link" tool and diffing it against a
    naive attempt.
    """
    return json.dumps(value)


def build_prefill_url(u: dict) -> str:
    a = u["answers"]
    params = {
        FIELD_IDS["side"]: _choice(SIDE_LABELS[u["side"]]),
        FIELD_IDS["first_name"]: u["first_name"],
        FIELD_IDS["last_name"]: u["last_name"],
        # NOTE: Phone number is a "number"-type question. Confirmed
        # 8/15/2026 that Microsoft Forms' own prefill tool does not
        # generate a parameter for number-type questions at all -- it
        # is simply not prefillable. Left out on purpose rather than
        # included-and-silently-ignored; the volunteer types it once,
        # same as today.
        FIELD_IDS["one_or_multiple"]: _choice(a["one_or_multiple"]),
    }
    if a["one_or_multiple"] == "Just one day":
        params[FIELD_IDS["single_day"]] = a["single_day"]
        # Part of day only exists as a question on the single-day branch --
        # confirmed 8/15/2026 (see handle_text). Leave it out entirely for
        # multi-day submissions rather than sending an answer to a question
        # that was never shown.
        if a.get("part_of_day"):
            params[FIELD_IDS["part_of_day"]] = _choice(a["part_of_day"])
        if a.get("part_of_day") == PART_LABELS["after"]:
            params[FIELD_IDS["time_text"]] = a.get("time_text", "")
    else:
        params[FIELD_IDS["first_day"]] = a["first_day"]
        params[FIELD_IDS["last_day"]] = a["last_day"]
    if a.get("reason"):
        params[FIELD_IDS["reason"]] = a["reason"]
    # quote_via=quote (not the default quote_plus) is required: this form
    # only recognizes %20 for spaces inside a prefill value, not '+'.
    # Confirmed 8/15/2026 -- a '+'-encoded link left every choice question
    # unanswered even though the URL looked identical apart from that.
    return FORM_BASE_URL + "&" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def submit_mode_a(chat_id, u):
    link = build_prefill_url(u)
    send_message(
        chat_id,
        "Everything's filled in except your phone number (Microsoft Forms "
        "can't pre-fill that one) -- tap below, add your number, glance it "
        "over, and hit <b>Submit</b>. That's it.\n\n"
        f'<a href="{link}">Open the pre-filled form</a>',
    )


def submit_mode_b(chat_id, u):
    if not FLOW_B_ENDPOINT:
        send_message(
            chat_id,
            "Direct-submit mode isn't turned on yet -- switching you back to "
            "the prefilled-link mode for this one.",
        )
        u["mode"] = "A"
        submit_mode_a(chat_id, u)
        return

    a = u["answers"]
    body = {
        "firstName": u["first_name"],
        "lastName": u["last_name"],
        "side": SIDE_LABELS[u["side"]],
        "phone": u["phone"],
        "oneOrMultiple": a["one_or_multiple"],
        "singleDay": a.get("single_day", ""),
        "firstDay": a.get("first_day", ""),
        "lastDay": a.get("last_day", ""),
        "partOfDay": a["part_of_day"],
        "timeText": a.get("time_text", ""),
        "reason": a.get("reason", ""),
    }
    req = urllib.request.Request(
        FLOW_B_ENDPOINT,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        send_message(chat_id, "✅ Submitted directly -- you're all set.")
    except Exception as exc:  # noqa: BLE001
        logger.error("Mode B submit failed: %s", exc)
        send_message(
            chat_id,
            "That didn't go through. Sending you the prefilled link instead so "
            "nothing gets lost:",
        )
        submit_mode_a(chat_id, u)


# --------------------------------------------------------------------------
# Update handling
# --------------------------------------------------------------------------


def handle_command(chat_id, u, text, from_user):
    cmd = text.split()[0].lower()
    if cmd in ("/start", "/absence"):
        if has_profile(u):
            reset_answers(u)
            ask_absence_start(chat_id)
        else:
            u["step"] = "phone"
            start_profile(chat_id)
        return True
    if cmd == "/switch":
        u["mode"] = "B" if u.get("mode", "A") == "A" else "A"
        label = "direct-submit" if u["mode"] == "B" else "prefilled-link"
        send_message(chat_id, f"Switched to {label} mode.")
        return True
    if cmd == "/profile":
        u.clear()
        u["mode"] = "A"
        u["step"] = "phone"
        start_profile(chat_id)
        return True
    return False


def handle_text(chat_id, u, text, from_user):
    if text.startswith("/"):
        if handle_command(chat_id, u, text, from_user):
            return
    step = u.get("step")

    if step == "first_name_typed":
        u["first_name"] = text.strip()
        u["step"] = "last_name_typed"
        send_message(chat_id, "And your last name?")
        return
    if step == "last_name_typed":
        u["last_name"] = text.strip()
        reset_answers(u)
        ask_absence_start(chat_id)
        return
    if step == "single_day":
        u["answers"]["single_day"] = text.strip()
        u["step"] = "part_of_day"
        ask_part_of_day(chat_id)
        return
    if step == "first_day":
        u["answers"]["first_day"] = text.strip()
        u["step"] = "last_day"
        ask_last_day(chat_id)
        return
    if step == "last_day":
        # The live form only asks "Part of day" on the single-day branch --
        # confirmed 8/15/2026 by prefilling a multi-day answer and watching
        # that question not even render. Go straight to reason here so this
        # matches the form exactly rather than asking something it never would.
        u["answers"]["last_day"] = text.strip()
        u["answers"]["part_of_day"] = ""
        u["step"] = "reason"
        ask_reason(chat_id)
        return
    if step == "time_text":
        u["answers"]["time_text"] = text.strip()
        u["step"] = "reason"
        ask_reason(chat_id)
        return
    if step == "reason":
        u["answers"]["reason"] = text.strip()
        u["step"] = "confirm"
        ask_confirm(chat_id, u)
        return

    # No step in progress -- treat any message as "let's start."
    if has_profile(u):
        reset_answers(u)
        ask_absence_start(chat_id)
    else:
        u["step"] = "phone"
        start_profile(chat_id)


def handle_contact(chat_id, u, contact):
    u["phone"] = contact.get("phone_number", "").lstrip("+")
    u["step"] = "side"
    send_message(chat_id, "Thanks!", remove_kb())
    ask_side(chat_id)


def handle_callback(chat_id, u, data, from_user):
    kind, _, value = data.partition(":")

    if kind == "side":
        u["side"] = value
        first = from_user.get("first_name", "")
        last = from_user.get("last_name", "")
        if first:
            u["_guess_first"], u["_guess_last"] = first, last
            u["step"] = "name_confirm"
            ask_name_confirm(chat_id, first, last)
        else:
            u["step"] = "first_name_typed"
            send_message(chat_id, "What's your first name?")
        return

    if kind == "name":
        if value == "yes":
            u["first_name"] = u.pop("_guess_first", "")
            u["last_name"] = u.pop("_guess_last", "")
            reset_answers(u)
            ask_absence_start(chat_id)
        else:
            u["step"] = "first_name_typed"
            send_message(chat_id, "No problem -- what's your first name?")
        return

    if kind == "days":
        u["answers"]["one_or_multiple"] = "Just one day" if value == "one" else "Multiple days"
        if value == "one":
            u["step"] = "single_day"
            ask_single_day(chat_id)
        else:
            u["step"] = "first_day"
            ask_first_day(chat_id)
        return

    if kind == "part":
        u["answers"]["part_of_day"] = PART_LABELS[value]
        if value == "after":
            u["step"] = "time_text"
            ask_time_text(chat_id)
        else:
            u["step"] = "reason"
            ask_reason(chat_id)
        return

    if kind == "reason":
        u["answers"]["reason"] = ""
        u["step"] = "confirm"
        ask_confirm(chat_id, u)
        return

    if kind == "confirm":
        if value == "yes":
            u["step"] = None
            if u.get("mode") == "B":
                submit_mode_b(chat_id, u)
            else:
                submit_mode_a(chat_id, u)
        else:
            reset_answers(u)
            ask_absence_start(chat_id)
        return


def process_update(update: dict):
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        u = get_user(chat_id)
        answer_callback(cq["id"])
        handle_callback(chat_id, u, cq.get("data", ""), cq.get("from", {}))
        return

    message = update.get("message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    u = get_user(chat_id)

    if "contact" in message:
        handle_contact(chat_id, u, message["contact"])
        return

    text = message.get("text", "")
    if text:
        handle_text(chat_id, u, text, message.get("from", {}))


# --------------------------------------------------------------------------
# HTTP plumbing (same shape as v1 -- webhook, not polling; see that file's
# docstring for why: Railway's free tier needs a serverless HTTP service)
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vs-absence-bot-v2"

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self._send(200, b"vs-absence-bot-v2 is awake\n")

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def do_POST(self):
        raw = self._read_body()
        if self.path != URL_PATH:
            self._send(404, b"not found\n")
            return
        if SECRET_TOKEN and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET_TOKEN:
            self._send(403, b"forbidden\n")
            return
        try:
            update = json.loads(raw or b"{}")
        except Exception as exc:  # noqa: BLE001
            logger.error("Bad update JSON: %s", exc)
            self._send(200)
            return

        # Answer Telegram immediately, do the work in a thread -- keeps
        # webhook round-trips fast and avoids Telegram retry storms.
        self._send(200)
        threading.Thread(target=process_update, args=(update,), daemon=True).start()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")
    threading.Thread(target=ensure_webhook, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logger.info("VS Absence bot v2 listening on port %s (webhook path %s)", PORT, URL_PATH)
    server.serve_forever()


if __name__ == "__main__":
    main()
