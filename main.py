"""
main.py — Discrete Mathematics Attendance Telegram Bot
======================================================
Students can check their own attendance by entering their
Registration Number and Roll Number via a Telegram bot.

Sheet structure (Discrete Mathematics):
  Rows 1–6  : Empty / title area
  Row  7    : Headers → A=S.NO, B=Roll No, C=Reg No, D=Name,
                        E=Present, F=Absent, G=Percentage,
                        H=DOB (or date start)
  Row  8+   : Student data

Conversation flow:
  /start  → Ask Reg No → Ask Roll No → Show result
  /cancel → Exit at any point

Run locally  : python main.py
Deploy Render: Web Service → python main.py
"""

import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import sys
import threading

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
import gspread
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────────────────────────────────────
# 1. Environment variables
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SHEET_ID = os.environ.get(
    "SHEET_ID", "1f2XM7HFk0IYSiOyKYEKkNMd3j-lB6NgLS0qlh5M3o0M"
)
GOOGLE_CREDS_PATH = os.environ.get("GOOGLE_CREDS_PATH", "credentials.json")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

# ─────────────────────────────────────────────────────────────────────────────
# 2. Sheet layout constants  (must match your actual Google Sheet)
# ─────────────────────────────────────────────────────────────────────────────
HEADER_ROW = 7  # Row number that contains column labels
DATA_START_ROW = 8  # First row with actual student data

