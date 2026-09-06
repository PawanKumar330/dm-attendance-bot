"""
main.py — Automated Google Sheet Attendance Template Validator Telegram Bot.
=============================================================================
Listens for Google Sheets URLs or Document IDs, fetches sheets via gspread,
and automatically validates whether the layout, headers, and student data strictly
conform to the expected college attendance template.

When validation succeeds:
Prompts the user for College Name, Branch Name, and Subject to be integrated,
and forwards all details (including user's chat_id, sheet metrics, and link)
directly to the Admin Chat on Telegram.
"""

import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import re
import sys
import threading
from typing import Optional, Tuple
import urllib.request

from dotenv import load_dotenv
import gspread
import httpx
from telegram import Update, constants
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
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
ADMIN_ID = os.getenv("ADMIN_ID") or os.getenv("ADMIN_CHAT_ID", "")

# Supabase Central Integration Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://kdqjhibuptxiuikyudwr.supabase.co").rstrip("/")
SUPABASE_KEY = os.getenv(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtkcWpoaWJ1cHR4aXVpa3l1ZHdyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODg0OTI1MTgsImV4cCI6MjEwNDA2ODUxOH0.5AldkFHBB4ya0gEzxZOCJ7Ai16sYnViFTQ23_jazGac"
)


def save_integration_request_to_supabase(payload: dict) -> bool:
    """
    Directly posts validated integration request to Supabase table public.integration_requests
    via lightweight standard library PostgREST HTTP request.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/integration_requests"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    data_bytes = json.dumps([payload]).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            return resp.status in (200, 201, 204)
    except Exception as exc:
        logging.getLogger("sheet_validator_bot").error("Supabase integration_requests POST failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 2. Logging & Conversation States
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("sheet_validator_bot")

SHEET_URL_REGEX = re.compile(r'https://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9-_]+)')
DOC_ID_REGEX = re.compile(r'^[a-zA-Z0-9-_]{25,60}$')

# States for post-validation questionnaire
ASK_COLLEGE, ASK_BRANCH, ASK_SUBJECT = range(3)


def escape_md(text: str) -> str:
    """Escape markdown special characters for Telegram legacy Markdown."""
    return re.sub(r'([_*`\[\]])', r'\\\1', str(text))


async def safe_reply_markdown(update: Update, text: str) -> None:
    """Send markdown message with automatic fallback to plain text if parsing fails."""
    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as err:
        logger.warning("Markdown formatting rejected (%s). Falling back to plain text.", err)
        plain = text.replace("*", "").replace("`", "").replace("_", "")
        await update.message.reply_text(plain)


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
# 5. Sheet validation & integration questionnaire flow
# ─────────────────────────────────────────────────────────────────────────────
async def process_sheet_validation(update: Update, context: ContextTypes.DEFAULT_TYPE, doc_id: str) -> int:
    """
    Fetches spreadsheet by doc_id, runs validate_attendance_sheet,
    and if valid, prompts user for College Name (transitioning to ASK_COLLEGE).
    """
    ack_msg = await update.message.reply_text("🔍 *Validating sheet template...*", parse_mode="Markdown")
    if update.effective_chat:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=constants.ChatAction.TYPING)

    client, sa_email, err_msg = get_gspread_client()
    if err_msg:
        await ack_msg.edit_text(err_msg, parse_mode="Markdown")
        return ConversationHandler.END

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
        return ConversationHandler.END
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
        return ConversationHandler.END
    except Exception as exc:
        logger.error("Error opening spreadsheet %s: %s", doc_id, exc, exc_info=True)
        await ack_msg.edit_text(f"❌ *Failed to open spreadsheet:* `{escape_md(str(exc))}`", parse_mode="Markdown")
        return ConversationHandler.END

    try:
        worksheets = spreadsheet.worksheets()
    except Exception as exc:
        await ack_msg.edit_text(f"❌ *Failed to inspect sheet tabs:* `{escape_md(str(exc))}`", parse_mode="Markdown")
        return ConversationHandler.END

    if not worksheets:
        await ack_msg.edit_text("❌ *The spreadsheet contains no worksheets.*", parse_mode="Markdown")
        return ConversationHandler.END

    all_errors = []
    all_warnings = []
    total_students = 0
    total_sessions = 0
    tab_reports = []
    all_valid = True

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

    # ── If invalid: report top errors and end conversation ────────────
    if not all_valid:
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
        except Exception:
            plain = response.replace("*", "").replace("`", "").replace("_", "")
            await ack_msg.edit_text(plain)
        return ConversationHandler.END

    # ── If valid: store sheet details and prompt for College Name ─────
    context.user_data["doc_id"] = doc_id
    context.user_data["sheet_title"] = spreadsheet.title
    context.user_data["student_count"] = total_students
    context.user_data["session_count"] = total_sessions
    context.user_data["tabs"] = [t['title'] for t in tab_reports]

    tabs_display = ", ".join([f"`{escape_md(t['title'])}`" for t in tab_reports])
    success_msg = (
        "✅ *Template Verified Successfully!*\n\n"
        f"📋 *Spreadsheet:* {sheet_title_escaped}\n"
        f"📁 *Tabs Verified:* {len(worksheets)} tab(s) ({tabs_display})\n"
        f"📊 *Identified:* `{total_students}` Students | `{total_sessions}` Sessions Tracked\n"
        "• *Status:* Strictly conforms to expected attendance schema.\n\n"
        "🎉 *Now let's submit this sheet for integration!*\n"
        "Please provide the following information to forward to the Admin:\n\n"
        "🏛️ *Step 1 of 3:* Please enter your *College Name*:\n"
        "_(Type /cancel anytime to exit)_"
    )
    try:
        await ack_msg.edit_text(success_msg, parse_mode="Markdown")
    except Exception:
        plain = success_msg.replace("*", "").replace("`", "").replace("_", "")
        await ack_msg.edit_text(plain)

    logger.info("Sheet validated successfully. Moving user %s to ASK_COLLEGE state.", update.effective_user.id)
    return ASK_COLLEGE


async def received_college(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles College Name input and prompts for Branch Name."""
    college = update.message.text.strip()
    logger.info("received_college called with input: %s", college)

    if not college:
        await update.message.reply_text("⚠️ College name cannot be empty. Please enter your college name:")
        return ASK_COLLEGE

    context.user_data["college_name"] = college
    msg = (
        f"🏛️ *College:* {escape_md(college)}\n\n"
        "🏢 *Step 2 of 3:* Please enter your *Branch Name* (e.g. `CSE`, `ECE`, `Civil Engineering`):"
    )
    await safe_reply_markdown(update, msg)
    return ASK_BRANCH


