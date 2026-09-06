"""
main.py — Automated Google Sheet Attendance Template Validator Telegram Bot.
=============================================================================
Listens for Google Sheets URLs or Document IDs, fetches sheets via gspread,
and automatically validates whether the layout, headers, and student data strictly
conform to the expected college attendance template.
"""

import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import re
import sys
import threading
from typing import Optional, Tuple

from dotenv import load_dotenv
import gspread
import httpx
from telegram import Update, constants
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from validator import validate_attendance_sheet

# ─────────────────────────────────────────────────────────────────────────────
# 1. Environment variables (strictly loaded via os.getenv)
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN", "")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_CREDS_PATH", "credentials.json")

# ─────────────────────────────────────────────────────────────────────────────
# 2. Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("sheet_validator_bot")

SHEET_URL_REGEX = re.compile(r'https://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9-_]+)')
DOC_ID_REGEX = re.compile(r'^[a-zA-Z0-9-_]{25,60}$')


def escape_md(text: str) -> str:
    """Escape markdown special characters for Telegram legacy Markdown."""
    return re.sub(r'([_*`\[\]])', r'\\\1', str(text))


# ─────────────────────────────────────────────────────────────────────────────
# 3. Render Health Check HTTP Server
# ─────────────────────────────────────────────────────────────────────────────
def start_health_check_server() -> None:
    """Starts a lightweight HTTP server on $PORT for Render health checks."""
    port_env = os.environ.get("PORT", "10000").strip()
    try:
        port = int(port_env)
    except ValueError:
        port = 10000

    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info("Health check HTTP server running on port %d", port)
    except Exception as exc:
        logger.warning("Could not start health check HTTP server: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Google Sheets authentication helper
# ─────────────────────────────────────────────────────────────────────────────
def get_gspread_client() -> Tuple[Optional[gspread.Client], Optional[str], Optional[str]]:
    """
    Initializes gspread client from GOOGLE_CREDENTIALS_JSON or local credentials file.
    Returns: (client, service_account_email, error_message)
    """
    creds_env = GOOGLE_CREDENTIALS_JSON.strip()

    # Case 1: Raw JSON string provided in environment variable
    if creds_env:
        try:
            info = json.loads(creds_env)
            client = gspread.service_account_from_dict(info)
            email = info.get("client_email", "your-service-account@iam.gserviceaccount.com")
            return client, email, None
        except Exception as e:
            logger.error("Failed to parse GOOGLE_CREDENTIALS_JSON string: %s", e)
            return None, None, f"❌ Failed to parse `GOOGLE_CREDENTIALS_JSON`: `{escape_md(str(e))}`"

    # Case 2: File path provided via GOOGLE_CREDS_PATH or local credentials.json
    creds_file = GOOGLE_CREDS_PATH.strip()
    if not os.path.isfile(creds_file):
        local_service_account = os.path.join(os.path.dirname(os.path.abspath(__file__)), "service_account.json")
        if os.path.isfile(local_service_account):
            creds_file = local_service_account

    if os.path.isfile(creds_file):
        try:
            with open(creds_file, "r", encoding="utf-8") as f:
                info = json.load(f)
            client = gspread.service_account(filename=creds_file)
            email = info.get("client_email", "your-service-account@iam.gserviceaccount.com")
            return client, email, None
        except Exception as e:
            logger.error("Failed to load credentials from file '%s': %s", creds_file, e)
            return None, None, f"❌ Failed to load credentials from `{creds_file}`: `{escape_md(str(e))}`"

    return None, None, (
        "⚠️ *Google Service Account Not Configured*\n\n"
        "Neither `GOOGLE_CREDENTIALS_JSON` nor a valid `credentials.json` file was found.\n"
        "Please provide Google service account credentials in `.env` to enable sheet validation."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Sheet validation handler
# ─────────────────────────────────────────────────────────────────────────────
async def process_sheet_validation(update: Update, context: ContextTypes.DEFAULT_TYPE, doc_id: str) -> None:
    """
    Fetches the spreadsheet by doc_id, iterates over all worksheets,
    runs validate_attendance_sheet on each, and sends a formatted Markdown report.
    """
    # 1. Immediate acknowledgement & typing indicator
    ack_msg = await update.message.reply_text("🔍 *Validating sheet template...*", parse_mode="Markdown")
    if update.effective_chat:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=constants.ChatAction.TYPING)

    # 2. Get gspread client
    client, sa_email, err_msg = get_gspread_client()
    if err_msg:
        await ack_msg.edit_text(err_msg, parse_mode="Markdown")
        return

    # 3. Open spreadsheet
    try:
        spreadsheet = client.open_by_key(doc_id)
    except gspread.exceptions.SpreadsheetNotFound:
        email_str = f"`{sa_email}`" if sa_email else "the bot's service account"
        msg = (
            "⚠️ *Permission Denied / Sheet Not Found*\n\n"
            "The bot cannot access this Google Sheet.\n\n"
            "👉 Please ensure you have shared the spreadsheet with **Viewer** access to:\n"
            f"{email_str}\n\n"
            "Also verify that the document link or ID is correct."
        )
        await ack_msg.edit_text(msg, parse_mode="Markdown")
        return
    except gspread.exceptions.APIError as api_err:
        err_str = str(api_err)
        if "PERMISSION_DENIED" in err_str or "403" in err_str or "404" in err_str:
            email_str = f"`{sa_email}`" if sa_email else "the bot's service account"
            msg = (
                "⚠️ *Permission Denied*\n\n"
                "The bot lacks view permission for this Google Sheet.\n\n"
                "👉 Please share the spreadsheet with **Viewer** access to:\n"
                f"{email_str}"
            )
        else:
            msg = f"❌ *Google API Error:* `{escape_md(err_str)}`"
        await ack_msg.edit_text(msg, parse_mode="Markdown")
        return
    except Exception as exc:
        logger.error("Error opening spreadsheet %s: %s", doc_id, exc, exc_info=True)
        await ack_msg.edit_text(f"❌ *Failed to open spreadsheet:* `{escape_md(str(exc))}`", parse_mode="Markdown")
        return

    # 4. Fetch all worksheet tabs (e.g. Sheet1, DSA, Discrete Mathematics)
    try:
        worksheets = spreadsheet.worksheets()
    except Exception as exc:
        await ack_msg.edit_text(f"❌ *Failed to inspect sheet tabs:* `{escape_md(str(exc))}`", parse_mode="Markdown")
        return

    if not worksheets:
        await ack_msg.edit_text("❌ *The spreadsheet contains no worksheets.*", parse_mode="Markdown")
        return

    all_errors = []
    all_warnings = []
    total_students = 0
    total_sessions = 0
    tab_reports = []
    all_valid = True

    # 5. Validate each tab
    for ws in worksheets:
        tab_title = ws.title
        res = validate_attendance_sheet(worksheet=ws)
        if not res['valid']:
            all_valid = False
            for err in res['errors']:
                all_errors.append(f"[{tab_title}] {err}")
        else:
            total_students += res['student_count']
            total_sessions += res['session_count']

        for warn in res['warnings']:
            all_warnings.append(f"[{tab_title}] {warn}")

        tab_reports.append({
            'title': tab_title,
            'valid': res['valid'],
            'student_count': res['student_count'],
            'session_count': res['session_count'],
        })

    sheet_title_escaped = escape_md(spreadsheet.title)

    # 6. Format Markdown response
    if all_valid:
        tabs_display = ", ".join([f"`{escape_md(t['title'])}`" for t in tab_reports])
        response = (
            "✅ *Template Verified Successfully*\n\n"
            f"📋 *Spreadsheet:* {sheet_title_escaped}\n"
            f"📁 *Tabs Verified:* {len(worksheets)} tab(s) ({tabs_display})\n\n"
            f"📊 *Summary Metrics:*\n"
            f"• *Student Count Identified:* `{total_students}`\n"
            f"• *Sessions Tracked:* `{total_sessions}`\n"
            f"• *Status:* Strictly conforms to expected attendance schema."
        )
        if all_warnings:
            response += "\n\n⚠️ *Formatting Notes:*\n"
            for w in all_warnings[:3]:
                response += f"• {escape_md(w)}\n"
            if len(all_warnings) > 3:
                response += f"• _...and {len(all_warnings) - 3} more minor note(s)._\n"
    else:
        response = (
            "❌ *Template Mismatch Found*\n\n"
            f"📋 *Spreadsheet:* {sheet_title_escaped}\n\n"
            "*Critical Errors Found (Top 5):*\n"
        )
        for err in all_errors[:5]:
            response += f"• {escape_md(err)}\n"

        if len(all_errors) > 5:
            response += f"\n_...and {len(all_errors) - 5} more error(s)._\n"

        response += (
            f"\n*(Total critical errors: {len(all_errors)})*\n\n"
            "Please fix the headers, sequences, or values in your Google Sheet and send the link again."
        )

    try:
        await ack_msg.edit_text(response, parse_mode="Markdown")
    except Exception as send_err:
        logger.warning("Markdown formatting rejected (%s). Sending plain text fallback.", send_err)
        plain = response.replace("*", "").replace("`", "").replace("_", "")
        await ack_msg.edit_text(plain)


async def sheet_link_listener(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detects Google Sheets links or document IDs in incoming messages."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # Match Google Sheets URL
    url_match = SHEET_URL_REGEX.search(text)
    if url_match:
        doc_id = url_match.group(1)
        await process_sheet_validation(update, context, doc_id)
        return

    # Match standalone Document ID
    if DOC_ID_REGEX.match(text) and not text.startswith("/"):
        await process_sheet_validation(update, context, text)
        return


async def cmd_validate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command handler: /validate <url_or_id>"""
    if not context.args:
        await update.message.reply_text(
            "📋 *Google Sheet Template Validator*\n\n"
            "Send or share any Google Sheets link to validate its attendance template.\n\n"
            "*Usage:*\n"
            "Simply paste the link directly in this chat, or type:\n"
            "`/validate <Google_Sheet_URL_or_ID>`",
            parse_mode="Markdown",
        )
        return

    arg = context.args[0].strip()
    url_match = SHEET_URL_REGEX.search(arg)
    doc_id = url_match.group(1) if url_match else arg
    await process_sheet_validation(update, context, doc_id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — Greeting and instructions."""
    await update.message.reply_text(
        "👋 *Welcome to the Attendance Template Validator Bot!*\n\n"
        "📊 *How to validate a Google Sheet:*\n"
        "Just paste any Google Sheets link directly into this chat, or use:\n"
        "`/validate <Google_Sheet_URL>`\n\n"
        "The bot will automatically check:\n"
        "• Required headers in exact sequence (`['S.NO', 'Roll NO', 'Reg.NO', 'Student Name', 'Present', 'Absent', 'Percentage']`)\n"
        "• Date session columns (`DD/MM/YYYY`)\n"
        "• Incremental S.NO & 11-digit Reg.NO\n"
        "• Valid attendance marks (`P`, `A`, or blank)\n\n"
        "Type /help for more information.",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — Guide and usage."""
    await update.message.reply_text(
        "🤖 *Attendance Template Validator — Help*\n\n"
        "✨ *Features:*\n"
        "• Paste any Google Sheets URL or document ID to validate its layout\n"
        "• Verifies all tabs (`Sheet1`, `DSA`, `Discrete Mathematics`, etc.)\n"
        "• Displays metrics (Student Count, Sessions tracked) if valid\n"
        "• Lists top critical errors if invalid\n\n"
        "📌 *Commands:*\n"
        "• `/validate <URL>` — Validate a specific spreadsheet\n"
        "• `/help` — Show this help message\n\n"
        "💡 *Sharing Access:* Make sure to share your Google Sheet with **Viewer** access "
        "to the service account email configured on the bot.",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Application entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN / BOT_TOKEN is not set in environment.")
        sys.exit(1)

    # Start HTTP server for Render health checks
    start_health_check_server()

    logger.info("Starting Attendance Template Validator Bot…")

    # Clean up any leftover webhooks
    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
                json={"drop_pending_updates": True},
            )
            logger.info("deleteWebhook response: %s", r.json())
    except Exception as exc:
        logger.warning("Could not delete webhook: %s", exc)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("validate", cmd_validate))

    # Link listener for Google Sheets URL or raw document ID
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, sheet_link_listener))

    logger.info("Bot is live and listening for messages...")
    sys.stdout.flush()

    # Ensure an active event loop exists for Python 3.12+ / 3.14 compatibility
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
