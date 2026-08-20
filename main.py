"""main.py — Discrete Mathematics Attendance Telegram Bot

======================================================
Secure dual Google Sheets architecture:
- SHEET_ID: Public attendance sheet (date labels row 105)
- USERS_SHEET_ID: Private sheet for storing user Chat IDs for broadcasting
- BEU Attendance Marks (out of 5) based on BEU/Exam/428/2026/Patna
- Admin Live Alerts on every attendance check
- Direct Two-Way Messaging: /contact (Student -> Admin) & /send (Admin -> Student)
"""

import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import sys
import threading

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
import gspread
import httpx
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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
SHEET_ID = os.environ.get(
    "SHEET_ID", "1f2XM7HFk0IYSiOyKYEKkNMd3j-lB6NgLS0qlh5M3o0M"
).strip()
USERS_SHEET_ID = os.environ.get(
    "USERS_SHEET_ID", "1lr27rxF3KZqdeXg8cuaLYeL0nTUjREIiecXA8cNXdMA"
).strip()
GOOGLE_CREDS_PATH = os.environ.get("GOOGLE_CREDS_PATH", "credentials.json").strip()
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()

# ─────────────────────────────────────────────────────────────────────────────
# 2. Sheet layout constants
# ─────────────────────────────────────────────────────────────────────────────
HEADER_ROW = 7
DATA_START_ROW = 8

COL_ROLL = 2  # B  → Roll No
COL_REG = 3  # C  → Reg No
COL_NAME = 4  # D  → Name
COL_PRESENT = 5  # E  → Present
COL_ABSENT = 6  # F  → Absent
COL_PERCENTAGE = 7  # G  → Percentage
COL_DATE_START = 8  # H  → First date column
DATE_LABEL_ROW = 105  # Row where date labels are located

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

ASK_REG_NO, ASK_ROLL_NO = range(2)


# ─────────────────────────────────────────────────────────────────────────────
# 4. BEU Attendance Marks Helper
# ─────────────────────────────────────────────────────────────────────────────
def calculate_beu_attendance_marks(percentage_val: float) -> int:
  """Calculates BEU attendance marks out of 5 according to Notice Memo No.

  BEU/Exam/428/2026/Patna:
  - 96% - 100% : 5 Marks
  - 91% - 95%  : 4 Marks
  - 86% - 90%  : 3 Marks
  - 81% - 85%  : 2 Marks
  - 75% - 80%  : 1 Mark
  - Below 75%  : 0 Marks
  """
  pct = round(percentage_val)
  if pct >= 96:
    return 5
  elif pct >= 91:
    return 4
  elif pct >= 86:
    return 3
  elif pct >= 81:
    return 2
  elif pct >= 75:
    return 1
  return 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. Render Health Check HTTP Server
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
# 6. Google Sheets & User Logging Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _build_creds() -> Credentials:
  if GOOGLE_CREDS_JSON:
    info = json.loads(GOOGLE_CREDS_JSON)
    return Credentials.from_service_account_info(info, scopes=SCOPES)
  if GOOGLE_CREDS_PATH and os.path.exists(GOOGLE_CREDS_PATH):
    return Credentials.from_service_account_file(
        GOOGLE_CREDS_PATH, scopes=SCOPES
    )
  raise RuntimeError("No Google credentials found.")


def _open_worksheet() -> gspread.Worksheet:
  client = gspread.authorize(_build_creds())
  spreadsheet = client.open_by_key(SHEET_ID)
  worksheet = spreadsheet.sheet1
  logger.info("Opened worksheet: %s", worksheet.title)
  return worksheet


