import os
import sys
import json
import time
import logging
import asyncio
import sqlite3
import datetime
import hashlib
import hmac
import uuid
import requests
from typing import Optional, List, Dict, Any

# Telegram Bot Framework
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    WebAppInfo,
    User
)
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes,
    ConversationHandler
)
from telegram.constants import ParseMode, ChatAction

# ==========================================================
# CONFIGURATION & SECRETS
# ==========================================================

# Load secrets from Environment Variables (Render Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "")
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")

# Convert ADMIN_ID to list of integers
ADMIN_IDS = [int(x.strip()) for x in ADMIN_ID_RAW.split(",") if x.strip().isdigit()]

# Constants — FIXED: Each on separate line
DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_TEMP = 0.7
DEFAULT_MAX_TOKENS = 1000DEFAULT_TOP_P = 1.0
FREE_DAILY_LIMIT = 50
PREMIUM_BADGE = "👑 "

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ==========================================================
# DATABASE MANAGER (SQLite)
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
        
        # Users Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_banned INTEGER DEFAULT 0,
                is_muted INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                messages_today INTEGER DEFAULT 0,
                last_reset_date DATE
            )
        """)

        # Chat History Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)

        # Premium Users Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS premium_users (
                user_id INTEGER PRIMARY KEY,
                plan_type TEXT,
                start_date TIMESTAMP,
                expiry_date TIMESTAMP,
                is_active INTEGER DEFAULT 1,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)

        # Payments Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER,
                amount REAL,
                currency TEXT,
                status TEXT,
                plan_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)

        # Config Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Admins Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                role TEXT DEFAULT 'admin'
            )        """)

        # Coupons Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS coupons (
                code TEXT PRIMARY KEY,
                discount_percent REAL,
                max_uses INTEGER,
                used_count INTEGER DEFAULT 0,
                expiry_date TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)

        # Tickets Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message TEXT,
                status TEXT DEFAULT 'OPEN',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)

        # Initialize default config if empty
        cursor.execute("SELECT count(*) FROM config")
        if cursor.fetchone()[0] == 0:
            defaults = [
                ("system_prompt", "You are a helpful, friendly, and intelligent AI assistant."),
                ("current_model", DEFAULT_MODEL),
                ("temperature", str(DEFAULT_TEMP)),
                ("max_tokens", str(DEFAULT_MAX_TOKENS)),
                ("top_p", str(DEFAULT_TOP_P)),
                ("welcome_msg", "Welcome! I am your advanced AI Assistant."),
                ("footer_text", "Powered by AI"),
                ("bot_name", "AI Assistant"),
                ("about_text", "Advanced AI Assistant Bot"),
                ("force_join_channel", ""),
                ("youtube_link", ""),
                ("instagram_link", ""),
                ("group_link", ""),
                ("website_link", ""),
                ("maintenance_mode", "0"),
                ("bot_status", "on")
            ]
            cursor.executemany("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", defaults)

        conn.commit()        conn.close()

    # --- User Methods ---
    def add_user(self, user_id: int, username: str, first_name: str, referred_by: int = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        ref_code = str(user_id) + "_" + hashlib.md5(str(user_id).encode()).hexdigest()[:6]
        try:
            cursor.execute("""
                INSERT INTO users (user_id, username, first_name, referral_code, referred_by)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, username, first_name, ref_code, referred_by))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        finally:
            conn.close()

    def get_user(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def update_last_active(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        today = datetime.date.today().isoformat()
        cursor.execute("""
            UPDATE users 
            SET last_active = CURRENT_TIMESTAMP,
                messages_today = CASE 
                    WHEN last_reset_date != ? THEN 1 
                    ELSE messages_today + 1 
                END,
                last_reset_date = CASE 
                    WHEN last_reset_date != ? THEN ? 
                    ELSE last_reset_date 
                END
            WHERE user_id = ?
        """, (today, today, today, user_id))
        conn.commit()
        conn.close()

    def reset_daily_messages(self):
        today = datetime.date.today().isoformat()
        conn = self.get_connection()
        cursor = conn.cursor()        cursor.execute("UPDATE users SET messages_today = 0, last_reset_date = ? WHERE last_reset_date != ?", (today, today))
        conn.commit()
        conn.close()

    # --- Premium Methods ---
    def is_premium(self, user_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT expiry_date FROM premium_users WHERE user_id = ? AND is_active = 1", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return False
        expiry = datetime.datetime.fromisoformat(row[0])
        if expiry < datetime.datetime.now():
            return False
        return True

    def add_premium(self, user_id: int, plan_type: str, days: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        start = datetime.datetime.now()
        if plan_type == 'lifetime':
            expiry = datetime.datetime(2099, 12, 31)
        else:
            expiry = start + datetime.timedelta(days=days)
        
        cursor.execute("""
            INSERT INTO premium_users (user_id, plan_type, start_date, expiry_date, is_active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                plan_type = excluded.plan_type,
                start_date = excluded.start_date,
                expiry_date = excluded.expiry_date,
                is_active = 1
        """, (user_id, plan_type, start.isoformat(), expiry.isoformat()))
        conn.commit()
        conn.close()

    def remove_premium(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE premium_users SET is_active = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

    # --- History Methods ---
    def add_history(self, user_id: int, role: str, content: str):
        conn = self.get_connection()
        cursor = conn.cursor()        cursor.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
        conn.commit()
        conn.close()

    def get_history(self, user_id: int, limit: int = 10):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT role, content FROM history 
            WHERE user_id = ? 
            ORDER BY id DESC LIMIT ?
        """, (user_id, limit))
        rows = cursor.fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    def clear_history(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

    # --- Config Methods ---
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

    # --- Payment Methods ---
    def add_payment(self, payment_id: str, user_id: int, amount: float, currency: str, plan_type: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO payments (payment_id, user_id, amount, currency, status, plan_type)
            VALUES (?, ?, ?, ?, 'PENDING', ?)
        """, (payment_id, user_id, amount, currency, plan_type))
        conn.commit()
        conn.close()
    def update_payment_status(self, payment_id: str, status: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        now = datetime.datetime.now().isoformat()
        cursor.execute("""
            UPDATE payments SET status = ?, completed_at = ? WHERE payment_id = ?
        """, (status, now, payment_id))
        conn.commit()
        conn.close()

    def get_pending_payment(self, payment_id: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM payments WHERE payment_id = ? AND status = 'PENDING'", (payment_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    # --- Admin/Stats Methods ---
    def get_stats(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        stats = {}
        
        cursor.execute("SELECT COUNT(*) FROM users")
        stats['total_users'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM users WHERE DATE(last_active) = DATE('now')")
        stats['active_today'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM premium_users WHERE is_active = 1")
        stats['premium_users'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM history")
        stats['total_chats'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT SUM(amount) FROM payments WHERE status = 'SUCCESS'")
        res = cursor.fetchone()[0]
        stats['revenue'] = res if res else 0.0
        
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

    def unban_user(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

# Initialize DB
db = DatabaseManager(DB_NAME)

# ==========================================================
# OPENROUTER API CLIENT
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
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tokens,
            "top_p": top_p
        }
        
        try:
            response = requests.post(self.base_url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if 'choices' in data and len(data['choices']) > 0:
                return data['choices'][0]['message']['content']
            else:                logger.error(f"OpenRouter API Error: {data}")
                return "Sorry, I encountered an error processing your request."
                
        except Exception as e:
            logger.error(f"OpenRouter Request Failed: {e}")
            return "Sorry, the AI service is currently unavailable. Please try again later."

ai_client = AIClient()

# ==========================================================
# CASHFREE PAYMENT CLIENT
# ==========================================================

class PaymentClient:
    def __init__(self):
        self.app_id = CASHFREE_APP_ID
        self.secret_key = CASHFREE_SECRET_KEY
        self.base_url = "https://sandbox.cashfree.com/pg"  # Change to https://api.cashfree.com/pg for production

    def create_order(self, order_id: str, amount: float, customer_name: str, customer_phone: str, customer_email: str) -> dict:
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
                "customer_email": customer_email or "user@example.com",
                "customer_phone": customer_phone or "9999999999"
            },
            "order_meta": {
                "return_url": f"https://t.me/{TELEGRAM_BOT_TOKEN.split(':')[0]}?start=payment_success"
            }
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Cashfree Order Creation Failed: {e}")
            return None
    def verify_payment(self, order_id: str) -> dict:
        url = f"{self.base_url}/orders/{order_id}"
        headers = {
            "x-api-version": "2023-08-01",
            "x-client-id": self.app_id,
            "x-client-secret": self.secret_key
        }
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Cashfree Verification Failed: {e}")
            return None

payment_client = PaymentClient()

# ==========================================================
# HELPER FUNCTIONS
# ==========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_banned(user_id: int) -> bool:
    user = db.get_user(user_id)
    return user and user['is_banned'] == 1

def is_maintenance_mode() -> bool:
    return db.get_config("maintenance_mode") == "1"

def check_force_join(update: Update) -> bool:
    channel = db.get_config("force_join_channel")
    if not channel:
        return True
    
    try:
        member = update.effective_chat.get_member(update.effective_user.id)
        if member.status in ['creator', 'administrator', 'member']:
            return True
    except Exception:
        pass
    
    return False

def get_plan_details(plan: str):
    plans = {
        "1month": {"price": 199, "days": 30, "name": "1 Month God Mode"},
        "3month": {"price": 499, "days": 90, "name": "3 Months God Mode"},        "lifetime": {"price": 1499, "days": 36500, "name": "Lifetime God Mode"}
    }
    return plans.get(plan, plans["1month"])

# ==========================================================
# COMMAND HANDLERS
# ==========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username, user.first_name)
    
    if is_banned(user.id):
        await update.message.reply_text("You have been banned from using this bot.")
        return

    if is_maintenance_mode() and not is_admin(user.id):
        await update.message.reply_text("Bot is currently under maintenance. Please try again later.")
        return

    if not check_force_join(update):
        channel = db.get_config("force_join_channel")
        await update.message.reply_text(
            f"Please join our channel @{channel} to use this bot.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Join Channel", url=f"https://t.me/{channel.replace('@', '')}")
            ], [
                InlineKeyboardButton("Try Again", callback_data="start_retry")
            ]])
        )
        return

    welcome_msg = db.get_config("welcome_msg")
    footer = db.get_config("footer_text")
    
    keyboard = [
        [InlineKeyboardButton("🤖 AI Chat", callback_data="menu_ai")],
        [InlineKeyboardButton("👑 Buy Premium", callback_data="menu_premium")],
        [InlineKeyboardButton("👤 My Profile", callback_data="menu_profile")],
        [InlineKeyboardButton("📊 Plans & Pricing", callback_data="menu_plans")],
        [InlineKeyboardButton("🆘 Help & Support", callback_data="menu_help")]
    ]
    
    social_row = []
    yt = db.get_config("youtube_link")
    ig = db.get_config("instagram_link")
    grp = db.get_config("group_link")
    
    if yt: social_row.append(InlineKeyboardButton("YouTube", url=yt))
    if ig: social_row.append(InlineKeyboardButton("Instagram", url=ig))    if grp: social_row.append(InlineKeyboardButton("Support Group", url=grp))
    
    if social_row:
        keyboard.append(social_row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"{welcome_msg}\n\n{footer}"
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
*Available Commands:*

/start - Start the bot
/newchat - Reset conversation memory
/model - View current AI model
/profile - View your account details
/buy - Purchase Premium God Mode
/referral - Invite friends and earn rewards
/support - Contact support team

*Admin Commands:*
/setprompt, /setmodel, /broadcast, /users, /stats
    """
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.clear_history(update.effective_user.id)
    await update.message.reply_text("🔄 Conversation memory reset. Start a new topic!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message or not update.message.text:
        return

    if update.message.text.startswith('/'):
        return

    if is_banned(user.id):
        return

    if is_maintenance_mode() and not is_admin(user.id):
        await update.message.reply_text("Bot is under maintenance.")
        return

    if not check_force_join(update):
        channel = db.get_config("force_join_channel")
        await update.message.reply_text(f"Please join @{channel} to chat.")
        return
    is_prem = db.is_premium(user.id)
    user_data = db.get_user(user.id)
    
    if not is_prem:
        if user_data['messages_today'] >= FREE_DAILY_LIMIT:
            await update.message.reply_text(
                "❌ You have reached your daily free limit (50 messages).\n"
                "Upgrade to *God Mode* for unlimited chats!",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Upgrade Now", callback_data="menu_premium")
                ]])
            )
            return

    await context.bot.send_chat_action(chat_id=user.id, action=ChatAction.TYPING)

    system_prompt = db.get_config("system_prompt")
    model = db.get_config("current_model")
    temp = float(db.get_config("temperature"))
    max_tokens = int(db.get_config("max_tokens"))
    top_p = float(db.get_config("top_p"))

    history = db.get_history(user.id, limit=10)
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": update.message.text})

    db.add_history(user.id, "user", update.message.text)
    db.update_last_active(user.id)

    response_text = ai_client.generate_response(messages, model, temp, max_tokens, top_p)

    db.add_history(user.id, "assistant", response_text)

    await update.message.reply_text(response_text, parse_mode=ParseMode.MARKDOWN)

# ==========================================================
# CALLBACK QUERY HANDLER
# ==========================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    if data == "start_retry":
        if check_force_join(update):
            await query.edit_message_text("Thanks for joining! How can I help you?", reply_markup=None)
            await start_command(update, context)
        else:
            await query.edit_message_text("You haven't joined the channel yet.")

    elif data == "menu_ai":
        await query.edit_message_text(" *AI Chat Active*\n\nJust send me a message to start chatting!", parse_mode=ParseMode.MARKDOWN)

    elif data == "menu_profile":
        user = db.get_user(user_id)
        is_prem = db.is_premium(user_id)
        plan_status = "👑 God Mode Active" if is_prem else "Free Plan"
        
        expiry_str = "N/A"
        if is_prem:
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT expiry_date FROM premium_users WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            conn.close()
            if row:
                expiry_str = row[0].split('T')[0]

        text = (
            f"👤 *Profile*\n\n"
            f"*ID:* `{user_id}`\n"
            f"*Name:* {user['first_name']}\n"
            f"*Plan:* {plan_status}\n"
            f"*Expiry:* {expiry_str}\n"
            f"*Messages Today:* {user['messages_today']}/{FREE_DAILY_LIMIT}\n"
            f"*Referrals:* 0"
        )
        
        kb = [[InlineKeyboardButton("Back", callback_data="start_back")]]
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

    elif data == "menu_premium" or data == "menu_plans":
        kb = [
            [InlineKeyboardButton("1 Month - ₹199", callback_data="buy_1month")],
            [InlineKeyboardButton("3 Months - ₹499", callback_data="buy_3month")],
            [InlineKeyboardButton("Lifetime - ₹1499", callback_data="buy_lifetime")],
            [InlineKeyboardButton("Back", callback_data="start_back")]
        ]
        text = "👑 *Upgrade to God Mode*\n\n✅ Unlimited Messages\n✅ Premium Models (GPT-4, Llama-3)\n✅ Faster Responses\n✅ Priority Support\n\nSelect a plan:"
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

    elif data == "menu_help":
        text = "For support, please contact @SupportUser or use /support command."        await query.edit_message_text(text)

    elif data == "start_back":
        await start_command(update, context)

    elif data.startswith("buy_"):
        plan_key = data.split("_")[1]
        plan = get_plan_details(plan_key)
        
        order_id = f"ORD_{user_id}_{int(time.time())}"
        db.add_payment(order_id, user_id, plan['price'], "INR", plan_key)
        
        cf_response = payment_client.create_order(
            order_id=order_id,
            amount=plan['price'],
            customer_name=query.from_user.first_name,
            customer_phone="", 
            customer_email=""
        )
        
        if cf_response and 'payment_session_id' in cf_response:
            session_id = cf_response['payment_session_id']
            redirect_url = cf_response.get('redirect_url', '')
            
            if redirect_url:
                kb = [[InlineKeyboardButton("Pay Now 💳", url=redirect_url)]]
                await query.edit_message_text(
                    f" *Order Created*\n\nPlan: {plan['name']}\nAmount: ₹{plan['price']}\n\nClick below to pay. After payment, your premium will activate automatically.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(kb)
                )
            else:
                await query.edit_message_text("Error creating payment link. Please try again.")
        else:
            await query.edit_message_text("Payment gateway error. Please contact admin.")

    elif data.startswith("admin_"):
        if not is_admin(user_id):
            await query.answer("Access Denied", show_alert=True)
            return
        
        if data == "admin_panel":
            stats = db.get_stats()
            text = (
                f"📊 *Admin Dashboard*\n\n"
                f"Total Users: {stats['total_users']}\n"
                f"Active Today: {stats['active_today']}\n"
                f"Premium Users: {stats['premium_users']}\n"
                f"Total Revenue: ₹{stats['revenue']}\n"
                f"Total Chats: {stats['total_chats']}"            )
            kb = [
                [InlineKeyboardButton("Users", callback_data="admin_users"), InlineKeyboardButton("Broadcast", callback_data="admin_broadcast")],
                [InlineKeyboardButton("AI Settings", callback_data="admin_ai"), InlineKeyboardButton("Payments", callback_data="admin_payments")],
                [InlineKeyboardButton("Close", callback_data="start_back")]
            ]
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

        elif data == "admin_ai":
            model = db.get_config("current_model")
            prompt = db.get_config("system_prompt")[:50] + "..."
            text = f"⚙️ *AI Settings*\n\nModel: `{model}`\nPrompt: {prompt}"
            kb = [
                [InlineKeyboardButton("Change Model", callback_data="admin_setmodel")],
                [InlineKeyboardButton("Change Prompt", callback_data="admin_setprompt")],
                [InlineKeyboardButton("Back", callback_data="admin_panel")]
            ]
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

# ==========================================================
# ADMIN COMMANDS
# ==========================================================

async def admin_panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("Opening Admin Panel...", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("Open Dashboard", callback_data="admin_panel")
    ]]))

