"""
VS Volunteer Absence -- Form Link Bot
=====================================

A minimal Telegram bot. When anyone sends it any message, it replies with a
short greeting and the link to the Microsoft Form used to report absences.

This is a SEPARATE bot from the existing `vs-absence-bot`. It runs with its
own bot token, supplied through the BOT_TOKEN environment variable, and does
not touch or depend on the original bot in any way.

To change the greeting or the form link, edit the two constants below.

-------------------------------------------------------------------------------
WHY THIS IS A WEBHOOK AND NOT A POLLING BOT
-------------------------------------------------------------------------------
The first version of this bot called `run_polling()`, which needs a process
running 24 hours a day. Railway's Free plan does not allow that: it requires
every service to be "serverless" (asleep until an HTTP request arrives), and a
polling worker has no HTTP port, so it can never wake up. The deployment failed
with "Free plan deployments must be serverless."

So the bot now runs as a tiny HTTP server instead. Telegram POSTs each incoming
message to it, which is what wakes the container up. It answers the POST with
the reply inline -- Telegram accepts a method call as the webhook response body
-- so the bot never has to make an outbound call at all. One request in, one
reply out, then it goes back to sleep.

That has three benefits: it is free, it is fast (no dependencies to import, so
a cold start is well under a second), and there is no long-running process to
silently die.
-------------------------------------------------------------------------------
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("vs-absence-form-bot")

# The Microsoft Form that collects absence reports.
FORM_URL = (
    "https://forms.office.com/Pages/ResponsePage.aspx?"
    "id=vYPE0EyNF0uHS9KIombwfqWi2RwnglFNix88qhzOJ1tUMzIzSUZXRkVPVUo2RDFTVEtUVE9DRTBLVi4u"
)

# The message sent in response to any incoming message.
GREETING = (
    "Jai Swaminarayan!\n\n"
    "To report any days you will be away from your seva, please fill out "
    "the VS Volunteer Absence Form here:\n\n"
    f"{FORM_URL}\n\n"
    "Please submit it as early as you can so the schedule can be updated. "
    "Thank you!"
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

# Railway sets this automatically once the service has a public domain.
PUBLIC_DOMAIN = (
    os.environ.get("PUBLIC_DOMAIN")
    or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    or ""
).strip()

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _derived(purpose: str) -> str:
    """A stable value derived from the bot token, so no extra secrets to manage."""
    return hmac.new(BOT_TOKEN.encode(), purpose.encode(), hashlib.sha256).hexdigest()[:32]


# Unguessable URL path, so random internet scanners cannot wake the container.
URL_PATH = "/tg/" + _derived("path") if BOT_TOKEN else "/tg/unconfigured"
# Telegram echoes this back in a header; anything without it is rejected.
SECRET_TOKEN = _derived("secret") if BOT_TOKEN else ""


def call(method: str, payload: dict, timeout: int = 15) -> dict:
    """Call the Telegram Bot API. Returns {} on any failure -- never raises."""
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
    """
    Point Telegram at this deployment, but only if it is not already pointed
    here. Skipping the redundant call keeps cold starts fast.
    """
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set -- the bot cannot talk to Telegram.")
        return
    if not PUBLIC_DOMAIN:
        logger.error(
            "No public domain. Generate a domain for this service in Railway "
            "(Settings -> Networking -> Generate Domain), then redeploy."
        )
        return

    wanted = f"https://{PUBLIC_DOMAIN}{URL_PATH}"
    info = call("getWebhookInfo", {}).get("result", {})
    if info.get("url") == wanted:
        logger.info("Webhook already correct: %s", wanted)
        return

    logger.info("Registering webhook: %s (was %r)", wanted, info.get("url"))
    result = call(
        "setWebhook",
        {
            "url": wanted,
            "secret_token": SECRET_TOKEN,
            "allowed_updates": ["message", "edited_message", "channel_post"],
            "max_connections": 10,
        },
    )
    if result.get("ok"):
        logger.info("Webhook registered.")
    else:
        logger.error("Webhook registration failed: %s", result)


def reply_for(update: dict):
    """
    Work out the reply to an update, or None if there is nothing to answer.

    Returned as a Telegram method call, which we hand straight back as the
    webhook response body -- Telegram executes it for us.
    """
    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
    )
    if not message:
        return None
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return None
    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": GREETING,
        "disable_web_page_preview": False,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vs-absence-form-bot"

    def log_message(self, fmt, *args):  # quieter, and routed through logging
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        """Health check / wake-up ping. Never reveals the webhook path."""
        self._send(200, b"vs-absence-form-bot is awake\n")

    def _read_body(self) -> bytes:
        """
        Always drain the request body, even when the answer is a rejection.
        Telegram reuses connections; leaving unread bytes in the socket would
        make the next request on that connection unparseable.
        """
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
        if SECRET_TOKEN and self.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        ) != SECRET_TOKEN:
            logger.warning("Rejected a POST with a bad secret token.")
            self._send(403, b"forbidden\n")
            return

        try:
            update = json.loads(raw or b"{}")
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not parse update: %s", exc)
            self._send(200)  # 200 so Telegram does not retry a bad payload
            return

        answer = reply_for(update)
        if answer is None:
            self._send(200)
            return

        logger.info("Replying to chat %s", answer["chat_id"])
        self._send(200, json.dumps(answer).encode(), "application/json")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is not set. "
            "Set it to the token BotFather gave you for this bot."
        )

    # Register the webhook in the background so the server starts listening
    # immediately -- on a serverless cold start, Telegram's request is already
    # waiting at the door.
    threading.Thread(target=ensure_webhook, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logger.info(
        "VS Absence Form link bot listening on port %s (webhook path %s)",
        PORT,
        URL_PATH,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