def save_user_record(
    chat_id: int, username: str, reg_no: str, name: str
) -> None:
  """Saves student Chat ID into the private USERS_SHEET_ID spreadsheet."""
  try:
    client = gspread.authorize(_build_creds())
    spreadsheet = client.open_by_key(USERS_SHEET_ID)
    ws_users = spreadsheet.sheet1
    all_rows = ws_users.get_all_values()

    if not all_rows:
      ws_users.append_row(
          ["Chat ID", "Username", "Reg No", "Name", "Last Active"]
      )
      all_rows = ws_users.get_all_values()

    chat_id_str = str(chat_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for row in all_rows[1:]:
      if row and row[0].strip() == chat_id_str:
        return

    ws_users.append_row([
        chat_id_str,
        f"@{username}" if username else "",
        reg_no,
        name,
        now_str,
    ])
    logger.info("Saved user record to private Users sheet: %s", chat_id_str)
  except Exception as exc:
    logger.error("Failed to save user record: %s", exc)


def get_all_user_chat_ids() -> list[int]:
  """Retrieves all unique user Chat IDs from the private USERS_SHEET_ID spreadsheet."""
  try:
    client = gspread.authorize(_build_creds())
    spreadsheet = client.open_by_key(USERS_SHEET_ID)
    ws_users = spreadsheet.sheet1
    all_rows = ws_users.get_all_values()
    logger.info("Fetched %d rows from Users sheet.", len(all_rows))
    chat_ids = []

    for row in all_rows:
      if not row:
        continue
      val = row[0].strip().lstrip("'")
      try:
        cid = int(val)
        chat_ids.append(cid)
      except ValueError:
        continue

    logger.info("Found %d valid Chat ID(s) for broadcast.", len(chat_ids))
    return list(set(chat_ids))
  except Exception as exc:
    logger.error("Failed to fetch user chat IDs: %s", exc)
    return []


def lookup_student(reg_no: str, roll_no: str) -> dict:
  try:
    ws = _open_worksheet()
    all_values = ws.get_all_values()

    reg_no_clean = reg_no.strip().upper()
    roll_no_clean = roll_no.strip()

    # Retrieve date header row (checks row 105 first, falls back to row 7)
    date_label_row = []
    if len(all_values) >= DATE_LABEL_ROW and any(
        x.strip() for x in all_values[DATE_LABEL_ROW - 1][COL_DATE_START - 1 :]
    ):
      date_label_row = all_values[DATE_LABEL_ROW - 1]
    elif len(all_values) >= HEADER_ROW:
      date_label_row = all_values[HEADER_ROW - 1]

    # Find the last valid date column index
    last_date_col = COL_DATE_START - 1
    if date_label_row:
      for c in range(COL_DATE_START, len(date_label_row) + 1):
        if date_label_row[c - 1].strip():
          last_date_col = c

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
        return {"status": "roll_mismatch"}

      row_name = row[COL_NAME - 1].strip()
      present = row[COL_PRESENT - 1].strip() or "0"
      absent = row[COL_ABSENT - 1].strip() or "0"
      percentage = row[COL_PERCENTAGE - 1].strip() or "N/A"

      effective_last_col = last_date_col
      if effective_last_col < COL_DATE_START:
        for c in range(len(row), COL_DATE_START - 1, -1):
          if row[c - 1].strip():
            effective_last_col = c
            break

      attendance_log = []
      if effective_last_col >= COL_DATE_START:
        for col_idx in range(COL_DATE_START, effective_last_col + 1):
          if col_idx <= len(date_label_row):
            date_label = date_label_row[col_idx - 1].strip()
          else:
            date_label = ""

          if not date_label:
            date_label = _col_letter(col_idx)

          cell_val = row[col_idx - 1].strip() if col_idx <= len(row) else ""
          upper_val = cell_val.upper()

          if upper_val in ("P", "PRESENT", "1"):
            status_char = "P"
          elif upper_val in ("A", "ABSENT", "0"):
            status_char = "A"
          else:
            status_char = "-"

          attendance_log.append((date_label, status_char))

      return {
          "status": "found",
          "name": row_name,
          "present": present,
          "absent": absent,
          "percentage": percentage,
          "attendance_log": attendance_log,
      }

    return {"status": "reg_not_found"}

  except Exception as exc:
    logger.exception("Sheet lookup failed: %s", exc)
    return {"status": "sheet_error", "detail": str(exc)}


def _col_letter(col: int) -> str:
  result = ""
  while col > 0:
    col, rem = divmod(col - 1, 26)
    result = chr(65 + rem) + result
  return result


# ─────────────────────────────────────────────────────────────────────────────
# 7. Telegram Conversation & Custom Handlers
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
  await update.message.reply_text(
      "👋 *Welcome to the Discrete Mathematics Attendance Bot!*\n\n"
      "I will tell you your attendance percentage and BEU attendance marks.\n\n"
      "📋 Please enter your *Registration Number:*\n"
      "_Example: 25151113001_",
      parse_mode="Markdown",
  )
  return ASK_REG_NO


async def received_reg_no(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
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
        "❌ *Registration number not found.*\nPlease check your Reg No and"
        " try again with /start.",
        parse_mode="Markdown",
    )
  elif status == "roll_mismatch":
    await update.message.reply_text(
        "❌ *Roll Number does not match our records.*\nPlease check your Roll"
        " No and try again with /start.",
        parse_mode="Markdown",
    )
  elif status == "sheet_error":
    await update.message.reply_text(
        "⚠️ *Could not reach the attendance sheet right now.*\nPlease try again"
        " in a moment.",
        parse_mode="Markdown",
    )
  else:
    name = result["name"]
    present = result["present"]
    absent = result["absent"]
    percentage = result["percentage"]
    attendance_log = result.get("attendance_log", [])

    # Save user record for broadcasting
    chat_id = update.effective_chat.id
    username = update.effective_user.username or ""
    save_user_record(chat_id, username, reg_no, name)

    try:
      pct_val = float(str(percentage).replace("%", "").strip())
      beu_marks = calculate_beu_attendance_marks(pct_val)
      marks_display = f"{beu_marks} / 5"

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
      marks_display = "N/A"

    # Send report to the student
    await update.message.reply_text(
        f"📋 *Attendance Record — Discrete Mathematics*\n"
        f"{'─' * 34}\n"
        f"👤 *Name:*               {name}\n"
        f"🆔 *Reg No:*             `{reg_no}`\n"
        f"🔢 *Roll No:*            `{roll_no}`\n"
        f"✅ *Classes Attended:*  {present}\n"
        f"❌ *Classes Missed:*    {absent}\n"
        f"{pct_emoji} *Attendance:*    *{percentage}%*\n"
        f"🎯 *BEU Marks:*         *{marks_display}*\n"
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

    # ─────────────────────────────────────────────────────────────────────────
    # Live Admin Notification on Attendance Check
    # ─────────────────────────────────────────────────────────────────────────
    if ADMIN_ID:
      try:
        user_info = update.effective_user
        user_handle = f"@{user_info.username}" if user_info.username else "N/A"
        current_time = datetime.now().strftime("%d %b %Y, %I:%M %p")

        admin_alert = (
            f"🔔 *Attendance Checked Alert*\n"
            f"{'─' * 30}\n"
            f"👤 *Student:* {name}\n"
            f"🆔 *Reg No:* `{reg_no}` | *Roll:* `{roll_no}`\n"
            f"📊 *Attendance:* *{percentage}%* ({marks_display} Marks)\n"
            f"✅ *Attended:* {present} | ❌ *Missed:* {absent}\n"
            f"📱 *Telegram:* {user_info.full_name} ({user_handle})\n"
            f"💬 *Chat ID:* `{chat_id}`\n"
            f"⏰ *Time:* `{current_time}`"
        )
        await context.bot.send_message(
            chat_id=int(ADMIN_ID),
            text=admin_alert,
            parse_mode="Markdown",
        )
      except Exception as alert_exc:
        logger.warning("Could not send admin notification: %s", alert_exc)

  context.user_data.clear()
  return ConversationHandler.END


async def cmd_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
  context.user_data.clear()
  await update.message.reply_text(
      "🚫 Cancelled. Type /start anytime to check your attendance."
  )
  return ConversationHandler.END


async def cmd_contact(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
  """Student Command: /contact <message> -> Forwards message to the Admin."""
  if not context.args:
    await update.message.reply_text(
        "⚠️ Usage: `/contact <your message or question>`",
        parse_mode="Markdown",
    )
    return

  user = update.effective_user
  chat_id = update.effective_chat.id
  user_text = " ".join(context.args)
  reg_no = context.user_data.get("reg_no", "Not Specified")

  admin_notification = (
      f"📩 *New Message from Student:*\n"
      f"👤 *Name:* {user.full_name} (@{user.username or 'N/A'})\n"
      f"🆔 *Chat ID:* `{chat_id}`\n"
      f"📋 *Reg No:* `{reg_no}`\n\n"
      f"💬 *Message:*\n{user_text}\n\n"
      f"👉 _To reply, use:_ `/send {chat_id} <your reply>`"
  )

  if ADMIN_ID:
    try:
      await context.bot.send_message(
          chat_id=int(ADMIN_ID), text=admin_notification, parse_mode="Markdown"
      )
      await update.message.reply_text(
          "✅ Your message has been forwarded to the instructor/admin."
      )
    except Exception as exc:
      logger.error("Failed to forward to admin: %s", exc)
      await update.message.reply_text(
          "⚠️ Could not send your message right now. Please try again later."
      )
  else:
    await update.message.reply_text("⚠️ Admin is not configured on this bot.")


async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
  """Admin Command: /send <chat_id> <message> -> Send direct message to a student."""
  user_id = update.effective_user.id
  if not ADMIN_ID or str(user_id) != ADMIN_ID:
    await update.message.reply_text(
        "⛔ You are not authorized to use this command."
    )
    return

  if len(context.args) < 2:
    await update.message.reply_text(
        "⚠️ Usage: `/send <chat_id> <your reply/message>`",
        parse_mode="Markdown",
    )
    return

  target_chat_id = context.args[0].strip()
  message_to_send = " ".join(context.args[1:])

  try:
    await context.bot.send_message(
        chat_id=int(target_chat_id),
        text=f"💬 *Message from Admin:*\n\n{message_to_send}",
        parse_mode="Markdown",
    )
    await update.message.reply_text(
        f"✅ Message sent successfully to `{target_chat_id}`.",
        parse_mode="Markdown",
    )
  except Exception as exc:
    logger.error("Failed to send message to %s: %s", target_chat_id, exc)
    await update.message.reply_text(
        f"❌ Failed to send message: `{exc}`", parse_mode="Markdown"
    )


async def cmd_broadcast(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
  """Admin Command: /broadcast <message>"""
  user_id = update.effective_user.id
  if not ADMIN_ID or str(user_id) != ADMIN_ID:
    await update.message.reply_text(
        "⛔ You are not authorized to use this command."
    )
    return

  if not context.args:
    await update.message.reply_text(
        "⚠️ Usage: `/broadcast <your message text>`", parse_mode="Markdown"
    )
    return

  message_text = " ".join(context.args)
  chat_ids = get_all_user_chat_ids()

  if not chat_ids:
    await update.message.reply_text(
        "⚠️ No user records found in the database."
    )
    return

  await update.message.reply_text(
      f"📢 Starting broadcast to {len(chat_ids)} user(s)…"
  )
  success_count = 0
  fail_count = 0

  for cid in chat_ids:
    try:
      await context.bot.send_message(
          chat_id=cid, text=message_text, parse_mode="Markdown"
      )
      success_count += 1
      await asyncio.sleep(0.05)
    except Exception as exc:
      logger.warning("Failed to send broadcast to %s: %s", cid, exc)
      fail_count += 1

  await update.message.reply_text(
      f"✅ *Broadcast Complete!*\n\n"
      f"📤 Sent: `{success_count}`\n"
      f"❌ Failed / Blocked: `{fail_count}`",
      parse_mode="Markdown",
  )


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
  await update.message.reply_text(
      "🤖 *Commands Available:*\n"
      "• /start — Check your attendance\n"
      "• /contact <message> — Send a question/message to Admin\n"
      "• /cancel — Cancel current action",
      parse_mode="Markdown",
  )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Application Entry Point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
  print("=" * 50, flush=True)
  print("🚀 Starting Discrete Mathematics Attendance Bot...", flush=True)
  print("=" * 50, flush=True)

  if not BOT_TOKEN:
    logger.critical(
        "BOT_TOKEN is missing! Set BOT_TOKEN in Render Environment Variables."
    )
    sys.stdout.flush()
    sys.exit(1)

  try:
    _ = _build_creds()
    logger.info("Google credentials verified.")
  except Exception as e:
    logger.critical("Google credentials verification failed: %s", e)
    sys.stdout.flush()
    sys.exit(1)

  # Start HTTP server for Render health checks
  start_health_check_server()

  # Clean up any leftover webhooks
  try:
    with httpx.Client(timeout=15) as client:
      r = client.post(
          f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
          json={"drop_pending_updates": True},
      )
      logger.info("deleteWebhook response: %s", r.json())
  except Exception as exc:
    logger.warning("Could not delete webhook: %s", exc)

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
  app.add_handler(CommandHandler("contact", cmd_contact))
  app.add_handler(CommandHandler("send", cmd_send))
  app.add_handler(CommandHandler("broadcast", cmd_broadcast))
  app.add_handler(MessageHandler(filters.COMMAND, unknown))

  logger.info("Bot is live and listening for messages...")
  sys.stdout.flush()

  # Ensure an active event loop exists for Python 3.12+ / 3.14 compatibility
  try:
    asyncio.get_event_loop()
  except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

  app.run_polling(
      allowed_updates=Update.ALL_TYPES,
      drop_pending_updates=True,
  )


if __name__ == "__main__":
  main()