async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setmodel <model_id>\nExample: /setmodel openai/gpt-4o-mini")
        return
    model_id = context.args[0]
    db.set_config("current_model", model_id)
    await update.message.reply_text(f"✅ Model updated to: `{model_id}`", parse_mode=ParseMode.MARKDOWN)

async def set_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setprompt <new_system_prompt>")
        return
    prompt = " ".join(context.args)
    db.set_config("system_prompt", prompt)
    await update.message.reply_text("✅ System prompt updated.")
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    message = " ".join(context.args)
    users = db.get_all_users()
    
    sent = 0
    failed = 0
    status_msg = await update.message.reply_text(f"Starting broadcast to {len(users)} users...")
    
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=message, parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast fail to {uid}: {e}")
            
        if sent % 10 == 0:
            await status_msg.edit_text(f"Broadcasting... Sent: {sent}, Failed: {failed}")

    await status_msg.edit_text(f"✅ Broadcast Complete.\nSent: {sent}\nFailed: {failed}")

async def add_premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addpremium <user_id> <days>")
        return
    uid = int(context.args[0])
    days = int(context.args[1])
    db.add_premium(uid, "manual", days)
    await update.message.reply_text(f"✅ Added {days} days premium to user {uid}")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return
    uid = int(context.args[0])
    db.ban_user(uid)
    await update.message.reply_text(f"🚫 User {uid} banned.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return    if not context.args:
        return
    uid = int(context.args[0])
    db.unban_user(uid)
    await update.message.reply_text(f"✅ User {uid} unbanned.")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    stats = db.get_stats()
    text = (
        f"📊 *Bot Statistics*\n\n"
        f"Total Users: {stats['total_users']}\n"
        f"Active Today: {stats['active_today']}\n"
        f"Premium Users: {stats['premium_users']}\n"
        f"Revenue: ₹{stats['revenue']}\n"
        f"Total Chats: {stats['total_chats']}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ==========================================================
# PAYMENT VERIFICATION
# ==========================================================

async def check_payment_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /checkpayment <order_id>")
        return
    
    order_id = context.args[0]
    pending = db.get_pending_payment(order_id)
    
    if not pending:
        await update.message.reply_text("Order not found or already processed.")
        return
    
    status_data = payment_client.verify_payment(order_id)
    
    if status_data and status_data.get('order_status') == 'PAID':
        db.update_payment_status(order_id, 'SUCCESS')
        db.add_premium(pending['user_id'], pending['plan_type'], get_plan_details(pending['plan_type'])['days'])
        await update.message.reply_text("✅ Payment Successful! God Mode Activated.")
    else:
        await update.message.reply_text("❌ Payment not verified yet. Please wait or contact support.")

# ==========================================================
# MAIN EXECUTION
# ==========================================================

def main():    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables.")
        sys.exit(1)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("newchat", new_chat))
    application.add_handler(CommandHandler("checkpayment", check_payment_status))
    
    application.add_handler(CommandHandler("setmodel", set_model))
    application.add_handler(CommandHandler("setprompt", set_prompt))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("addpremium", add_premium_cmd))
    application.add_handler(CommandHandler("ban", ban_cmd))
    application.add_handler(CommandHandler("unban", unban_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("admin", admin_panel_cmd))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(msg="Exception while handling an update:", exc_info=context.error)
        
    application.add_error_handler(error_handler)

    logger.info("Bot is starting in Polling mode...")
    print("✅ Bot started successfully!")  # Added for Render log visibility
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
