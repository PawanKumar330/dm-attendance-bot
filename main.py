"""main.py — Discrete Mathematics Attendance Telegram Bot

======================================================
Secure dual Google Sheets architecture:
- SHEET_ID: Public attendance sheet (date labels row 105)
- USERS_SHEET_ID: Private sheet for storing user Chat IDs for broadcasting
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
# 4. Render Health Check HTTP Server
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
# 5. Google Sheets & User Logging Helpers
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
  logger.info("Opened worksheet '%s'.", worksheet.title)
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
    logger.info("
