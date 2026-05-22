"""
VS Volunteer Absence -- Form Link Bot
=====================================

A minimal Telegram bot. When anyone sends it any message, it replies with a
short greeting and the link to the Microsoft Form used to report absences.

This is a SEPARATE bot from the existing `vs-absence-bot`. It runs with its
own bot token, supplied through the BOT_TOKEN environment variable, and does
not touch or depend on the original bot in any way.

To change the greeting or the form link, edit the two constants below.
"""

import logging
import os

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

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


async def send_form_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to any incoming message with the greeting and the form link."""
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(GREETING)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN environment variable is not set. "
            "Set it to the token BotFather gave you for this bot."
        )

    app = ApplicationBuilder().token(token).build()

    # /start and every other message get the exact same reply.
    app.add_handler(CommandHandler("start", send_form_link))
    app.add_handler(MessageHandler(filters.ALL, send_form_link))

    logger.info("VS Absence Form link bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
