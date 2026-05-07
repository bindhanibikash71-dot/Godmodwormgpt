import os
import sys
import json
import time
import logging
import asyncio
import sqlite3
import datetime
import hashlib
import uuid
import requests
from typing import Optional, List, Dict, Any

# Telegram Bot Framework
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    User
)
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes
)
from telegram.constants import ParseMode, ChatAction

# ==========================================================
# CONFIGURATION & SECRETS
# ==========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "")
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")

ADMIN_IDS = [int(x.strip()) for x in ADMIN_ID_RAW.split(",") if x.strip().isdigit()]

# ✅ FIXED: Each constant on its own line
DEFAULT_MODEL = "openai/gpt-oss-120b:free"
DEFAULT_TEMP = 0.7
DEFAULT_MAX_TOKENS = 1000
DEFAULT_TOP_P = 1.0
FREE_DAILY_LIMIT = 50
PREMIUM_BADGE = "👑 "
DB_NAME = "bot.db"
# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ==========================================================
# DATABASE MANAGER
# ==========================================================

class DatabaseManager:
    def __init__(self, db_name: str):
        self.db_name = db_name
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_banned INTEGER DEFAULT 0,
            messages_today INTEGER DEFAULT 0,
            last_reset_date DATE
        )""")

        cursor.execute("""CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        cursor.execute("""CREATE TABLE IF NOT EXISTS premium_users (
            user_id INTEGER PRIMARY KEY,
            plan_type TEXT,
            start_date TIMESTAMP,
            expiry_date TIMESTAMP,            is_active INTEGER DEFAULT 1
        )""")

        cursor.execute("""CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            currency TEXT,
            status TEXT,
            plan_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        cursor.execute("""CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")

        # Initialize defaults
        cursor.execute("SELECT count(*) FROM config")
        if cursor.fetchone()[0] == 0:
            defaults = [
                ("system_prompt", "You are a helpful AI assistant."),
                ("current_model", DEFAULT_MODEL),
                ("temperature", str(DEFAULT_TEMP)),
                ("max_tokens", str(DEFAULT_MAX_TOKENS)),
                ("top_p", str(DEFAULT_TOP_P)),
                ("welcome_msg", "Welcome! I am your AI Assistant."),
                ("footer_text", "Powered by AI"),
                ("force_join_channel", ""),
                ("maintenance_mode", "0")
            ]
            cursor.executemany("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", defaults)

        conn.commit()
        conn.close()

    def add_user(self, user_id: int, username: str, first_name: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)", 
                          (user_id, username, first_name))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        finally:
            conn.close()

    def get_user(self, user_id: int):        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def update_last_active(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        today = datetime.date.today().isoformat()
        cursor.execute("""UPDATE users SET last_active = CURRENT_TIMESTAMP,
            messages_today = CASE WHEN last_reset_date != ? THEN 1 ELSE messages_today + 1 END,
            last_reset_date = CASE WHEN last_reset_date != ? THEN ? ELSE last_reset_date END
            WHERE user_id = ?""", (today, today, today, user_id))
        conn.commit()
        conn.close()

    def is_premium(self, user_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT expiry_date FROM premium_users WHERE user_id = ? AND is_active = 1", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return False
        expiry = datetime.datetime.fromisoformat(row[0])
        return expiry > datetime.datetime.now()

    def add_premium(self, user_id: int, plan_type: str, days: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        start = datetime.datetime.now()
        expiry = start + datetime.timedelta(days=days) if plan_type != 'lifetime' else datetime.datetime(2099, 12, 31)
        
        cursor.execute("""INSERT INTO premium_users (user_id, plan_type, start_date, expiry_date, is_active)
            VALUES (?, ?, ?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET
            plan_type = excluded.plan_type, start_date = excluded.start_date,
            expiry_date = excluded.expiry_date, is_active = 1""",
            (user_id, plan_type, start.isoformat(), expiry.isoformat()))
        conn.commit()
        conn.close()

    def add_history(self, user_id: int, role: str, content: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
        conn.commit()
        conn.close()
    def get_history(self, user_id: int, limit: int = 10):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
        rows = cursor.fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    def clear_history(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

    def get_config(self, key: str) -> str:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else ""

    def set_config(self, key: str, value: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()

    def add_payment(self, payment_id: str, user_id: int, amount: float, currency: str, plan_type: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO payments (payment_id, user_id, amount, currency, status, plan_type) VALUES (?, ?, ?, ?, 'PENDING', ?)",
                      (payment_id, user_id, amount, currency, plan_type))
        conn.commit()
        conn.close()

    def update_payment_status(self, payment_id: str, status: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE payments SET status = ? WHERE payment_id = ?", (status, payment_id))
        conn.commit()
        conn.close()

    def get_pending_payment(self, payment_id: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM payments WHERE payment_id = ? AND status = 'PENDING'", (payment_id,))
        row = cursor.fetchone()        conn.close()
        return row

    def get_stats(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        stats = {}
        cursor.execute("SELECT COUNT(*) FROM users")
        stats['total_users'] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM premium_users WHERE is_active = 1")
        stats['premium_users'] = cursor.fetchone()[0]
        conn.close()
        return stats

    def get_all_users(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        users = [row[0] for row in cursor.fetchall()]
        conn.close()
        return users

    def ban_user(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

db = DatabaseManager(DB_NAME)

# ==========================================================
# OPENROUTER CLIENT
# ==========================================================

class AIClient:
    def __init__(self):
        self.api_key = OPENROUTER_API_KEY
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/user/bot",
            "X-Title": "Telegram AI Bot"
        }

    def generate_response(self, messages: list, model: str, temp: float, max_tokens: int, top_p: float) -> str:
        payload = {
            "model": model,
            "messages": messages,            "temperature": temp,
            "max_tokens": max_tokens,
            "top_p": top_p
        }
        try:
            response = requests.post(self.base_url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            if 'choices' in data and len(data['choices']) > 0:
                return data['choices'][0]['message']['content']
            return "Sorry, I encountered an error."
        except Exception as e:
            logger.error(f"OpenRouter Error: {e}")
            return "AI service unavailable."

ai_client = AIClient()

# ==========================================================
# CASHFREE CLIENT
# ==========================================================

class PaymentClient:
    def __init__(self):
        self.app_id = CASHFREE_APP_ID
        self.secret_key = CASHFREE_SECRET_KEY
        self.base_url = "https://sandbox.cashfree.com/pg"

    def create_order(self, order_id: str, amount: float, customer_name: str) -> dict:
        url = f"{self.base_url}/orders"
        headers = {
            "Content-Type": "application/json",
            "x-api-version": "2023-08-01",
            "x-client-id": self.app_id,
            "x-client-secret": self.secret_key
        }
        payload = {
            "order_id": order_id,
            "order_amount": amount,
            "order_currency": "INR",
            "customer_details": {
                "customer_id": str(uuid.uuid4()),
                "customer_name": customer_name,
                "customer_email": "user@example.com",
                "customer_phone": "9999999999"
            }
        }
        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()        except Exception as e:
            logger.error(f"Cashfree Error: {e}")
            return None

payment_client = PaymentClient()

# ==========================================================
# HELPERS
# ==========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_banned(user_id: int) -> bool:
    user = db.get_user(user_id)
    return user and user['is_banned'] == 1

def get_plan_details(plan: str):
    plans = {
        "1month": {"price": 199, "days": 30, "name": "1 Month God Mode"},
        "3month": {"price": 499, "days": 90, "name": "3 Months God Mode"},
        "lifetime": {"price": 1499, "days": 36500, "name": "Lifetime God Mode"}
    }
    return plans.get(plan, plans["1month"])

# ==========================================================
# COMMAND HANDLERS
# ==========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username, user.first_name)
    
    if is_banned(user.id):
        await update.message.reply_text("You are banned.")
        return

    welcome = db.get_config("welcome_msg")
    keyboard = [
        [InlineKeyboardButton("🤖 AI Chat", callback_data="menu_ai")],
        [InlineKeyboardButton("👑 Buy Premium", callback_data="menu_premium")],
        [InlineKeyboardButton("👤 Profile", callback_data="menu_profile")]
    ]
    await update.message.reply_text(welcome, reply_markup=InlineKeyboardMarkup(keyboard))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /start to begin. Admins: /admin")

async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.clear_history(update.effective_user.id)    await update.message.reply_text("🔄 Chat reset!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message or not update.message.text or update.message.text.startswith('/'):
        return

    if is_banned(user.id):
        return

    user_data = db.get_user(user.id)
    if not db.is_premium(user.id) and user_data['messages_today'] >= FREE_DAILY_LIMIT:
        await update.message.reply_text("❌ Daily limit reached. Upgrade to Premium!")
        return

    await context.bot.send_chat_action(chat_id=user.id, action=ChatAction.TYPING)

    system_prompt = db.get_config("system_prompt")
    model = db.get_config("current_model")
    temp = float(db.get_config("temperature"))
    max_tokens = int(db.get_config("max_tokens"))
    top_p = float(db.get_config("top_p"))

    history = db.get_history(user.id, limit=10)
    messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": update.message.text}]

    db.add_history(user.id, "user", update.message.text)
    db.update_last_active(user.id)

    response = ai_client.generate_response(messages, model, temp, max_tokens, top_p)
    db.add_history(user.id, "assistant", response)

    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

# ==========================================================
# CALLBACK HANDLER
# ==========================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "menu_ai":
        await query.edit_message_text("🤖 Send me a message to chat!")
    elif data == "menu_profile":
        user = db.get_user(user_id)
        is_prem = db.is_premium(user_id)
        text = f"👤 *Profile*\nID: `{user_id}`\nName: {user['first_name']}\nPlan: {'Premium' if is_prem else 'Free'}"        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    elif data == "menu_premium":
        kb = [
            [InlineKeyboardButton("1 Month - ₹199", callback_data="buy_1month")],
            [InlineKeyboardButton("3 Months - ₹499", callback_data="buy_3month")],
            [InlineKeyboardButton("Lifetime - ₹1499", callback_data="buy_lifetime")]
        ]
        await query.edit_message_text("👑 Choose a plan:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("buy_"):
        plan_key = data.split("_")[1]
        plan = get_plan_details(plan_key)
        order_id = f"ORD_{user_id}_{int(time.time())}"
        db.add_payment(order_id, user_id, plan['price'], "INR", plan_key)
        
        cf_resp = payment_client.create_order(order_id, plan['price'], query.from_user.first_name)
        if cf_resp and 'redirect_url' in cf_resp:
            kb = [[InlineKeyboardButton("Pay Now 💳", url=cf_resp['redirect_url'])]]
            await query.edit_message_text(f"💰 Order: {plan['name']}\nAmount: ₹{plan['price']}", reply_markup=InlineKeyboardMarkup(kb))
        else:
            await query.edit_message_text("Payment error. Contact admin.")

# ==========================================================
# ADMIN COMMANDS
# ==========================================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    stats = db.get_stats()
    text = f"📊 *Admin Panel*\nUsers: {stats['total_users']}\nPremium: {stats['premium_users']}"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    db.set_config("current_model", context.args[0])
    await update.message.reply_text(f"✅ Model set to: {context.args[0]}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    msg = " ".join(context.args)
    users = db.get_all_users()
    sent = 0
    for uid in users:
        try:
            await context.bot.send_message(uid, msg)
            sent += 1
        except:
            pass    await update.message.reply_text(f"✅ Broadcasted to {sent} users.")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    db.ban_user(int(context.args[0]))
    await update.message.reply_text("🚫 User banned.")

# ==========================================================
# MAIN
# ==========================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN missing!")
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("newchat", new_chat))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("setmodel", set_model))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("✅ Bot starting...")
    print("✅ Bot started successfully!")
    app.run_polling()

if __name__ == "__main__":
    main()
