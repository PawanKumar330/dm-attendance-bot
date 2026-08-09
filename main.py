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
DATE_LABEL_ROW = 104  # Row where date labels are located (H104, I104, J104…)

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

    # Read date labels from row 104
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

        cell_val = row[col_idx - 1].
