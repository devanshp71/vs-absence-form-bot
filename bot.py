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
Phone, side, name, and any in-progress absence report are kept in the
`users` dict in memory AND mirrored to a JSON file on a mounted Railway
Volume (see DATA_DIR below) after every update. Railway's free/hobby tier
fully stops the container after a few minutes of no traffic and starts a
fresh process on the next request -- that used to silently wipe `users`
back to empty, which is exactly the bug reported 8/15/2026 (phone/name/
side "not saving"). On startup the bot now reloads that JSON file first,
so a volunteer's profile (and even a half-finished report) survives the
container going to sleep and waking back up. If DATA_DIR isn't backed by
a real Volume (e.g. running locally, or the Volume isn't attached), saving
just fails silently and behavior falls back to the old memory-only mode.

HOW IDENTITY WORKS
-------------------
Telegram's "share contact" button hands back the phone number attached to
the person's own Telegram account -- they can't type someone else's number
in by accident, and it's the same phone-first matching the backend already
uses to find the right roster row. Side (E/I) and name are asked once and
then reused; name is offered as a guess from their Telegram profile first
("We have you as X -- right?") so most people just tap Yes.

FAST-TRACKING SICK-DAY CALL-OUTS (added 8/16/2026)
----------------------------------------------------
Feedback: the full question-by-question flow is the right amount of detail
for a planned, advance-notice absence (coverage planning genuinely needs
the date range / part-of-day / time specifics) but way too many taps for
"I'm sick, I'm out today" -- at that point who's-out-and-when is what
matters, not a six-question form. Three changes address that, all living
alongside the original flow rather than replacing it:

  1. Split entry point -- once a profile exists, starting a report first
     asks "Calling out today/tomorrow?" vs "Planning ahead?" (ask_entry_choice).
     "Planning ahead" is the untouched original flow. "Calling out" is new.
  2. Shrunk sick-day path -- picking Today/Tomorrow (ask_sick_day) sets
     one_or_multiple/single_day/part_of_day automatically (single day,
     full day -- the overwhelmingly common case for a same-day call-out)
     and skips straight to a quick-reason step (ask_reason_quick: Sick /
     Family emergency / Car trouble / Other / Skip) then confirmation.
     Two taps total instead of the full flow.
  3. Natural-language shortcut -- typing something like "sick today" or
     "calling out tomorrow" as a plain message (when idle, i.e. no step in
     progress) is parsed directly by try_quick_absence() into the same
     single-day/full-day answers and drops straight to the confirm screen.
     One message, one tap ("Looks good") and it's submitted.
"""

import calendar
import datetime
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
# Per-user state -- in memory, mirrored to disk (see module docstring)
# --------------------------------------------------------------------------

# users[chat_id] = {
#   "mode": "A" | "B",
#   "phone": str, "side": "e"|"i", "first_name": str, "last_name": str,
#   "step": str,                # current question we're waiting on
#   "answers": {...},           # in-progress absence report
# }
users: dict = {}
_lock = threading.Lock()

# Mount a Railway Volume at this path and the JSON file below survives a
# container restart/sleep-wake cycle. Without a Volume attached, DATA_DIR
# just points at ordinary (ephemeral) container disk -- saving still works
# for as long as that particular container is alive, but a fresh container
# starts from an empty file again, same as the old memory-only behavior.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATE_FILE = os.path.join(DATA_DIR, "users.json")


def load_users() -> None:
    global users
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        with _lock:
            users = {int(k): v for k, v in raw.items()}
        logger.info("Loaded %d saved user(s) from %s", len(users), STATE_FILE)
    except FileNotFoundError:
        logger.info("No saved state file at %s yet -- starting fresh.", STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load saved state from %s: %s", STATE_FILE, exc)


def save_users() -> None:
    """Best-effort, atomic (write-temp-then-rename) snapshot of `users` to
    disk. Called after every update is processed. Never raises -- a save
    failure should not take the bot down mid-conversation."""
    try:
        with _lock:
            snapshot = json.dumps({str(k): v for k, v in users.items()})
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp_path = STATE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(snapshot)
        os.replace(tmp_path, STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to save state to %s: %s", STATE_FILE, exc)


def get_user(chat_id) -> dict:
    with _lock:
        return users.setdefault(chat_id, {"mode": "A"})


def reset_answers(u: dict) -> None:
    u["step"] = "entry"
    u["answers"] = {}
    u.pop("_range_start", None)
    u.pop("cal_year", None)
    u.pop("cal_month", None)


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


def edit_markup(chat_id, message_id, reply_markup):
    call(
        "editMessageReplyMarkup",
        {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
    )


def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup if reply_markup is not None else {"inline_keyboard": []},
    }
    call("editMessageText", payload)


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


_WEEKDAY_HEADERS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]
_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _fmt_date(d: "datetime.date") -> str:
    return f"{d.month}/{d.day}/{d.year}"


def _shift_month(year: int, month: int, delta: int):
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def build_calendar_kb(year: int, month: int, min_date: "datetime.date | None" = None):
    """
    Flight-picker-style date grid as an inline keyboard. Days before today
    (or before min_date, for the "last day" of a range -- so you can't pick
    an end date earlier than the start date) render as a disabled '·12'
    label with a no-op callback; everything else is tappable.
    """
    weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(year, month)
    today = datetime.date.today()
    floor = max(today, min_date) if min_date else today

    prev_y, prev_m = _shift_month(year, month, -1)
    next_y, next_m = _shift_month(year, month, 1)
    can_go_prev = (year, month) > (floor.year, floor.month)

    rows = [
        [
            ("‹", f"cal:nav:{prev_y}-{prev_m:02d}") if can_go_prev else (" ", "cal:noop"),
            (f"{_MONTH_NAMES[month - 1]} {year}", "cal:noop"),
            ("›", f"cal:nav:{next_y}-{next_m:02d}"),
        ],
        [(w, "cal:noop") for w in _WEEKDAY_HEADERS],
    ]
    for week in weeks:
        row = []
        for day in week:
            if day == 0:
                row.append((" ", "cal:noop"))
                continue
            d = datetime.date(year, month, day)
            if d < floor:
                row.append((f"·{day}", "cal:noop"))
            else:
                row.append((str(day), f"cal:pick:{d.isoformat()}"))
        rows.append(row)
    return kb(rows)


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


def ask_entry_choice(chat_id):
    send_message(
        chat_id,
        "What's this for?",
        kb(
            [
                [("🤒 Calling out today/tomorrow", "entry:sick")],
                [("📅 Planning ahead", "entry:planned")],
            ]
        ),
    )


def ask_sick_day(chat_id, u):
    u["answers"] = {}
    u.pop("_range_start", None)
    u["step"] = "sickday"
    send_message(chat_id, "When?", kb([[("Today", "sickday:today"), ("Tomorrow", "sickday:tomorrow")]]))


def ask_reason_quick(chat_id):
    send_message(
        chat_id,
        "Quick reason? (optional)",
        kb(
            [
                [("Sick", "reasonquick:Sick"), ("Family emergency", "reasonquick:Family emergency")],
                [("Car trouble", "reasonquick:Car trouble"), ("Other", "reasonquick:other")],
                [("Skip", "reasonquick:")],
            ]
        ),
    )


_QUICK_ABSENCE_TRIGGERS = (
    "sick", "calling out", "call out", "won't be in", "wont be in",
    "can't come in", "cant come in", "not coming in", "absent",
)


def try_quick_absence(text: str) -> "datetime.date | None":
    """
    Parses free-typed messages like "sick today" or "calling out tomorrow"
    into a date, so a volunteer can skip straight to the confirm screen
    without touching a single button. Only ever consulted when there's no
    step in progress (see handle_text) -- it never intercepts an answer to
    an actual question. See FAST-TRACKING SICK-DAY CALL-OUTS in the module
    docstring.
    """
    t = text.lower()
    if "tomorrow" in t:
        day = datetime.date.today() + datetime.timedelta(days=1)
    elif "today" in t:
        day = datetime.date.today()
    else:
        return None
    if not any(k in t for k in _QUICK_ABSENCE_TRIGGERS):
        return None
    return day


def ask_absence_start(chat_id):
    send_message(
        chat_id,
        "Got it. Now the absence itself -- one day, or multiple days?",
        kb([[("Just one day", "days:one"), ("Multiple days", "days:multi")]]),
    )


def ask_single_day(chat_id, u):
    today = datetime.date.today()
    u["cal_year"], u["cal_month"] = today.year, today.month
    send_message(
        chat_id,
        "Which day will you be away? Tap a date:",
        build_calendar_kb(today.year, today.month),
    )


def ask_first_day(chat_id, u):
    today = datetime.date.today()
    u["cal_year"], u["cal_month"] = today.year, today.month
    u.pop("_range_start", None)
    send_message(
        chat_id,
        "What's the first day you'll be away? Tap a date:",
        build_calendar_kb(today.year, today.month),
    )


def ask_last_day(chat_id, u, min_date=None):
    if min_date:
        year, month = min_date.year, min_date.month
    else:
        today = datetime.date.today()
        year, month = today.year, today.month
    u["cal_year"], u["cal_month"] = year, month
    label = f" (on or after {_fmt_date(min_date)})" if min_date else ""
    send_message(
        chat_id,
        f"And the last day?{label} Tap a date:",
        build_calendar_kb(year, month, min_date=min_date),
    )


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
            ask_entry_choice(chat_id)
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
        ask_entry_choice(chat_id)
        return
    if step == "single_day":
        u["answers"]["single_day"] = text.strip()
        u["step"] = "part_of_day"
        ask_part_of_day(chat_id)
        return
    if step == "first_day":
        u["answers"]["first_day"] = text.strip()
        u["step"] = "last_day"
        ask_last_day(chat_id, u)
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

    # No step in progress -- treat any message as "let's start." First check
    # whether it's already enough on its own ("sick today") to skip straight
    # to the confirm screen; see try_quick_absence().
    if has_profile(u):
        quick_day = try_quick_absence(text)
        if quick_day is not None:
            u["answers"] = {
                "one_or_multiple": "Just one day",
                "single_day": _fmt_date(quick_day),
                "part_of_day": PART_LABELS["full"],
                "reason": "Sick" if "sick" in text.lower() else "",
            }
            u.pop("_range_start", None)
            u["step"] = "confirm"
            ask_confirm(chat_id, u)
            return
        reset_answers(u)
        ask_entry_choice(chat_id)
    else:
        u["step"] = "phone"
        start_profile(chat_id)


def handle_contact(chat_id, u, contact):
    u["phone"] = contact.get("phone_number", "").lstrip("+")
    u["step"] = "side"
    send_message(chat_id, "Thanks!", remove_kb())
    ask_side(chat_id)


def handle_callback(chat_id, u, data, from_user, message_id=None):
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
            ask_entry_choice(chat_id)
        else:
            u["step"] = "first_name_typed"
            send_message(chat_id, "No problem -- what's your first name?")
        return

    if kind == "entry":
        if value == "sick":
            ask_sick_day(chat_id, u)
        else:
            ask_absence_start(chat_id)
        return

    if kind == "sickday":
        day = datetime.date.today() if value == "today" else datetime.date.today() + datetime.timedelta(days=1)
        u["answers"]["one_or_multiple"] = "Just one day"
        u["answers"]["single_day"] = _fmt_date(day)
        u["answers"]["part_of_day"] = PART_LABELS["full"]
        u["step"] = "reason"
        ask_reason_quick(chat_id)
        return

    if kind == "reasonquick":
        if value == "other":
            u["step"] = "reason"
            ask_reason(chat_id)
        else:
            u["answers"]["reason"] = value
            u["step"] = "confirm"
            ask_confirm(chat_id, u)
        return

    if kind == "days":
        u["answers"]["one_or_multiple"] = "Just one day" if value == "one" else "Multiple days"
        if value == "one":
            u["step"] = "single_day"
            ask_single_day(chat_id, u)
        else:
            u["step"] = "first_day"
            ask_first_day(chat_id, u)
        return

    if kind == "cal":
        # Flight-picker-style date selection. "nav" re-renders the same
        # message's keyboard on a new month; "pick" records a date and,
        # for a range, walks straight into picking the end date in the
        # SAME message rather than sending a new one.
        sub, _, val = value.partition(":")
        if sub == "noop":
            return
        if sub == "nav":
            y_str, m_str = val.split("-")
            year, month = int(y_str), int(m_str)
            u["cal_year"], u["cal_month"] = year, month
            min_date = u.get("_range_start") if u.get("step") == "last_day" else None
            if message_id:
                edit_markup(chat_id, message_id, build_calendar_kb(year, month, min_date=min_date))
            return
        if sub == "pick":
            picked = datetime.date.fromisoformat(val)
            step = u.get("step")
            if step == "single_day":
                u["answers"]["single_day"] = _fmt_date(picked)
                if message_id:
                    edit_message(chat_id, message_id, f"📅 Away on <b>{_fmt_date(picked)}</b>")
                u["step"] = "part_of_day"
                ask_part_of_day(chat_id)
                return
            if step == "first_day":
                u["_range_start"] = picked
                u["answers"]["first_day"] = _fmt_date(picked)
                u["step"] = "last_day"
                u["cal_year"], u["cal_month"] = picked.year, picked.month
                if message_id:
                    edit_message(
                        chat_id,
                        message_id,
                        f"First day: <b>{_fmt_date(picked)}</b>\nNow tap your last day:",
                        build_calendar_kb(picked.year, picked.month, min_date=picked),
                    )
                else:
                    ask_last_day(chat_id, u, min_date=picked)
                return
            if step == "last_day":
                start = u.get("_range_start")
                if start and picked < start:
                    return
                u["answers"]["last_day"] = _fmt_date(picked)
                u["answers"]["part_of_day"] = ""
                if message_id:
                    label = f"{_fmt_date(start)} to {_fmt_date(picked)}" if start else _fmt_date(picked)
                    edit_message(chat_id, message_id, f"📅 Away <b>{label}</b>")
                u.pop("_range_start", None)
                u["step"] = "reason"
                ask_reason(chat_id)
                return
            return
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
            ask_entry_choice(chat_id)
        return


def process_update(update: dict):
    try:
        _dispatch_update(update)
    finally:
        # Persist after every update, whatever happened -- see DATA_DIR /
        # load_users() / save_users() above. Cheap at this bot's traffic
        # volume, and it's exactly what stops a container sleep/restart
        # from losing phone/name/side or an in-progress report.
        save_users()


def _dispatch_update(update: dict):
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        message_id = cq["message"].get("message_id")
        u = get_user(chat_id)
        answer_callback(cq["id"])
        handle_callback(chat_id, u, cq.get("data", ""), cq.get("from", {}), message_id)
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
    load_users()
    threading.Thread(target=ensure_webhook, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logger.info("VS Absence bot v2 listening on port %s (webhook path %s)", PORT, URL_PATH)
    server.serve_forever()


if __name__ == "__main__":
    main()