# 1-based column positions for fixed columns
COL_ROLL = 2  # B  → Roll No
COL_REG = 3  # C  → Reg No
COL_NAME = 4  # D  → Name
COL_PRESENT = 5  # E  → Present  (formula/count — read-only)
COL_ABSENT = 6  # F  → Absent   (formula/count — read-only)
COL_PERCENTAGE = 7  # G  → Percentage (formula — read-only)
COL_DATE_START = 8  # H  → First date column (P/A values start here)
DATE_LABEL_ROW = 105  # Row where date labels are located (H105, I105, J105…)

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
# 5. Render Health Check HTTP Server (binds to PORT to pass port scanning)
# ─────────────────────────────────────────────────────────────────────────────
def start_health_check_server() -> None:
  """Starts a lightweight HTTP server on $PORT to satisfy Render Web Service health checks."""
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
      pass  # Suppress HTTP request logs

  try:
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health check HTTP server running on port %d", port)
  except Exception as exc:
    logger.warning("Could not start health check HTTP server: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Google Sheets helpers
# ─────────────────────────────────────────────────────────────────────────────


def _build_creds() -> Credentials:
  """Build Google credentials from either an env-var JSON string (Render)

  or a local JSON file path (local development).
  """
  if GOOGLE_CREDS_JSON.strip():
    info = json.loads(GOOGLE_CREDS_JSON.strip())
    return Credentials.from_service_account_info(info, scopes=SCOPES)
  if GOOGLE_CREDS_PATH.strip() and os.path.exists(GOOGLE_CREDS_PATH.strip()):
    return Credentials.from_service_account_file(
        GOOGLE_CREDS_PATH.strip(), scopes=SCOPES
    )
  raise RuntimeError(
      "No Google credentials found. Set GOOGLE_CREDENTIALS_JSON or"
      " GOOGLE_CREDS_PATH."
  )


def _open_worksheet() -> gspread.Worksheet:
  """Opens the Discrete Mathematics spreadsheet, first worksheet."""
  client = gspread.authorize(_build_creds())
  spreadsheet = client.open_by_key(SHEET_ID.strip())
  worksheet = spreadsheet.sheet1  # gid=0, first tab
  logger.info("Opened worksheet '%s'.", worksheet.title)
  return worksheet


def lookup_student(reg_no: str, roll_no: str) -> dict:
  """Finds a student row by Reg No + Roll No, then builds the full date-wise log.

  Returns:
      {"status": "found",  "name": …, "present": …, "absent": …,
       "percentage": …, "attendance_log": [(date_label, "P"|"A"|"-"), …]}
      {"status": "reg_not_found"}
      {"status": "roll_mismatch"}
      {"status": "sheet_error", "detail": …}
  """
  try:
    ws = _open_worksheet()
    all_values = ws.get_all_values()

    reg_no_clean = reg_no.strip().upper()
    roll_no_clean = roll_no.strip()

    # Read date labels from row 105
    if len(all_values) >= DATE_LABEL_ROW:
      date_label_row = all_values[DATE_LABEL_ROW - 1]
    else:
      date_label_row = []
    logger.info("Date labels row has %d cells.", len(date_label_row))

    # Find student row
    data_rows = all_values[DATA_START_ROW - 1 :]

    for row in data_rows:
      needed = max(
          COL_REG,
          COL_ROLL,
          COL_NAME,
          COL_PRESENT,
          COL_ABSENT,
          COL_PERCENTAGE,
      )
      while len(row) < needed:
        row.append("")

      row_reg = row[COL_REG - 1].strip().upper()
      if row_reg != reg_no_clean:
        continue

      row_roll = row[COL_ROLL - 1].strip()
      if row_roll != roll_no_clean:
        logger.info(
            "Roll mismatch reg=%s sheet=%s input=%s",
            reg_no_clean,
            row_roll,
            roll_no_clean,
        )
        return {"status": "roll_mismatch"}

      row_name = row[COL_NAME - 1].strip()
      present = row[COL_PRESENT - 1].strip() or "0"
      absent = row[COL_ABSENT - 1].strip() or "0"
      percentage = row[COL_PERCENTAGE - 1].strip() or "N/A"

      attendance_log = []
      col_idx = COL_DATE_START

      while True:
        if col_idx > len(row):
          break

        cell_val = row[col_idx - 1].strip()
        if not cell_val:
          break

        if col_idx <= len(date_label_row):
          date_label = date_label_row[col_idx - 1].strip()
        else:
          date_label = ""

        if not date_label:
          date_label = _col_letter(col_idx)

        upper_val = cell_val.upper()
        if upper_val in ("P", "PRESENT"):
          status_char = "P"
        elif upper_val in ("A", "ABSENT"):
          status_char = "A"
        else:
          status_char = "-"

        attendance_log.append((date_label, status_char))
        col_idx += 1

      logger.info(
          "Student found: name=%s reg=%s pct=%s dates=%d",
          row_name,
          reg_no_clean,
          percentage,
          len(attendance_log),
      )
      return {
          "status": "found",
          "name": row_name,
          "present": present,
          "absent": absent,
          "percentage": percentage,
          "attendance_log": attendance_log,
      }

    return {"status": "reg_not_found"}

  except gspread.exceptions.SpreadsheetNotFound:
    logger.error("Spreadsheet not found. Check SHEET_ID / sharing.")
    return {"status": "sheet_error", "detail": "Spreadsheet not found."}
  except Exception as exc:
    logger.exception("Sheet lookup failed: %s", exc)
    return {"status": "sheet_error", "detail": str(exc)}


def _col_letter(col: int) -> str:
  """Convert 1-based column number to spreadsheet letter(s). 1→A, 8→H, 27→AA"""
  result = ""
  while col > 0:
    col, rem = divmod(col - 1, 26)
    result = chr(65 + rem) + result
  return result


# ─────────────────────────────────────────────────────────────────────────────
# 7. Telegram conversation handlers
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


async def received_reg_no(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
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


async def received_roll_no(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
  """Step 2 — verify Roll No and return attendance."""
  roll_no = update.message.text.strip()
  reg_no = context.user_data.get("reg_no", "")

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
    name = result["name"]
    present = result["present"]
    absent = result["absent"]
    percentage = result["percentage"]
    attendance_log = result.get("attendance_log", [])

    try:
      pct_val = float(str(percentage).replace("%", "").strip())
      if pct_val >= 75:
        pct_emoji = "🟢"
        status_text = "Good Standing ✅"
      elif pct_val >= 60:
        pct_emoji = "🟡"
        status_text = "At Risk ⚠️  — attend more classes"
      else:
        pct_emoji = "🔴"
        status_text = "Shortage ❗ — immediate attention required"
    except ValueError:
      pct_emoji = "📊"
      status_text = ""

    await update.message.reply_text(
        f"📋 *Attendance Record — Discrete Mathematics*\n"
        f"{'─' * 34}\n"
        f"👤 *Name:*               {name}\n"
        f"🆔 *Reg No:*             `{reg_no}`\n"
        f"🔢 *Roll No:*            `{roll_no}`\n"
        f"✅ *Classes Attended:*  {present}\n"
        f"❌ *Classes Missed:*    {absent}\n"
        f"{pct_emoji} *Attendance:*    *{percentage}%*\n"
        f"📌 *Status:*             {status_text}\n"
        f"{'─' * 34}\n"
        f"_Discrete Mathematics • Academic Year 2025-26_",
        parse_mode="Markdown",
    )

    if attendance_log:
      lines = ["📅 *Date-wise Attendance Log:*\n"]
      for i, (date_label, status_char) in enumerate(attendance_log, start=1):
        if status_char == "P":
          mark = "✅ Present"
        elif status_char == "A":
          mark = "❌ Absent "
        else:
          mark = "➖ —      "
        lines.append(f"`{i:02d}.` {date_label:<12}  {mark}")

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


async def cmd_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
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
# 8. Application entry point
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
  if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set in .env")
    sys.exit(1)

  logger.info("Starting Discrete Mathematics Attendance Bot…")

  # Start HTTP server in daemon thread to satisfy Render port health checks
  start_health_check_server()

  app = Application.builder().token(BOT_TOKEN).build()

  conv = ConversationHandler(
      entry_points=[CommandHandler("start", cmd_start)],
      states={
          ASK_REG_NO: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, received_reg_no)
          ],
          ASK_ROLL_NO: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, received_roll_no)
          ],
      },
      fallbacks=[CommandHandler("cancel", cmd_cancel)],
      allow_reentry=True,
  )

  app.add_handler(conv)
  app.add_handler(MessageHandler(filters.COMMAND, unknown))

  logger.info("Bot is live. Press Ctrl+C to stop.")

  # Clear webhooks before polling
  try:
    import httpx

    with httpx.Client(timeout=15) as client:
      r = client.post(
          f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
          json={"drop_pending_updates": True},
      )
      logger.info("deleteWebhook → %s", r.json())
  except Exception as exc:
    logger.warning("Could not delete webhook: %s", exc)

  asyncio.set_event_loop(asyncio.new_event_loop())
  app.run_polling(
      allowed_updates=Update.ALL_TYPES,
      drop_pending_updates=True,
  )


if __name__ == "__main__":
  main()