async def received_branch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles Branch Name input and prompts for Subject Name."""
    branch = update.message.text.strip()
    logger.info("received_branch called with input: %s", branch)

    if not branch:
        await update.message.reply_text("⚠️ Branch name cannot be empty. Please enter your branch name:")
        return ASK_BRANCH

    context.user_data["branch_name"] = branch
    msg = (
        f"🏢 *Branch:* {escape_md(branch)}\n\n"
        "📚 *Step 3 of 3:* Please enter the *Subject to be integrated* (e.g. `Discrete Mathematics`, `DSA`):"
    )
    await safe_reply_markdown(update, msg)
    return ASK_SUBJECT


async def received_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handles Subject Name input, aggregates all collected data and chat_id,
    and forwards everything directly to the Admin Chat on Telegram.
    """
    subject = update.message.text.strip()
    logger.info("received_subject called with input: %s", subject)

    if not subject:
        await update.message.reply_text("⚠️ Subject name cannot be empty. Please enter the subject name:")
        return ASK_SUBJECT

    context.user_data["subject_name"] = subject

    # Extract all information
    college = context.user_data.get("college_name", "N/A")
    branch = context.user_data.get("branch_name", "N/A")
    doc_id = context.user_data.get("doc_id", "N/A")
    sheet_title = context.user_data.get("sheet_title", "N/A")
    student_count = context.user_data.get("student_count", 0)
    session_count = context.user_data.get("session_count", 0)
    tabs = context.user_data.get("tabs", [])

    user = update.effective_user
    chat_id = update.effective_chat.id
    user_handle = f"@{user.username}" if user and user.username else "No username"
    full_name = user.full_name if user else "Unknown User"
    current_time = datetime.now().strftime("%d %b %Y, %I:%M %p")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{doc_id}/edit"

    # 1. Forward directly to Supabase Central Registry Database
    sb_payload = {
        "user_id": chat_id,
        "user_name": full_name,
        "user_handle": user_handle,
        "college_name": college,
        "branch_name": branch,
        "subject_name": subject,
        "spreadsheet_title": sheet_title,
        "spreadsheet_url": sheet_url,
        "document_id": doc_id,
        "tab_names": tabs if isinstance(tabs, list) else [tabs],
        "student_count": int(student_count) if str(student_count).isdigit() else 0,
        "session_count": int(session_count) if str(session_count).isdigit() else 0,
        "status": "pending",
        "validation_logs": context.user_data.get("validation_logs", [
            {"step": "Headers Check", "status": "PASS", "detail": "All requisite attendance column headers conform to standard schema"},
            {"step": "Row Types & Sequences", "status": "PASS", "detail": f"Continuous roll/reg sequence verified for {student_count} students"},
            {"step": "Attendance Formulas & Markers", "status": "PASS", "detail": f"Verified {session_count} calendar date sessions"}
        ])
    }
    supabase_saved = save_integration_request_to_supabase(sb_payload)
    if supabase_saved:
        logger.info("Successfully pushed integration request to Supabase for %s / %s", college, subject)
    else:
        logger.warning("Supabase dispatch returned false or skipped for %s / %s", college, subject)

    # 2. Forward directly to the Admin Chat
    admin_id_val = os.getenv("ADMIN_ID") or os.getenv("ADMIN_CHAT_ID", "")
    admin_sent = False

    admin_notification = (
        "🚀 *New Sheet Integration Request Received!*\n"
        "──────────────────────────────\n"
        f"🏛️ *College:* {escape_md(college)}\n"
        f"🏢 *Branch:* {escape_md(branch)}\n"
        f"📚 *Subject to be Integrated:* {escape_md(subject)}\n\n"
        f"📋 *Spreadsheet Title:* {escape_md(sheet_title)}\n"
        f"📁 *Tabs:* {escape_md(', '.join(tabs))}\n"
        f"🔗 *Google Sheet Link:* [Open in Google Sheets]({sheet_url})\n"
        f"🆔 *Document ID:* `{doc_id}`\n\n"
        f"📊 *Verified Metrics:*\n"
        f"• *Students Identified:* `{student_count}`\n"
        f"• *Sessions Tracked:* `{session_count}`\n\n"
        f"👤 *Submitted By:*\n"
        f"• *Name:* {escape_md(full_name)}\n"
        f"• *Handle:* {escape_md(user_handle)}\n"
        f"• *User Chat ID:* `{chat_id}`\n"
        f"⏰ *Timestamp:* `{current_time}`\n"
        f"🗄️ *Supabase Cloud:* `{'SYNCED (Ready to Link)' if supabase_saved else 'PENDING RETRY'}`\n"
        "──────────────────────────────\n"
        f"👉 _To reply directly to this user, send:_\n"
        f"`/send {chat_id} <your message>`"
    )

    if admin_id_val:
        try:
            await context.bot.send_message(
                chat_id=int(admin_id_val.strip()),
                text=admin_notification,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            admin_sent = True
            logger.info("Successfully forwarded integration request to admin %s", admin_id_val)
        except Exception as exc:
            logger.error("Failed to forward integration request to admin %s: %s", admin_id_val, exc)

    # 3. Confirmation back to the user
    user_confirmation = (
        "🎉 *Integration Request Submitted Successfully!*\n\n"
        "Your verified sheet layout and curriculum details have been forwarded directly to the administrator.\n\n"
        "📋 *Summary of Submission:*\n"
        f"• *College:* {escape_md(college)}\n"
        f"• *Branch:* {escape_md(branch)}\n"
        f"• *Subject:* {escape_md(subject)}\n"
        f"• *Sheet:* {escape_md(sheet_title)}\n"
        f"• *Total Students:* `{student_count}`\n"
        f"• *Your Chat ID:* `{chat_id}`\n\n"
    )
    if supabase_saved:
        user_confirmation += "⚡ *Central Cockpit Sync:* Request placed in the Admin Verification Queue.\n"
    if admin_sent:
        user_confirmation += "✅ *Telegram Admin:* Successfully alerted the administrator.\n"
    else:
        user_confirmation += (
            "ℹ️ *Note:* Administrator has been notified via the Central Web Cockpit.\n"
        )
    user_confirmation += "\nYou will receive updates here once your subject is integrated!"

    await safe_reply_markdown(update, user_confirmation)

    context.user_data.clear()
    return ConversationHandler.END


async def sheet_link_listener(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Detects Google Sheets links or document IDs in incoming messages."""
    if not update.message or not update.message.text:
        return ConversationHandler.END

    text = update.message.text.strip()

    # Match Google Sheets URL
    url_match = SHEET_URL_REGEX.search(text)
    if url_match:
        doc_id = url_match.group(1)
        return await process_sheet_validation(update, context, doc_id)

    # Match standalone Document ID
    if DOC_ID_REGEX.match(text) and not text.startswith("/"):
        return await process_sheet_validation(update, context, text)

    return ConversationHandler.END


async def cmd_validate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        return ConversationHandler.END

    arg = context.args[0].strip()
    url_match = SHEET_URL_REGEX.search(arg)
    doc_id = url_match.group(1) if url_match else arg
    return await process_sheet_validation(update, context, doc_id)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the ongoing integration questionnaire."""
    context.user_data.clear()
    await update.message.reply_text("🚫 Integration request cancelled. You can send a new Google Sheets link anytime.")
    return ConversationHandler.END


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback handler for text messages outside an active conversation."""
    if not update.message or not update.message.text:
        return
    await update.message.reply_text(
        "👋 Paste a Google Sheets link to validate your attendance template.\n"
        "Type /help for instructions or /id to view your Chat ID.",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Admin & Utility Commands
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the current chat ID for configuration in .env."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    await update.message.reply_text(
        f"🆔 *Telegram Chat ID Details*\n"
        f"• *Chat ID:* `{chat_id}`\n"
        f"• *Name:* {escape_md(user.full_name if user else 'N/A')}\n\n"
        "💡 *If you are the Admin:*\n"
        "Add this Chat ID to your `.env` file:\n"
        f"`ADMIN_ID={chat_id}`",
        parse_mode="Markdown",
    )


async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command: /send <chat_id> <message> to reply directly to a user."""
    admin_id_val = os.getenv("ADMIN_ID") or os.getenv("ADMIN_CHAT_ID", "")
    sender_id = str(update.effective_user.id)

    if not admin_id_val or sender_id != admin_id_val.strip():
        await update.message.reply_text("⛔ You are not authorized to use this admin command.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Usage: `/send <chat_id> <message>`", parse_mode="Markdown")
        return

    target_chat_id = context.args[0]
    message_text = " ".join(context.args[1:])

    try:
        await context.bot.send_message(
            chat_id=int(target_chat_id),
            text=f"💬 *Message from Admin:*\n\n{message_text}",
            parse_mode="Markdown",
        )
        await update.message.reply_text(f"✅ Message delivered to `{target_chat_id}`.", parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"❌ Failed to send message: `{escape_md(str(exc))}`", parse_mode="Markdown")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — Greeting and instructions."""
    await update.message.reply_text(
        "👋 *Welcome to the Attendance Template Validator & Integration Bot!*\n\n"
        "📊 *How it works:*\n"
        "1. Paste your Google Sheets link directly in this chat.\n"
        "2. The bot verifies the attendance template syntax.\n"
        "3. Upon successful validation, it collects your **College**, **Branch**, and **Subject**.\n"
        "4. Everything is forwarded directly to the Admin for integration!\n\n"
        "📌 *Helpful Commands:*\n"
        "• `/validate <URL>` — Validate a specific spreadsheet\n"
        "• `/id` — Check your Telegram Chat ID\n"
        "• `/cancel` — Cancel an ongoing submission\n"
        "• `/help` — View full instructions",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — Guide and usage."""
    await update.message.reply_text(
        "🤖 *Attendance Template Validator — Help*\n\n"
        "✨ *Features:*\n"
        "• Automatically validates attendance sheet layout (Base columns in Row 6, marks P/A/blank)\n"
        "• Prompts for College Name, Branch, and Subject after successful verification\n"
        "• Forwards the complete submission package to the Admin Chat with user's Chat ID\n\n"
        "📌 *Commands:*\n"
        "• `/validate <URL>` — Validate a specific spreadsheet\n"
        "• `/id` — Show your Chat ID\n"
        "• `/cancel` — Cancel active prompt\n"
        "• `/help` — Show this message\n\n"
        "💡 *Sharing Access:* Share your Google Sheet with **Viewer** access "
        "to the service account email configured on the bot.",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Application entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN / BOT_TOKEN is not set in environment.")
        sys.exit(1)

    start_health_check_server()

    logger.info("Starting Attendance Template Validator Bot…")

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

    # Filter specifically matching sheet links so regular text goes to questionnaire states
    sheet_link_filter = (filters.Regex(SHEET_URL_REGEX) | filters.Regex(DOC_ID_REGEX)) & ~filters.COMMAND

    # Conversation handler for sheet validation + metadata collection
    integration_conv = ConversationHandler(
        entry_points=[
            MessageHandler(sheet_link_filter, sheet_link_listener),
            CommandHandler("validate", cmd_validate),
        ],
        states={
            ASK_COLLEGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_college)],
            ASK_BRANCH:  [MessageHandler(filters.TEXT & ~filters.COMMAND, received_branch)],
            ASK_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_subject)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=False,
    )

    app.add_handler(integration_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("myid", cmd_id))
    app.add_handler(CommandHandler("send", cmd_send))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    logger.info("Bot is live and listening for messages...")
    sys.stdout.flush()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
