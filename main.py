"""
main.py — Discrete Mathematics Attendance Telegram Bot
======================================================
Students can check their own attendance by entering their
Registration Number and Date of Birth via a Telegram bot.

Sheet structure (Discrete Mathematics):
  Rows 1–6  : Empty / title area
  Row  7    : Headers → A=S.NO, B=Roll No, C=Reg No, D=Name,
                         E=Present, F=Absent, G=Percentage,
                         H=DOB  ← you add this column,
                         I+     = daily attendance dates (P/A)
  Row  8+   : Student data

Conversation flow:
  /start  → Ask Reg No → Ask DOB → Show result
  /cancel → Exit at any point

Run locally  : python main.py
Deploy Render: Background Worker → python main.py
"""

import asyncio
import json
import logging
import os
import sys

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────────────────────────────────────
# 1. Environment variables
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
SHEET_ID          = os.environ.get("SHEET_ID", "1f2XM7HFk0IYSiOyKYEKkNMd3j-lB6NgLS0qlh5M3o0M")
GOOGLE_CREDS_PATH = os.environ.get("GOOGLE_CREDS_PATH", "credentials.json")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

# ─────────────────────────────────────────────────────────────────────────────
# 2. Sheet layout constants  (must match your actual Google Sheet)
# ─────────────────────────────────────────────────────────────────────────────
HEADER_ROW     = 7    # Row number that contains column labels
DATA_START_ROW = 8    # First row with actual student data

# 1-based column positions for fixed columns
COL_ROLL       = 2    # B  → Roll No
COL_REG        = 3    # C  → Reg No
COL_NAME       = 4    # D  → Name
COL_PRESENT    = 5    # E  → Present  (formula/count — read-only)
COL_ABSENT     = 6    # F  → Absent   (formula/count — read-only)
COL_PERCENTAGE = 7    # G  → Percentage (formula — read-only)
COL_DATE_START = 8    # H  → First date column (P/A values start here)

# Google API scopes
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ─────────────────────────────────────────────────────────────────────────────
# 3. Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("dm_bot")

# ─────────────────────────────────────────────────────────────────────────────
# 4. Conversation states
# ─────────────────────────────────────────────────────────────────────────────
ASK_REG_NO, ASK_ROLL_NO = range(2)

# ─────────────────────────────────────────────────────────────────────────────
# 5. Google Sheets helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_creds() -> Credentials:
    """
    Build Google credentials from either an env-var JSON string (Render)
    or a local JSON file path (local development).
    """
    if GOOGLE_CREDS_JSON.strip():
        info = json.loads(GOOGLE_CREDS_JSON.strip())
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    if GOOGLE_CREDS_PATH.strip() and os.path.exists(GOOGLE_CREDS_PATH.strip()):
        return Credentials.from_service_account_file(GOOGLE_CREDS_PATH.strip(), scopes=SCOPES)
    raise RuntimeError(
        "No Google credentials found. Set GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDS_PATH."
    )


def _open_worksheet() -> gspread.Worksheet:
    """Opens the Discrete Mathematics spreadsheet, first worksheet."""
    client = gspread.authorize(_build_creds())
    spreadsheet = client.open_by_key(SHEET_ID.strip())
    worksheet = spreadsheet.sheet1      # gid=0, first tab
    logger.info("Opened worksheet '%s'.", worksheet.title)
    return worksheet


