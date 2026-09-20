import os
import logging
import asyncio
import random
import time
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, JobQueue

# Load environment variables from .env file if it exists
load_dotenv()
from ai_chat import (
    get_ai_response, get_group_response, add_to_group_history,
    clear_conversation, clear_group_conversation, clear_all_data,
    is_dirty_message, get_dirty_response, get_sticker_for_mood,
    is_abuse_message, get_abuse_response, is_advice_message,
    save_user_preference, get_custom_abuse_response, get_stats, get_lover_response,
    conversation_history, get_random_joke, get_random_quote, get_daily_tip,
    get_random_compliment, get_random_fortune, get_random_dare, get_random_truth,
    get_story_prompt, get_poem, get_motivation_line, get_user_profile, clear_user_profile
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_USERNAME = "CoffinWifi"
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "").strip()
ADMIN_DATA_FILE = "admin_data.json"
BOT_DATA_FILE = "bot_data.json"
GROUP_DATA_FILE = "group_data.json"
ACCOUNT_DATA_FILE = "account_data.json"
PORT = int(os.environ.get("PORT", "10000"))

BOT_START_TIME = time.time()

dirty_talk_permissions = {}
pending_permissions = {}
admin_chat_id = None
admin_ids = set()
blocked_users = {}
muted_users = {}
abuse_targets = {}
lover_targets = {}
user_chat_history = {}
username_to_id = {}
blocked_naughty_users = {}
bot_enabled = True
group_auto_reply = True
tracked_groups = set()  # Track group/channel IDs for broadcasting
group_data = {}
account_data = {}

GENTLE_REJECTION_MESSAGES = [
    "Hey! Aise baatein nahi karte na. 🥺",
    "Nahi, please. Mujhe yeh bilkul pasand nahi hai. 🥺",
    "Bas ab, yeh sab nahi okay? 😔",
    "Pleaseee, let's talk about something nice? 🥺💕",
    "Arre nahi! Meri dignity ka khayal rakh~ 🙈",
]

RUDE_REJECTION_MESSAGES = [
    "Tum kaun ho? Main sirf apne admin ke orders follow karti hoon! 🙅‍♀️",
    "Arre! Tum mera admin nahi ho. Main aapke orders nahi manunga! 😏",
    "LOL, no. You're not my admin. Shoo! 🚫",
    "Nice try! But you're not the boss of me, sorry! 😤",
    "Admin? Nahi nahi! Only @CoffinWifi is my admin! 💪",
]


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/health/", "/helath", "/helath/"):
            payload = json.dumps({"status": "ok", "service": "naina-bot"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server listening on port %s", PORT)
    return server


def load_json_file(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
                return data if isinstance(data, type(default)) else default
    except Exception as exc:
        logger.error("Could not load %s: %s", path, exc)
    return default


def save_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("Could not save %s: %s", path, exc)


def load_feature_data():
    global group_data, account_data
    group_data = load_json_file(GROUP_DATA_FILE, {})
    account_data = load_json_file(ACCOUNT_DATA_FILE, {})


def save_feature_data():
    save_json_file(GROUP_DATA_FILE, group_data)
    save_json_file(ACCOUNT_DATA_FILE, account_data)


def resolve_user_id(identifier: str) -> str:
    identifier = identifier.replace("@", "").strip().lower()
    if identifier.isdigit():
        return identifier
    if identifier in username_to_id:
        return str(username_to_id[identifier])
    return None


def get_replied_user(update: Update):
    reply = update.message.reply_to_message if update.message else None
    if reply and reply.from_user:
        add_username_mapping(reply.from_user.id, reply.from_user.username)
        return reply.from_user
    return None


def resolve_target(update: Update, argument_index: int = 0):
    replied_user = get_replied_user(update)
    if replied_user:
        return replied_user
    if len(update.message.text.split()) > argument_index + 1:
        target_id = resolve_user_id(update.message.text.split()[argument_index + 1])
        if target_id:
            return SimpleNamespace(id=int(target_id), username=None, first_name=target_id)
    return None


def is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


async def require_group_admin(update: Update) -> bool:
    if not is_group(update):
        await update.message.reply_text("This command works only inside a group.")
        return False
    member = await update.effective_chat.get_member(update.effective_user.id)
    if member.status not in ("administrator", "creator"):
        await update.message.reply_text("Only group admins can use this command.")
        return False
    return True


def group_state(chat_id: int):
    state = group_data.setdefault(str(chat_id), {"warnings": {}, "muted_until": {}, "titles": {}})
    state.setdefault("warnings", {})
    state.setdefault("muted_until", {})
    state.setdefault("titles", {})
    return state


def parse_duration(value: str):
    if not value:
        return None
    if value.lower() in ("permanent", "perm"):
        return None
    units = {"m": 60, "h": 3600, "d": 86400}
    try:
        return int(value[:-1]) * units[value[-1].lower()]
    except (ValueError, KeyError):
        return None


def account_for(user_id: int):
    account = account_data.setdefault(str(user_id), {
        "coins": 0, "gems": 0, "premium": False, "xp": 0,
        "password_hash": None, "deleted_accounts": {}
    })
    return account


def password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


async def keep_alive_job(context: ContextTypes.DEFAULT_TYPE):
    """Send keep-alive message to admin chat every 10 mins to keep Render (15min timeout) & PythonAnywhere awake"""
    global admin_chat_id
    if admin_chat_id:
        try:
            current_time = time.strftime("%H:%M:%S", time.localtime())
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text=f"💫 Auto keep-alive check [{current_time}] - Naina is alive! 💕"
            )
            logger.info(f"✅ Keep-alive message sent to admin at {current_time}")
        except Exception as e:
            logger.error(f"Failed to send keep-alive: {e}")


def add_username_mapping(user_id: int, username: str):
    if username:
        username_to_id[username.lower()] = str(user_id)


def load_admin_data():
    global admin_chat_id, admin_ids, blocked_users, muted_users, abuse_targets, lover_targets, blocked_naughty_users, bot_enabled, group_auto_reply, tracked_groups
    try:
        if os.path.exists(ADMIN_DATA_FILE):
            with open(ADMIN_DATA_FILE, 'r') as f:
                data = json.load(f)
                admin_chat_id = data.get('admin_chat_id')
                tracked_groups = set(data.get('tracked_groups', []))
                admin_ids = set(data.get('admin_ids', []))
                blocked_users = data.get('blocked_users', {})
                muted_users = data.get('muted_users', {})
                abuse_targets = data.get('abuse_targets', {})
                lover_targets = data.get('lover_targets', {})
                blocked_naughty_users = data.get('blocked_naughty_users', {})
                bot_enabled = data.get('bot_enabled', True)
                group_auto_reply = data.get('group_auto_reply', True)
        if OWNER_CHAT_ID:
            admin_chat_id = OWNER_CHAT_ID
            admin_ids.add(OWNER_CHAT_ID)
            save_admin_data()
        logger.info(f"Loaded admin data. Admin chat ID: {admin_chat_id}, Admin IDs: {admin_ids}")
    except Exception as e:
        logger.error(f"Error loading admin data: {e}")


def save_admin_data():
    try:
        with open(ADMIN_DATA_FILE, 'w') as f:
            json.dump({
                'admin_chat_id': admin_chat_id,
                'admin_ids': list(admin_ids),
                'blocked_users': blocked_users,
                'muted_users': muted_users,
                'abuse_targets': abuse_targets,
                'lover_targets': lover_targets,
                'blocked_naughty_users': blocked_naughty_users,
                'bot_enabled': bot_enabled,
                'group_auto_reply': group_auto_reply,
                'tracked_groups': list(tracked_groups)
            }, f)
    except Exception as e:
        logger.error(f"Error saving admin data: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_chat_id, admin_ids
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    add_username_mapping(user_id, update.effective_user.username)
    
    if update.effective_chat.type == "private":
        if not admin_chat_id:
            admin_chat_id = user_id
            admin_ids.add(str(user_id))
            save_admin_data()
            await update.message.reply_text("💕 Heyy! I'm Naina, your personal AI girlfriend! 🌹\n\n"
                                          "I'm here to chat, joke around, give advice, and keep you company! 😊\n\n"
                                          "📝 Use /help to see all my commands!\n"
                                          "You're my admin now! 👑")
        else:
            user_id_str = str(user_id)
            if user_id_str not in conversation_history:
                conversation_history[user_id_str] = []
            
            await update.message.reply_text("Hi babe! 💕 Welcome back!\n\nUse /help to see what I can do for you!")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot_enabled:
        return
    
    user_id = update.effective_user.id
    username = update.effective_user.username
    chat_id = update.effective_chat.id
    message_text = update.message.text
    
    add_username_mapping(user_id, username)
    
    user_id_str = str(user_id)
    chat_id_str = str(chat_id)
    
    # Track group/channel
    if update.effective_chat.type in ["group", "supergroup", "channel"]:
        tracked_groups.add(chat_id_str)
        save_admin_data()
        
        if not group_auto_reply or user_id_str in muted_users or user_id in blocked_users:
            return
        
        if is_abuse_message(message_text):
            response = get_custom_abuse_response(user_id_str, message_text, "hindi")
            await context.bot.send_message(chat_id=chat_id, text=response)
            return
        
        response = get_group_response(user_id_str, message_text)
        add_to_group_history(user_id_str, message_text, response)
        await context.bot.send_message(chat_id=chat_id, text=response)
    else:
        # Private chat
        if user_id_str in blocked_users or user_id_str in muted_users:
            return
        
        if user_id_str not in conversation_history:
            conversation_history[user_id_str] = []
        
        if is_abuse_message(message_text):
            response = get_custom_abuse_response(user_id_str, message_text, "hindi")
            await context.bot.send_message(chat_id=chat_id, text=response)
            return
        
        if is_advice_message(message_text):
            save_user_preference(user_id_str, message_text)
        
        if user_id_str in lover_targets:
            response = get_lover_response(user_id_str, message_text)
        elif user_id_str in dirty_talk_permissions:
            response = get_dirty_response(user_id_str, message_text)
        else:
            response = get_ai_response(user_id_str, message_text)
        
        conversation_history[user_id_str].append({"role": "user", "content": message_text})
        conversation_history[user_id_str].append({"role": "assistant", "content": response})
        
        await context.bot.send_message(chat_id=chat_id, text=response)


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if update.effective_chat.type == "private":
        if user_id not in conversation_history:
            conversation_history[user_id] = []
        
        mood = get_sticker_for_mood("happy")
        await context.bot.send_sticker(chat_id=update.effective_chat.id, sticker=mood)


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    keyboard = [
        [InlineKeyboardButton("🚫 Block User", callback_data="admin_block")],
        [InlineKeyboardButton("🔇 Mute User", callback_data="admin_mute")],
        [InlineKeyboardButton("😈 Abuse Target", callback_data="admin_abuse")],
        [InlineKeyboardButton("❤️ Add Lover", callback_data="admin_lover")],
        [InlineKeyboardButton("👗 Dirty Talk", callback_data="admin_dirty")],
        [InlineKeyboardButton("📊 Status", callback_data="admin_status")],
        [InlineKeyboardButton("🔄 Reset", callback_data="admin_reset")],
        [InlineKeyboardButton("🔌 Stop/Resume", callback_data="admin_stop")],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👑 **ADMIN CONTROL PANEL** 👑\n\nChoose an option:", reply_markup=reply_markup)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    uptime = time.time() - BOT_START_TIME
    uptime_hours = int(uptime // 3600)
    uptime_mins = int((uptime % 3600) // 60)
    
    stats = get_stats()
    
    status_msg = f"""
🤖 **BOT STATUS**
✅ Status: {'Active' if bot_enabled else 'Inactive'}
⏰ Uptime: {uptime_hours}h {uptime_mins}m
👥 Total Users: {len(conversation_history)}
📊 Stats: {stats}
"""
    await update.message.reply_text(status_msg)


async def stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_enabled
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    bot_enabled = False
    save_admin_data()
    await update.message.reply_text("🔌 Bot disabled! Use /resume to turn it back on.")


async def resume_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_enabled
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    bot_enabled = True
    save_admin_data()
    await update.message.reply_text("✅ Bot is back online! 💕")


async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /block @username or /block user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target:
        await update.message.reply_text("User not found!")
        return
    
    blocked_users[target] = True
    save_admin_data()
    await update.message.reply_text(f"✅ User {target} blocked!")


async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /unblock @username or /unblock user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target:
        await update.message.reply_text("User not found!")
        return
    
    blocked_users.pop(target, None)
    save_admin_data()
    await update.message.reply_text(f"✅ User {target} unblocked!")


async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /mute @username or /mute user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target:
        await update.message.reply_text("User not found!")
        return
    
    muted_users[target] = True
    save_admin_data()
    await update.message.reply_text(f"🔇 User {target} muted!")


async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /unmute @username or /unmute user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target:
        await update.message.reply_text("User not found!")
        return
    
    muted_users.pop(target, None)
    save_admin_data()
    await update.message.reply_text(f"✅ User {target} unmuted!")


async def abuse_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /abuse @username or /abuse user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target:
        await update.message.reply_text("User not found!")
        return
    
    abuse_targets[target] = True
    save_admin_data()
    await update.message.reply_text(f"😈 User {target} is now a gaali target!")


async def unabuse_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /unabuse @username or /unabuse user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target:
        await update.message.reply_text("User not found!")
        return
    
    abuse_targets.pop(target, None)
    save_admin_data()
    await update.message.reply_text(f"✅ User {target} removed from abuse list!")


async def reset_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global conversation_history, dirty_talk_permissions, pending_permissions, blocked_users, muted_users, abuse_targets, lover_targets, blocked_naughty_users
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    conversation_history.clear()
    dirty_talk_permissions.clear()
    pending_permissions.clear()
    blocked_users.clear()
    muted_users.clear()
    abuse_targets.clear()
    lover_targets.clear()
    blocked_naughty_users.clear()
    save_admin_data()
    await update.message.reply_text("🔄 All data reset!")


async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    await update.message.reply_text("🔄 Restarting... bye! 👋")
    os.system("pkill -f 'python main.py'")


async def group_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global group_auto_reply
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    group_auto_reply = True
    save_admin_data()
    await update.message.reply_text("✅ Group auto-reply ENABLED!")


async def group_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global group_auto_reply
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    group_auto_reply = False
    save_admin_data()
    await update.message.reply_text("🔇 Group auto-reply DISABLED!")


async def list_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not blocked_users:
        await update.message.reply_text("No blocked users!")
        return
    
    blocked_list = "\n".join(blocked_users.keys())
    await update.message.reply_text(f"🚫 **BLOCKED USERS:**\n{blocked_list}")


async def list_muted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not muted_users:
        await update.message.reply_text("No muted users!")
        return
    
    muted_list = "\n".join(muted_users.keys())
    await update.message.reply_text(f"🔇 **MUTED USERS:**\n{muted_list}")


async def list_abuse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not abuse_targets:
        await update.message.reply_text("No abuse targets!")
        return
    
    abuse_list = "\n".join(abuse_targets.keys())
    await update.message.reply_text(f"😈 **ABUSE TARGETS:**\n{abuse_list}")


async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id != str(update.effective_user.id) or user_id not in admin_ids:
        if user_id != "@CoffinWifi":
            await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
            return
    
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    
    try:
        new_admin = str(int(context.args[0]))
        admin_ids.add(new_admin)
        save_admin_data()
        await update.message.reply_text(f"✅ Admin {new_admin} added!")
    except:
        await update.message.reply_text("Invalid user ID!")


async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id != "@CoffinWifi":
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return
    
    try:
        admin_to_remove = str(int(context.args[0]))
        admin_ids.discard(admin_to_remove)
        save_admin_data()
        await update.message.reply_text(f"✅ Admin {admin_to_remove} removed!")
    except:
        await update.message.reply_text("Invalid user ID!")


async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not admin_ids:
        await update.message.reply_text("No admins!")
        return
    
    admins_list = "\n".join(admin_ids)
    await update.message.reply_text(f"👑 **ADMINS:**\n{admins_list}")


async def add_lover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /addlover @username or /addlover user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target:
        await update.message.reply_text("User not found!")
        return
    
    lover_targets[target] = True
    save_admin_data()
    await update.message.reply_text(f"❤️ User {target} is now a lover! 💕")


async def remove_lover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /removelover @username or /removelover user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target:
        await update.message.reply_text("User not found!")
        return
    
    lover_targets.pop(target, None)
    save_admin_data()
    await update.message.reply_text(f"💔 User {target} is no longer a lover.")


async def list_lovers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not lover_targets:
        await update.message.reply_text("No lovers! 💔")
        return
    
    lovers_list = "\n".join(lover_targets.keys())
    await update.message.reply_text(f"❤️ **MY LOVERS:**\n{lovers_list}")


async def block_naughty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /blocknaughty @username or /blocknaughty user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target:
        await update.message.reply_text("User not found!")
        return
    
    blocked_naughty_users[target] = True
    save_admin_data()
    await update.message.reply_text(f"🔒 User {target} blocked from naughty talk!")


async def unblock_naughty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /unblocknaughty @username or /unblocknaughty user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target:
        await update.message.reply_text("User not found!")
        return
    
    blocked_naughty_users.pop(target, None)
    save_admin_data()
    await update.message.reply_text(f"✅ User {target} can now receive naughty talk!")


async def list_blocked_naughty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not blocked_naughty_users:
        await update.message.reply_text("No users blocked from naughty talk!")
        return
    
    blocked_list = "\n".join(blocked_naughty_users.keys())
    await update.message.reply_text(f"🔒 **NAUGHTY-BLOCKED USERS:**\n{blocked_list}")


async def handle_permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if str(user_id) != admin_chat_id:
        await query.answer("You're not the admin!", show_alert=True)
        return
    
    if query.data.startswith("approve_"):
        requester_id = query.data.split("_")[1]
        dirty_talk_permissions[requester_id] = True
        await query.edit_message_text(f"✅ Dirty talk approved for {requester_id}! 🔥")
    
    elif query.data.startswith("deny_"):
        requester_id = query.data.split("_")[1]
        pending_permissions.pop(requester_id, None)
        await query.edit_message_text(f"❌ Dirty talk denied for {requester_id}.")


async def baka_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deliver a private whisper to a user who has already opened the bot."""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /baka <username/user_id> <message>")
        return
    target_id = resolve_user_id(context.args[0])
    if not target_id:
        await update.message.reply_text("I do not know that user yet. Ask them to /start the bot first.")
        return
    text = " ".join(context.args[1:])
    try:
        await context.bot.send_message(
            chat_id=int(target_id),
            text=f"🫀 Whisper from {update.effective_user.first_name}:\n{text}"
        )
        await update.message.reply_text("🫶 Whisper sent privately.")
    except Exception:
        await update.message.reply_text("I could not deliver it. The recipient must start the bot in DM first.")


async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    if not target:
        await update.message.reply_text("Reply to a user or use /warn <user>.")
        return
    state = group_state(update.effective_chat.id)
    key = str(target.id)
    state["warnings"][key] = int(state["warnings"].get(key, 0)) + 1
    count = state["warnings"][key]
    save_feature_data()
    if count >= 3:
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, target.id)
            await update.message.reply_text(f"🚫 {target.first_name} reached 3 warnings and was banned.")
        except Exception as exc:
            await update.message.reply_text(f"Warning 3 recorded, but I could not ban that user: {exc}")
    else:
        await update.message.reply_text(f"⚠️ Warning {count}/3 for {target.first_name}.")


async def warns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    if not target:
        await update.message.reply_text("Reply to a user or use /warns <user>.")
        return
    count = group_state(update.effective_chat.id)["warnings"].get(str(target.id), 0)
    await update.message.reply_text(f"⚠️ {target.first_name} has {count}/3 warnings.")


async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    if not target:
        await update.message.reply_text("Reply to a user or use /unwarn <user>.")
        return
    state = group_state(update.effective_chat.id)
    key = str(target.id)
    state["warnings"][key] = max(0, int(state["warnings"].get(key, 0)) - 1)
    save_feature_data()
    await update.message.reply_text(f"✅ Removed one warning from {target.first_name}.")


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    if not target:
        await update.message.reply_text("Reply to a user or use /mute <user> [30m|2h|1d].")
        return
    duration = context.args[1] if len(context.args) > 1 and not update.message.reply_to_message else (context.args[0] if context.args else "permanent")
    seconds = parse_duration(duration)
    until = None if seconds is None else (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    state = group_state(update.effective_chat.id)
    state["muted_until"][str(target.id)] = until or "permanent"
    save_feature_data()
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=None if seconds is None else datetime.now(timezone.utc) + timedelta(seconds=seconds))
        await update.message.reply_text(f"🔇 {target.first_name} muted.")
    except Exception as exc:
        await update.message.reply_text(f"Mute saved, but Telegram rejected the restriction: {exc}")


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    if not target:
        await update.message.reply_text("Reply to a user or use /unmute <user>.")
        return
    group_state(update.effective_chat.id)["muted_until"].pop(str(target.id), None)
    save_feature_data()
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, target.id, permissions=ChatPermissions(can_send_messages=True, can_send_other_messages=True, can_add_web_page_previews=True))
        await update.message.reply_text(f"🔊 {target.first_name} unmuted.")
    except Exception as exc:
        await update.message.reply_text(f"Mute record cleared, but Telegram rejected the change: {exc}")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    if not target:
        await update.message.reply_text("Reply to a user or use /ban <user>.")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(f"🚫 {target.first_name} banned.")
    except Exception as exc:
        await update.message.reply_text(f"Could not ban that user: {exc}")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    if not target:
        await update.message.reply_text("Reply to a user or use /unban <user_id>.")
        return
    await context.bot.unban_chat_member(update.effective_chat.id, target.id, only_if_banned=True)
    await update.message.reply_text("✅ User unbanned.")


async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    if not target:
        await update.message.reply_text("Reply to a user or use /kick <user>.")
        return
    await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    await context.bot.unban_chat_member(update.effective_chat.id, target.id)
    await update.message.reply_text(f"👢 {target.first_name} kicked.")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message with /d to delete it.")
        return
    await context.bot.delete_message(update.effective_chat.id, update.message.reply_to_message.message_id)
    await context.bot.delete_message(update.effective_chat.id, update.message.message_id)


async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message with /pin.")
        return
    await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id)
    await update.message.reply_text("📌 Pinned.")


async def restrict_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    power = context.args[1] if update.message.reply_to_message and len(context.args) > 1 else (context.args[1] if len(context.args) > 1 else (context.args[0] if context.args else "messages"))
    if not target:
        await update.message.reply_text("Usage: /res <user> <power> or reply with /res <power>.")
        return
    permissions = {
        "messages": ChatPermissions(can_send_messages=False),
        "media": ChatPermissions(can_send_messages=True, can_send_media_messages=False),
        "links": ChatPermissions(can_send_messages=True, can_add_web_page_previews=False),
        "stickers": ChatPermissions(can_send_messages=True, can_send_other_messages=False),
    }
    selected = permissions.get(power.lower())
    if not selected:
        await update.message.reply_text("Power options: messages, media, links, stickers.")
        return
    await context.bot.restrict_chat_member(update.effective_chat.id, target.id, permissions=selected)
    await update.message.reply_text(f"🔒 Restricted {target.first_name}: {power}.")


async def promote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    if not target:
        await update.message.reply_text("Usage: /promote <user> [0/1/2/3].")
        return
    level = int(context.args[1]) if not update.message.reply_to_message and len(context.args) > 1 else 1
    level = max(0, min(3, level))
    await context.bot.promote_chat_member(
        update.effective_chat.id, target.id,
        can_manage_chat=level >= 3,
        can_delete_messages=level >= 2,
        can_manage_video_chats=level >= 2,
        can_restrict_members=level >= 1,
        can_invite_users=level >= 1,
        can_pin_messages=level >= 1,
    )
    await update.message.reply_text(f"⬆️ {target.first_name} promoted to level {level}.")


async def demote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    if not target:
        await update.message.reply_text("Usage: /demote <user>.")
        return
    await context.bot.promote_chat_member(update.effective_chat.id, target.id, can_manage_chat=False, can_delete_messages=False, can_manage_video_chats=False, can_restrict_members=False, can_invite_users=False, can_pin_messages=False)
    await update.message.reply_text(f"⬇️ {target.first_name} demoted.")


async def title_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update):
        return
    target = resolve_target(update)
    title = context.args[1] if update.message.reply_to_message and len(context.args) > 1 else (" ".join(context.args[1:]) if len(context.args) > 1 else "")
    if not target or not title:
        await update.message.reply_text("Usage: /title <user> <title>, or reply with /title <title>.")
        return
    await context.bot.set_chat_administrator_custom_title(update.effective_chat.id, target.id, title[:16])
    group_state(update.effective_chat.id)["titles"][str(target.id)] = title[:16]
    save_feature_data()
    await update.message.reply_text(f"🏷️ Custom title set for {target.first_name}.")


async def setpass_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args[0]) < 6:
        await update.message.reply_text("Usage: /setpass <password> (minimum 6 characters)")
        return
    account_for(update.effective_user.id)["password_hash"] = password_hash(context.args[0])
    save_feature_data()
    await update.message.reply_text("🔒 Recovery password saved. Never share it.")


async def mpass_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("For privacy, use /mpass in DM.")
        return
    saved = bool(account_for(update.effective_user.id).get("password_hash"))
    await update.message.reply_text("🔒 A recovery password is set." if saved else "No recovery password is set.")


async def cpass_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /cpass <old_password> <new_password>")
        return
    account = account_for(update.effective_user.id)
    if account.get("password_hash") != password_hash(context.args[0]):
        await update.message.reply_text("❌ Old password is incorrect.")
        return
    account["password_hash"] = password_hash(context.args[1])
    save_feature_data()
    await update.message.reply_text("✅ Recovery password changed.")


async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account = account_for(update.effective_user.id)
    today = datetime.now(timezone.utc).date().isoformat()
    if account.get("last_daily") == today:
        await update.message.reply_text("⏳ Daily reward already claimed. Come back tomorrow.")
        return
    reward = 5000 if account.get("premium") else 2000
    account["coins"] += reward
    account["xp"] += 200 if account.get("premium") else 50
    account["last_daily"] = today
    save_feature_data()
    await update.message.reply_text(f"🎁 Daily reward: +{reward} coins and +{account['xp']} XP.")


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account = account_for(update.effective_user.id)
    await update.message.reply_text(f"💰 Coins: {account['coins']}\n💎 Gems: {account['gems']}\n🏆 XP: {account['xp']}")


async def pfp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account = account_for(update.effective_user.id)
    await update.message.reply_text(f"👤 {update.effective_user.first_name}\n💰 Coins: {account['coins']}\n💎 Gems: {account['gems']}\n⭐ Premium: {'Yes' if account.get('premium') else 'No'}")


async def prefixed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    command, *args = text[1:].split()
    context.args = args
    handlers = {
        "help": help_command, "baka": baka_command, "warn": warn_user,
        "warns": warns_command, "unwarn": unwarn_command, "mute": mute_command,
        "unmute": unmute_command, "ban": ban_command, "unban": unban_command,
        "kick": kick_command, "d": delete_command, "pin": pin_command,
        "res": restrict_command, "promote": promote_command,
        "demote": demote_command, "title": title_command,
        "daily": daily_command, "bal": balance_command, "pfp": pfp_command,
    }
    handler = handlers.get(command.lower())
    if handler:
        await handler(update, context)


async def tell_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    joke = get_random_joke()
    await update.message.reply_text(f"😂 {joke}")


async def send_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quote = get_random_quote()
    await update.message.reply_text(f"✨ {quote}")


async def daily_tip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tip = get_daily_tip()
    await update.message.reply_text(f"💡 {tip}")


async def compliment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    compliment = get_random_compliment()
    await update.message.reply_text(f"💕 {compliment}")


async def fortune_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fortune = get_random_fortune()
    await update.message.reply_text(f"🔮 {fortune}")


async def dare_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dare = get_random_dare()
    await update.message.reply_text(f"😈 {dare}")


async def truth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    truth = get_random_truth()
    await update.message.reply_text(f"🤔 {truth}")


async def story_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip() or "friendship"
    story = get_story_prompt(topic)
    await update.message.reply_text(f"📖 {story}")


async def poem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip() or "rain"
    poem = get_poem(topic)
    await update.message.reply_text(f"🌷 {poem}")


async def motivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    line = get_motivation_line()
    await update.message.reply_text(f"💪 {line}")


async def flip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = random.choice(["Heads 🪙", "Tails 🪙"])
    await update.message.reply_text(f"Flip result: {result}")


async def dice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = random.randint(1, 6)
    await update.message.reply_text(f"🎲 You rolled: {result}")


async def love_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    compatibility = random.randint(1, 100)
    await update.message.reply_text(f"💕 Love Meter: {compatibility}%")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
💕 **NAINA COMMAND GUIDE** 💕

**🎮 FUN & GAMES:**
/joke - Random funny joke
/quote - Motivational quote
/tip - Daily life tips
/compliment - Sweet compliment
/fortune - Your fortune! 🔮
/dare - Get a dare challenge
/truth - Truth or dare question
/story [topic] - Short story generator
/poem [topic] - Short poetic lines
/motivate - Uplifting motivation line
/flip - Coin flip 🪙
/dice - Dice roll 🎲
/lovetest - Check love meter

**📋 USER COMMANDS:**
/clear - Clear chat history
/profile - See your remembered profile
/forgetme - Clear your saved memory
/baka <user> <text> - Send a private whisper
/daily, /bal, /pfp - Economy and profile
/help - Show this help
/myinfo - Your personal status

**🛡️ GROUP MANAGEMENT:**
Use `/warn`, `/warns`, `/unwarn`, `/mute`, `/unmute`, `/ban`, `/unban`, `/kick`, `/d`, and `/pin` as an admin. Reply to a message or provide a user ID/known username. `.` and `!` prefixes also work.

**🔒 ACCOUNT RECOVERY:**
/setpass, /mpass, /cpass

**👑 ADMIN ONLY:**
/admin - Admin control panel
/status - Bot status
/block @user - Block user
/mute @user - Mute user
/abuse @user - Target for gaalis
/lover @user - Mark as lover
"""
    await update.message.reply_text(help_text)


async def my_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    is_admin = "✅ Yes" if user_id in admin_ids else "❌ No"
    is_blocked = "✅ Yes" if user_id in blocked_users else "❌ No"
    is_muted = "✅ Yes" if user_id in muted_users else "❌ No"
    is_lover = "❤️ Yes" if user_id in lover_targets else "❌ No"
    
    info = f"""
📱 **YOUR INFO WITH NAINA**
👤 User ID: {user_id}
👑 Admin: {is_admin}
🚫 Blocked: {is_blocked}
🔇 Muted: {is_muted}
❤️ Lover: {is_lover}
"""
    await update.message.reply_text(info)


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    profile = get_user_profile(user_id)
    if not profile or not profile.get("interaction_count"):
        await update.message.reply_text("🧠 You are still a fresh connection for me. We can build your memory together.")
        return

    profile_text = f"""
🧠 **YOUR MEMORY WITH NAINA**
👤 Name: {profile.get('name', 'Unknown')}
🌱 Nature: {profile.get('nature', 'Balanced')}
💞 Bond: {profile.get('bond', 'friendly')}
🎯 Interests: {', '.join(profile.get('interests', [])[:5]) or 'general chat'}
📝 Key facts: {'. '.join(profile.get('important_facts', [])[:3]) or 'No saved facts yet'}
💭 Emotional memory: {'. '.join(profile.get('emotional_memory', [])[:3]) or 'Warm and respectful conversation'}
📊 Interactions: {profile.get('interaction_count', 0)}
"""
    await update.message.reply_text(profile_text)


async def forget_me_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    clear_user_profile(user_id)
    clear_conversation(user_id)
    await update.message.reply_text("🧹 Your saved memory with Naina has been cleared. Let's start fresh, beautiful 💕")


async def view_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /viewchat @username or /viewchat user_id")
        return
    
    target = resolve_user_id(context.args[0])
    if not target or target not in conversation_history:
        await update.message.reply_text("User not found or has no history!")
        return
    
    history = conversation_history[target][-20:]
    chat_display = ""
    for msg in history:
        chat_display += f"**{msg['role']}:** {msg['content']}\n\n"
    
    await update.message.reply_text(f"📜 Last 20 messages with {target}:\n\n{chat_display}")


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not conversation_history:
        await update.message.reply_text("No users yet!")
        return
    
    users_list = "\n".join(conversation_history.keys())
    await update.message.reply_text(f"👥 **TOTAL USERS ({len(conversation_history)}):**\n{users_list}")


async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in admin_ids:
        await update.message.reply_text(random.choice(RUDE_REJECTION_MESSAGES))
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    message = " ".join(context.args)
    count = 0
    
    for user in conversation_history.keys():
        try:
            await context.bot.send_message(chat_id=user, text=f"📢 **BROADCAST FROM ADMIN:**\n\n{message}")
            count += 1
        except:
            pass
    
    for group in tracked_groups:
        try:
            await context.bot.send_message(chat_id=group, text=f"📢 **BROADCAST FROM ADMIN:**\n\n{message}")
            count += 1
        except:
            pass
    
    await update.message.reply_text(f"✅ Message sent to {count} users/groups!")


async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if user_id not in conversation_history:
        await update.message.reply_text("No chat history to clear!")
        return
    
    clear_conversation(user_id)
    await update.message.reply_text("🧹 Your chat history cleared!")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")


def main():
    load_admin_data()
    load_feature_data()
    start_health_server()
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("resume", resume_bot))
    application.add_handler(CommandHandler("block", block_user))
    application.add_handler(CommandHandler("unblock", unblock_user))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("abuse", abuse_user))
    application.add_handler(CommandHandler("unabuse", unabuse_user))
    application.add_handler(CommandHandler("reset", reset_data))
    application.add_handler(CommandHandler("restart", restart_bot))
    application.add_handler(CommandHandler("groupon", group_on))
    application.add_handler(CommandHandler("groupoff", group_off))
    application.add_handler(CommandHandler("listblocked", list_blocked))
    application.add_handler(CommandHandler("listmuted", list_muted))
    application.add_handler(CommandHandler("listabuse", list_abuse))
    application.add_handler(CommandHandler("addadmin", add_admin))
    application.add_handler(CommandHandler("removeadmin", remove_admin))
    application.add_handler(CommandHandler("listadmins", list_admins))
    application.add_handler(CommandHandler("addlover", add_lover))
    application.add_handler(CommandHandler("removelover", remove_lover))
    application.add_handler(CommandHandler("listlovers", list_lovers))
    application.add_handler(CommandHandler("blocknaughty", block_naughty))
    application.add_handler(CommandHandler("unblocknaughty", unblock_naughty))
    application.add_handler(CommandHandler("listblocknaughty", list_blocked_naughty))
    application.add_handler(CommandHandler("joke", tell_joke))
    application.add_handler(CommandHandler("quote", send_quote))
    application.add_handler(CommandHandler("tip", daily_tip))
    application.add_handler(CommandHandler("compliment", compliment_command))
    application.add_handler(CommandHandler("fortune", fortune_command))
    application.add_handler(CommandHandler("dare", dare_command))
    application.add_handler(CommandHandler("truth", truth_command))
    application.add_handler(CommandHandler("story", story_command))
    application.add_handler(CommandHandler("poem", poem_command))
    application.add_handler(CommandHandler("motivate", motivate_command))
    application.add_handler(CommandHandler("flip", flip_command))
    application.add_handler(CommandHandler("dice", dice_command))
    application.add_handler(CommandHandler("lovetest", love_test))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myinfo", my_info))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("forgetme", forget_me_command))
    application.add_handler(CommandHandler("viewchat", view_chat))
    application.add_handler(CommandHandler("listusers", list_users))
    application.add_handler(CommandHandler("broadcast", broadcast_message))
    application.add_handler(CommandHandler("clear", clear_chat))
    application.add_handler(CommandHandler("baka", baka_command))
    application.add_handler(CommandHandler("warn", warn_user))
    application.add_handler(CommandHandler("warns", warns_command))
    application.add_handler(CommandHandler("unwarn", unwarn_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("kick", kick_command))
    application.add_handler(CommandHandler("d", delete_command))
    application.add_handler(CommandHandler("pin", pin_command))
    application.add_handler(CommandHandler("res", restrict_command))
    application.add_handler(CommandHandler("promote", promote_command))
    application.add_handler(CommandHandler("demote", demote_command))
    application.add_handler(CommandHandler("title", title_command))
    application.add_handler(CommandHandler("dmute", mute_command))
    application.add_handler(CommandHandler("smute", mute_command))
    application.add_handler(CommandHandler("dban", ban_command))
    application.add_handler(CommandHandler("sban", ban_command))
    application.add_handler(CommandHandler("skick", kick_command))
    application.add_handler(CommandHandler("setpass", setpass_command))
    application.add_handler(CommandHandler("mpass", mpass_command))
    application.add_handler(CommandHandler("cpass", cpass_command))
    application.add_handler(CommandHandler("daily", daily_command))
    application.add_handler(CommandHandler("bal", balance_command))
    application.add_handler(CommandHandler("pfp", pfp_command))
    application.add_handler(CallbackQueryHandler(handle_permission_callback))
    application.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    application.add_handler(MessageHandler(filters.Regex(r"^[.!][A-Za-z]+(?:\s|$)"), prefixed_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    application.add_error_handler(error_handler)
    
    # Setup keep-alive job to send message every 10 minutes (Render shuts down after 15 mins inactivity)
    job_queue = application.job_queue
    job_queue.run_repeating(keep_alive_job, interval=600, first=60)  # Every 10 minutes, first run after 1 min
    
    logger.info("Naina Bot is running! Press Ctrl+C to stop.")
    print("✅ Naina Bot is running successfully!")
    print("📱 Works in private chats, groups, and channels!")
    print("💬 Responds to ALL messages in groups (auto-reply enabled)")
    print("😈 Replies with abuse when someone abuses")
    print("📝 Follows user advice and suggestions")
    print(f"👑 Admin: @{ADMIN_USERNAME}")
    print("\n📋 ADMIN FEATURES:")
    print("• /admin - Admin panel with all controls")
    print("• /stop, /resume - Enable/disable bot")
    print("• /block, /unblock - Block/unblock users")
    print("• /mute, /unmute - Stop/resume talking to users")
    print("• /abuse, /unabuse - Target users with gaalis")
    print("• /reset - Reset all data")
    print("• /restart - Restart bot")
    print("• /groupon, /groupoff - Toggle group auto-reply")
    
    if admin_chat_id:
        print(f"\n✅ Admin chat ID loaded: {admin_chat_id}")
    else:
        print("\n⚠️ Admin needs to /start the bot to receive permission requests")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