def lookup_student(reg_no: str, roll_no: str) -> dict:
    """
    Finds a student row by Reg No and verifies their Roll No.

    Returns one of:
      {"status": "found",  "name": ..., "present": ..., "absent": ...,
       "percentage": ..., "attendance_log": [(date_label, status), ...]}
      {"status": "reg_not_found"}
      {"status": "roll_mismatch"}
      {"status": "sheet_error", "detail": ...}

    attendance_log is a list of (date_str, "P" | "A" | "-") tuples
    from column H onwards where a date header exists.
    """
    try:
        ws = _open_worksheet()
        all_values = ws.get_all_values()   # single API call — list of lists

        reg_no_clean  = reg_no.strip().upper()
        roll_no_clean = roll_no.strip()

        # ── Extract date headers from Row 7 (index 6) ──────────────────────
        # Date columns start at COL_DATE_START (1-based) = index COL_DATE_START-1
        header_row  = all_values[HEADER_ROW - 1] if len(all_values) >= HEADER_ROW else []
        date_headers = []
        for i, cell in enumerate(header_row[COL_DATE_START - 1:], start=COL_DATE_START):
            label = cell.strip()
            if label:   # only keep columns that actually have a date label
                date_headers.append((i, label))   # (1-based col index, label string)

        # ── Scan data rows ──────────────────────────────────────────────────
        data_rows = all_values[DATA_START_ROW - 1:]

        for row in data_rows:
            # Pad row so index lookups never raise IndexError
            max_col_needed = max(
                COL_REG, COL_ROLL, COL_NAME,
                COL_PRESENT, COL_ABSENT, COL_PERCENTAGE,
                *(col_idx for col_idx, _ in date_headers) if date_headers else [0]
            )
            while len(row) < max_col_needed:
                row.append("")

            row_reg  = row[COL_REG  - 1].strip().upper()
            row_name = row[COL_NAME - 1].strip()

            if row_reg != reg_no_clean:
                continue

            # ── Reg No matched — verify Roll No ────────────────────────────
            row_roll = row[COL_ROLL - 1].strip()
            if row_roll != roll_no_clean:
                logger.info("Roll No mismatch for reg=%s (sheet=%s, input=%s)",
                            reg_no_clean, row_roll, roll_no_clean)
                return {"status": "roll_mismatch"}

            # ── Both matched — read summary stats ──────────────────────────
            present    = row[COL_PRESENT    - 1].strip() or "0"
            absent     = row[COL_ABSENT     - 1].strip() or "0"
            percentage = row[COL_PERCENTAGE - 1].strip() or "N/A"

            # ── Build date-wise log from H onwards ─────────────────────────
            attendance_log = []
            for col_idx, date_label in date_headers:
                cell_val = row[col_idx - 1].strip().upper() if col_idx <= len(row) else ""
                if cell_val in ("P", "PRESENT"):
                    status_char = "P"
                elif cell_val in ("A", "ABSENT"):
                    status_char = "A"
                else:
                    status_char = "-"   # not recorded / empty
                attendance_log.append((date_label, status_char))

            logger.info("Found student: name=%s reg=%s pct=%s%% dates=%d",
                        row_name, reg_no_clean, percentage, len(attendance_log))
            return {
                "status":         "found",
                "name":           row_name,
                "present":        present,
                "absent":         absent,
                "percentage":     percentage,
                "attendance_log": attendance_log,
            }

        return {"status": "reg_not_found"}

    except gspread.exceptions.SpreadsheetNotFound:
        logger.error("Spreadsheet not found. Check SHEET_ID and sharing permissions.")
        return {"status": "sheet_error", "detail": "Spreadsheet not found."}
    except Exception as exc:
        logger.exception("Sheet lookup failed: %s", exc)
        return {"status": "sheet_error", "detail": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# 6. Telegram conversation handlers
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/start — entry point of the conversation."""
    await update.message.reply_text(
        "👋 *Welcome to the Discrete Mathematics Attendance Bot!*\n\n"
        "I will tell you your attendance percentage.\n\n"
        "📋 Please enter your *Registration Number:*\n"
        "_Example: 25151113001_",
        parse_mode="Markdown",
    )
    return ASK_REG_NO


async def received_reg_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 1 — store Reg No and ask for Roll No."""
    reg_no = update.message.text.strip()

    if not reg_no:
        await update.message.reply_text(
            "⚠️ Registration number cannot be empty.\nPlease enter it again:"
        )
        return ASK_REG_NO

    context.user_data["reg_no"] = reg_no

    await update.message.reply_text(
        f"✅ Got it!  Reg No: `{reg_no}`\n\n"
        "🔢 Now enter your *Roll Number:*\n"
        "_Example: 12345_",
        parse_mode="Markdown",
    )
    return ASK_ROLL_NO


async def received_roll_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 2 — verify Roll No and return attendance."""
    roll_no = update.message.text.strip()
    reg_no  = context.user_data.get("reg_no", "")

    if not roll_no:
        await update.message.reply_text(
            "⚠️ Roll number cannot be empty. Please enter it again:"
        )
        return ASK_ROLL_NO

    await update.message.reply_text("🔍 Fetching your attendance record…")

    result = lookup_student(reg_no, roll_no)
    status = result.get("status")

    if status == "reg_not_found":
        await update.message.reply_text(
            "❌ *Registration number not found.*\n\n"
            "Please check your Reg No and try again with /start.",
            parse_mode="Markdown",
        )

    elif status == "roll_mismatch":
        await update.message.reply_text(
            "❌ *Roll Number does not match our records.*\n\n"
            "Please check your Roll No and try again with /start.",
            parse_mode="Markdown",
        )

    elif status == "sheet_error":
        await update.message.reply_text(
            "⚠️ *Could not reach the attendance sheet right now.*\n"
            "Please try again in a moment.",
            parse_mode="Markdown",
        )

    else:
        name           = result["name"]
        present        = result["present"]
        absent         = result["absent"]
        percentage     = result["percentage"]
        attendance_log = result.get("attendance_log", [])

        # ── Status emoji ───────────────────────────────────────────────────
        try:
            pct_val = float(str(percentage).replace("%", "").strip())
            if pct_val >= 75:
                pct_emoji   = "🟢"
                status_text = "Good Standing ✅"
            elif pct_val >= 60:
                pct_emoji   = "🟡"
                status_text = "At Risk ⚠️  — attend more classes"
            else:
                pct_emoji   = "🔴"
                status_text = "Shortage ❗ — immediate attention required"
        except ValueError:
            pct_emoji   = "📊"
            status_text = ""

        # ── Message 1: Summary card ────────────────────────────────────────
        await update.message.reply_text(
            f"📋 *Attendance Record — Discrete Mathematics*\n"
            f"{'─' * 34}\n"
            f"👤 *Name:*              {name}\n"
            f"🆔 *Reg No:*            `{reg_no}`\n"
            f"🔢 *Roll No:*           `{roll_no}`\n"
            f"✅ *Classes Attended:*  {present}\n"
            f"❌ *Classes Missed:*    {absent}\n"
            f"{pct_emoji} *Attendance:*    *{percentage}%*\n"
            f"📌 *Status:*            {status_text}\n"
            f"{'─' * 34}\n"
            f"_Discrete Mathematics • Academic Year 2025-26_",
            parse_mode="Markdown",
        )

        # ── Message 2: Date-wise log ───────────────────────────────────────
        if attendance_log:
            # Build lines: "DD/MM/YY  ✅ Present" or "❌ Absent" or "➖ "
            lines = ["📅 *Date-wise Attendance Log:*\n"]
            for i, (date_label, status_char) in enumerate(attendance_log, start=1):
                if status_char == "P":
                    mark = "✅ Present"
                elif status_char == "A":
                    mark = "❌ Absent "
                else:
                    mark = "➖ —      "  # not recorded
                lines.append(f"`{i:02d}.` {date_label:<12}  {mark}")

            # Telegram max message = 4096 chars — chunk if necessary
            CHUNK_SIZE = 4000
            chunk = ""
            for line in lines:
                candidate = chunk + line + "\n"
                if len(candidate) > CHUNK_SIZE:
                    await update.message.reply_text(chunk, parse_mode="Markdown")
                    chunk = line + "\n"
                else:
                    chunk = candidate
            if chunk.strip():
                await update.message.reply_text(chunk, parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "_No date-wise records found in the sheet yet._",
                parse_mode="Markdown",
            )

    context.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel — exits the conversation cleanly."""
    context.user_data.clear()
    await update.message.reply_text(
        "🚫 Cancelled. Type /start anytime to check your attendance."
    )
    return ConversationHandler.END


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles messages/commands sent outside of any conversation."""
    await update.message.reply_text(
        "🤖 Type /start to check your attendance.\n"
        "Type /cancel to stop at any time."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Application entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in .env")
        sys.exit(1)

    logger.info("Starting Discrete Mathematics Attendance Bot…")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_REG_NO:  [MessageHandler(filters.TEXT & ~filters.COMMAND, received_reg_no)],
            ASK_ROLL_NO: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_roll_no)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot is live. Press Ctrl+C to stop.")
    # Fix for Python 3.12+ — asyncio no longer auto-creates an event loop
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
