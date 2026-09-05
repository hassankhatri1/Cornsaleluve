import asyncio
import html
import io
import json
import logging
import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone

import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InputMediaVideo
from telegram.constants import ParseMode
from telegram.error import TelegramError, Forbidden, RetryAfter
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters,
)

# ============================================================
# CONFIGURATION — preserved from the existing project
# ============================================================
BOT_TOKEN = "8848838995:AAHfSAEzeVISIHo77IBgR66UsLhY_gO8zMA"  # Set your BotFather token here; no environment variable is used.
ADMIN_ID = 8239453740
# Multiple-admin support. Keep the existing admin ID and optionally add IDs via ADMIN_IDS=1,2,3.
ADMIN_IDS = {ADMIN_ID}
try:
    ADMIN_IDS.update(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
except ValueError:
    pass
DB_FILE = "bot.sqlite"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Conversation states. No deleted/stale plan states are referenced.
PLAN_NAME, PLAN_PRICE, UPI_ID, CHANNEL_LINK, DEMO_MEDIA, WELCOME_IMAGE, WELCOME_TEXT, BROADCAST_CONTENT, REJECTION_REASON, EDIT_PLAN_NAME, EDIT_PLAN_PRICE, DB_IMPORT, CHOOSE_PHOTO, PRO_PHOTO, USER_SEARCH, WELCOME_VIDEO, WELCOME_VIDEOS = range(17)

DEFAULT_WELCOME = "💎 Welcome to our Premium Service!"
LIFETIME_DAYS = 365

# Fallback conversation routing. This keeps admin setup buttons working even if
# a deployment has an older ConversationHandler callback dispatch behavior.
def set_manual_state(context, state):
    context.user_data["_manual_state"] = state

def clear_manual_state(context):
    context.user_data.pop("_manual_state", None)


def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def esc(value):
    return html.escape("" if value is None else str(value))


def bold(text):
    return f"<b>{text}</b>"


def style_button(text, callback_data=None, url=None, style=None):
    """Create a Telegram inline button, compatible with older PTB installs.

    Telegram Bot API supports button styles (primary/blue, success/green,
    danger/red). PTB added the explicit ``style`` constructor argument in
    v22.7. If an older PTB is installed, pass the field through ``api_kwargs``
    when available instead of crashing the bot.
    """
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style not in {"primary", "success", "danger"}:
        return InlineKeyboardButton(**kwargs)

    try:
        # PTB >= 22.7
        return InlineKeyboardButton(**kwargs, style=style)
    except TypeError as exc:
        # Older PTB: style can still be forwarded as a raw Bot API field.
        if "unexpected keyword argument 'style'" not in str(exc):
            raise
        try:
            return InlineKeyboardButton(**kwargs, api_kwargs={"style": style})
        except TypeError:
            # Very old PTB: keep the button functional, but unstyled.
            return InlineKeyboardButton(**kwargs)


def reply_button(text, style=None):
    """Create a reply-keyboard button with native Telegram color styling.

    Uses the same compatibility strategy as inline buttons so an older PTB
    installation does not stop the bot with an unexpected ``style`` error.
    """
    kwargs = {"text": text}
    if style not in {"primary", "success", "danger"}:
        return KeyboardButton(**kwargs)

    try:
        return KeyboardButton(**kwargs, style=style)
    except TypeError as exc:
        if "unexpected keyword argument 'style'" not in str(exc):
            raise
        try:
            return KeyboardButton(**kwargs, api_kwargs={"style": style})
        except TypeError:
            return KeyboardButton(**kwargs)
def main_user_inline_keyboard():
    return InlineKeyboardMarkup([
        [style_button("💎 GET PREMIUM", "user:premium", style="success")],
        [style_button("🥵 DEMO", "user:demo", style="danger")],
        [style_button("✅ HOW TO GET PREMIUM", "user:tutorial", style="primary")],
    ])


def user_reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            [reply_button("💎 GET PREMIUM", "success")],
            [reply_button("🥵 DEMO", "danger")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [style_button("📊 DASHBOARD", "admin:dashboard", style="success"), style_button("💎 PLANS", "admin:plans", style="success")],
        [style_button("👥 USERS", "admin:users", style="primary"), style_button("💳 PAYMENTS", "admin:payments", style="primary")],
        [style_button("🥵 DEMO", "admin:demo", style="danger"), style_button("🏦 UPI SETTINGS", "admin:upi", style="primary")],
        [style_button("🖼️ WELCOME SETTINGS", "admin:welcome", style="primary")],
        [style_button("📢 BROADCAST", "admin:broadcast", style="primary")],
        [style_button("💾 BACKUP / RESTORE", "admin:db:backup", style="primary")],
        [style_button("👑 ADMIN MANAGEMENT", "admin:security", style="primary")],
        [style_button("📥 IMPORT DATABASE", "admin:db:import", style="primary"), style_button("📤 EXPORT DATABASE", "admin:db:export", style="primary")],
    ])


def back_keyboard(target="admin:menu"):
    return InlineKeyboardMarkup([[style_button("🔙 Back", target, style="primary")]])


def current_admin_ids():
    ids = set(ADMIN_IDS)
    try:
        raw = get_setting('extra_admins', '') or ''
        ids.update(int(x.strip()) for x in raw.split(',') if x.strip())
    except (ValueError, TypeError):
        pass
    return ids

def admin_only(obj):
    user = getattr(obj, "effective_user", None) or getattr(obj, "from_user", None)
    return bool(user and user.id in current_admin_ids())


def safe_name(user):
    return user.full_name or user.username or str(user.id)


def init_db():
    con = db()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            registered_at TEXT NOT NULL,
            premium_until TEXT,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            validity_days INTEGER NOT NULL,
            description TEXT,
            link TEXT,
            image_file_id TEXT,
            enabled INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS plan_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER,
            media_type TEXT,
            file_id TEXT,
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS demo_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT,
            file_id TEXT,
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tutorial(
            id INTEGER PRIMARY KEY CHECK(id=1),
            video_file_id TEXT,
            text TEXT
        );

        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            plan_id INTEGER NOT NULL,
            plan_name TEXT,
            amount REAL,
            validity INTEGER,
            screenshot_file_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            rejection_reason TEXT,
            created_at TEXT NOT NULL,
            approved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS broadcasts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT,
            file_id TEXT,
            text TEXT,
            target TEXT DEFAULT 'all',
            created_at TEXT NOT NULL,
            total INTEGER DEFAULT 0,
            success INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        );
        """)

        cols = {r["name"] for r in con.execute("PRAGMA table_info(plans)").fetchall()}
        if "category" not in cols:
            con.execute("ALTER TABLE plans ADD COLUMN category TEXT NOT NULL DEFAULT 'choose'")
        con.execute("UPDATE plans SET category='choose' WHERE category IS NULL OR category=''")
        bcols = {r["name"] for r in con.execute("PRAGMA table_info(broadcasts)").fetchall()}
        if "target" not in bcols:
            con.execute("ALTER TABLE broadcasts ADD COLUMN target TEXT NOT NULL DEFAULT 'all'")

        con.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('welcome_text',?)",
            (DEFAULT_WELCOME,),
        )
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('choose_photo','')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('pro_photo','')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('extra_admins','')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('welcome_videos','')")
        con.commit()
    finally:
        con.close()


def log_event(event, details=""):
    try:
        con = db()
        con.execute(
            "INSERT INTO logs(event,details,created_at) VALUES(?,?,?)",
            (event, str(details)[:1000], now_iso()),
        )
        con.commit()
        con.close()
    except Exception:
        logger.exception("log_event failed")


def set_setting(key, value):
    con = db()
    try:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()
    finally:
        con.close()


def get_setting(key, default=None):
    con = db()
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        con.close()


async def answer(q, text=None, alert=False):
    try:
        await q.answer(text, show_alert=alert)
    except TelegramError:
        pass


def register_user(user):
    con = db()
    try:
        con.execute("""
            INSERT INTO users(id,username,first_name,last_name,registered_at,status)
            VALUES(?,?,?,?,?,'active')
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name
        """, (
            user.id, user.username, user.first_name, user.last_name, now_iso()
        ))
        con.commit()
    finally:
        con.close()


async def send_start_screen(bot, chat_id):
    text = get_setting("welcome_text", DEFAULT_WELCOME) or DEFAULT_WELCOME
    image = get_setting("welcome_image")
    inline_kb = main_user_inline_keyboard()

    # Up to five welcome videos are stored together and sent as one Telegram
    # media group (album), so users do not receive five separate messages.
    raw_videos = get_setting("welcome_videos", "") or ""
    videos = []
    try:
        parsed = json.loads(raw_videos)
        if isinstance(parsed, list):
            videos = [str(x) for x in parsed if x]
    except (ValueError, TypeError):
        videos = []

    # Backward compatibility with the older single-video setting.
    if not videos:
        legacy_video = get_setting("welcome_video")
        if legacy_video:
            videos = [legacy_video]

    if videos:
        try:
            media = []
            for i, file_id in enumerate(videos[:5]):
                kwargs = {"media": file_id}
                if i == 0:
                    kwargs["caption"] = bold(esc(text))
                    kwargs["parse_mode"] = ParseMode.HTML
                media.append(InputMediaVideo(**kwargs))
            await bot.send_media_group(chat_id, media)
            await bot.send_message(
                chat_id,
                bold("👇 Choose an option:"),
                parse_mode=ParseMode.HTML,
                reply_markup=inline_kb,
            )
            await bot.send_message(
                chat_id,
                "👇",
                reply_markup=user_reply_keyboard(),
            )
            return
        except TelegramError:
            pass

    if image:
        try:
            await bot.send_photo(
                chat_id,
                image,
                caption=bold(esc(text)),
                parse_mode=ParseMode.HTML,
                reply_markup=inline_kb,
            )
            await bot.send_message(
                chat_id,
                bold("👇 Choose an option:"),
                parse_mode=ParseMode.HTML,
                reply_markup=user_reply_keyboard(),
            )
            return
        except TelegramError:
            pass

    await bot.send_message(
        chat_id,
        bold(esc(text)),
        parse_mode=ParseMode.HTML,
        reply_markup=inline_kb,
    )
    await bot.send_message(chat_id, bold("👇 Choose an option:"), parse_mode=ParseMode.HTML, reply_markup=user_reply_keyboard())

async def tutorial_user(q, context):
    await answer(q)
    con = db()
    try:
        row = con.execute("SELECT * FROM tutorial WHERE id=1").fetchone()
    finally:
        con.close()

    if not row or (not row["video_file_id"] and not row["text"]):
        await q.message.reply_text(
            bold("✅ How to Get Premium is not configured yet."),
            parse_mode=ParseMode.HTML,
        )
        return

    if row["video_file_id"]:
        try:
            await context.bot.send_video(q.message.chat_id, row["video_file_id"])
        except TelegramError:
            pass
    if row["text"]:
        await context.bot.send_message(
            q.message.chat_id,
            bold(esc(row["text"])),
            parse_mode=ParseMode.HTML,
        )
    await context.bot.send_message(
        q.message.chat_id,
        bold("✅ HOW TO GET PREMIUM"),
        parse_mode=ParseMode.HTML,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    register_user(update.effective_user)
    await send_start_screen(context.bot, update.effective_chat.id)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text(bold("⛔ Admin access required"), parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        bold("🔐 Admin Panel"),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_main_keyboard(),
    )


async def edit_or_send(q, text, keyboard=None):
    try:
        await q.edit_message_text(
            bold(text), parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    except TelegramError:
        await q.message.reply_text(
            bold(text), parse_mode=ParseMode.HTML, reply_markup=keyboard
        )


def plan_heading(name):
    value = (name or "").strip().upper()
    return value if value.endswith(" PLAN") else f"{value} PLAN"


def get_plans(category):
    con = db()
    try:
        return con.execute(
            "SELECT * FROM plans WHERE enabled=1 AND category=? ORDER BY id",
            (category,),
        ).fetchall()
    finally:
        con.close()


async def send_purchase_broadcast(bot, payment_id, plan_name, screenshot_file_id):
    """Broadcast an approved purchase to all registered users.

    Uses the verified payment screenshot and live plan buttons. Errors are
    isolated per user so one blocked/deleted chat cannot stop the broadcast.
    """
    try:
        con = db()
        try:
            users = con.execute("SELECT id FROM users").fetchall()
        finally:
            con.close()

        plans = get_plans("choose") + get_plans("pro")
        rows = [[style_button(f"💎 {plan['name']}", f"user:plan:{plan['id']}", style=["success", "danger", "primary"][i % 3])] for i, plan in enumerate(plans)]
        keyboard = InlineKeyboardMarkup(rows) if rows else None

        caption = (
            "🎉 <b>NEW PURCHASE SUCCESS</b>\n\n"
            f"💎 <b>PLAN NAME:</b> {esc(plan_name)}\n"
            f"🧾 <b>PAYMENT ID:</b> #{payment_id}\n\n"
            "✅ <b>Payment verified by admin.</b>"
        )

        success = 0
        failed = 0
        for row in users:
            chat_id = row["id"]
            try:
                if screenshot_file_id:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=screenshot_file_id,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                success += 1
            except RetryAfter as exc:
                try:
                    await asyncio.sleep(float(exc.retry_after) + 0.5)
                    if screenshot_file_id:
                        await bot.send_photo(chat_id=chat_id, photo=screenshot_file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
                    else:
                        await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
                    success += 1
                except TelegramError as retry_exc:
                    failed += 1
                    log_event("Purchase broadcast retry failed", f"{chat_id}: {retry_exc}")
            except TelegramError as exc:
                failed += 1
                log_event("Purchase broadcast failed", f"{chat_id}: {exc}")

        log_event("Purchase broadcast complete", f"payment=#{payment_id}, total={len(users)}, success={success}, failed={failed}")
    except Exception as exc:
        logger.exception("Purchase broadcast crashed")
        log_event("Purchase broadcast crashed", f"payment=#{payment_id}: {exc}")


async def send_category_plans(bot, chat_id, category="choose", admin_view=False):
    heading = "══════« PRO PLAN »═════" if category == "pro" else "══════« CHOOSE PLAN »═════"
    plans = get_plans(category)
    photo = get_setting("pro_photo" if category == "pro" else "choose_photo")
    rows = [[style_button(f"💎 {plan['name']}", f"user:plan:{plan['id']}", style=["success","danger","primary"][i % 3])] for i, plan in enumerate(plans)]
    if admin_view:
        rows = [[style_button(f"💎 {plan['name']}", f"admin:plan:view:{plan['id']}", style="primary"), style_button("✏️ EDIT", f"admin:plan:edit:{plan['id']}", style="primary")] for plan in plans]
    keyboard = InlineKeyboardMarkup(rows) if rows else None
    caption = bold(heading + ("\n\nNo plans available." if not plans else ""))
    if photo and not admin_view:
        try:
            await bot.send_photo(chat_id, photo, caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except TelegramError:
            await bot.send_message(chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await bot.send_message(chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    if admin_view:
        label = "CHOOSE" if category == "choose" else "PRO"
        controls = [
            [style_button(f"➕ ADD {label} PLAN", f"admin:plans:add:{category}", style="success")],
            [style_button(f"🖼️ SET {label} PHOTO", f"admin:photo:{category}", style="primary")]
        ]
        await bot.send_message(chat_id, bold(f"{label} PLAN SETTINGS"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(controls))

async def show_all_plans(bot, chat_id):
    # GET PREMIUM shows both categories: CHOOSE PLAN list first, as its own
    # message, then a second message with the PRO PLAN list.
    await send_category_plans(bot, chat_id, "choose")
    await send_category_plans(bot, chat_id, "pro")


async def show_choose_plans(q):
    await answer(q)
    await show_all_plans(q.get_bot(), q.message.chat_id)


async def send_pro_plans(q):
    await answer(q)
    await send_category_plans(q.get_bot(), q.message.chat_id, "pro")


async def send_plan(q, context, plan_id):
    con = db()
    try:
        p = con.execute(
            "SELECT * FROM plans WHERE id=? AND enabled=1",
            (plan_id,),
        ).fetchone()
    finally:
        con.close()

    if not p:
        await answer(q, "Plan unavailable.", True)
        return

    upi = (get_setting("upi_id") or "").strip()
    if not upi:
        await answer(q)
        await q.message.reply_text(
            bold("🏦 UPI payment is not configured yet."),
            parse_mode=ParseMode.HTML,
        )
        return

    price = float(p["price"])
    amount = f"{price:.2f}".rstrip("0").rstrip(".")
    merchant = "Premium"
    uri = "upi://pay?" + urllib.parse.urlencode({
        "pa": upi,
        "pn": merchant,
        "am": amount,
        "cu": "INR",
    })

    qr = io.BytesIO()
    qrcode.make(uri).save(qr, format="PNG")
    qr.seek(0)

    caption = (
        f"🏷️ Price : ₹{price:g}\n\n"
        f"🏦 𝐔𝐏𝐈 𝐈𝐃: {esc(upi)}\n\n"
        "1️⃣ 𝐒𝐜𝐚𝐧  |  2️⃣ 𝐏𝐚𝐲  |  3️⃣ 𝐂𝐥𝐢𝐜𝐤 ' GET LINK '"
    )

    keyboard = InlineKeyboardMarkup([[
        style_button("GET LINK", f"user:getlink:{p['id']}", style="success")
    ]])

    await answer(q)
    await context.bot.send_photo(
        q.message.chat_id,
        qr,
        caption=bold(caption),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def get_link(q, context, plan_id):
    con = db()
    try:
        p = con.execute(
            "SELECT * FROM plans WHERE id=? AND enabled=1", (plan_id,)
        ).fetchone()
    finally:
        con.close()

    if not p:
        await answer(q, "Plan unavailable.", True)
        return

    context.user_data["payment_plan_id"] = p["id"]
    context.user_data["awaiting_payment_screenshot"] = True
    await answer(q)
    await context.bot.send_message(
        q.message.chat_id,
        bold("📸 Please send your payment screenshot."),
        parse_mode=ParseMode.HTML,
    )


async def handle_payment_photo(update, context):
    if not context.user_data.get("awaiting_payment_screenshot"):
        return False

    if not update.message or not update.message.photo:
        return False

    plan_id = context.user_data.get("payment_plan_id")
    user = update.effective_user

    con = db()
    try:
        p = con.execute(
            "SELECT * FROM plans WHERE id=? AND enabled=1", (plan_id,)
        ).fetchone()

        if not p:
            context.user_data.pop("payment_plan_id", None)
            context.user_data.pop("awaiting_payment_screenshot", None)
            await update.message.reply_text(
                bold("❌ The selected plan is no longer available."),
                parse_mode=ParseMode.HTML,
            )
            return True

        duplicate = con.execute(
            "SELECT id FROM payments WHERE user_id=? AND plan_id=? AND status='pending' LIMIT 1",
            (user.id, p['id'])
        ).fetchone()
        if duplicate:
            context.user_data.pop('payment_plan_id', None)
            context.user_data.pop('awaiting_payment_screenshot', None)
            await update.message.reply_text(bold(f"⏳ This plan already has a pending payment. Order ID: #{duplicate['id']}"), parse_mode=ParseMode.HTML)
            return True

        cur = con.execute("""
            INSERT INTO payments(
                user_id,username,plan_id,plan_name,amount,validity,
                screenshot_file_id,status,created_at
            )
            VALUES(?,?,?,?,?,?,?,'pending',?)
        """, (
            user.id,
            user.username or "",
            p["id"],
            p["name"],
            p["price"],
            p["validity_days"],
            update.message.photo[-1].file_id,
            now_iso(),
        ))
        payment_id = cur.lastrowid
        con.commit()
    finally:
        con.close()

    context.user_data.pop("payment_plan_id", None)
    context.user_data.pop("awaiting_payment_screenshot", None)

    admin_text = (
        "💳 PAYMENT REQUEST\n\n"
        f"PLAN NAME: {esc(p['name'])}\n"
        f"USER: {esc(safe_name(user))}\n"
        f"USER ID: {user.id}\n"
        f"PRICE: ₹{p['price']:g}\n"
        f"PAYMENT ID: {payment_id}"
    )

    keyboard = InlineKeyboardMarkup([[
        style_button(
            "✅ APPROVED",
            f"admin:payment:approve:{payment_id}",
            style="success",
        ),
        style_button(
            "❌ REJECT",
            f"admin:payment:reject:{payment_id}",
            style="danger",
        ),
    ]])

    for admin_id in current_admin_ids():
        try:
            await context.bot.send_photo(
                admin_id,
                update.message.photo[-1].file_id,
                caption=bold(admin_text),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except TelegramError as exc:
            log_event("Admin payment notification failed", f"{admin_id}: {exc}")

    await update.message.reply_text(
        bold("✅ Payment screenshot received."),
        parse_mode=ParseMode.HTML,
    )
    return True


async def approve_payment(q, context, payment_id):
    if not admin_only(q):
        return

    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        payment = con.execute(
            "SELECT * FROM payments WHERE id=?", (payment_id,)
        ).fetchone()

        if not payment:
            con.rollback()
            await answer(q, "Payment not found.", True)
            return

        if payment["status"] != "pending":
            con.rollback()
            await answer(q, f"Payment already {payment['status']}.", True)
            return

        # Preserve the existing premium tracking while granting lifetime access.
        current = datetime.now(timezone.utc)
        user_row = con.execute(
            "SELECT premium_until FROM users WHERE id=?", (payment["user_id"],)
        ).fetchone()

        old = None
        if user_row and user_row["premium_until"]:
            try:
                old = datetime.fromisoformat(user_row["premium_until"])
            except ValueError:
                old = None

        base = old if old and old > current else current
        expiry = base + timedelta(days=max(1, int(payment['validity'] or LIFETIME_DAYS)))

        con.execute(
            "UPDATE payments SET status='approved', approved_at=? WHERE id=?",
            (now_iso(), payment_id),
        )
        con.execute(
            "UPDATE users SET premium_until=? WHERE id=?",
            (expiry.isoformat(), payment["user_id"]),
        )
        con.commit()
    except Exception:
        con.rollback()
        logger.exception("approval failed")
        await answer(q, "Approval failed.", True)
        return
    finally:
        con.close()

    channel = (get_setting("premium_channel") or "").strip()
    join_button = None
    if channel:
        join_button = InlineKeyboardMarkup([[
            style_button(
                "JOIN NOW",
                url=channel,
                style="success",
            )
        ]])

    await answer(q, "Approved")

    # ------------------------------------------------------------
    # PURCHASE SUCCESS NOTIFICATION
    # Send the original payment screenshot back to the buyer together
    # with a clear NEW PURCHASE SUCCESS message and the plan name.
    # ------------------------------------------------------------
    success_caption = (
        "🎉 <b>NEW PURCHASE SUCCESS</b>\n\n"
        f"💎 <b>PLAN NAME:</b> {esc(payment['plan_name'])}\n"
        f"💰 <b>AMOUNT:</b> ₹{float(payment['amount'] or 0):g}\n"
        f"🧾 <b>PAYMENT ID:</b> #{payment_id}\n\n"
        "✅ <b>Payment approved successfully.</b>\n"
        "🔐 <b>Premium access has been granted.</b>"
    )

    try:
        if payment["screenshot_file_id"]:
            await context.bot.send_photo(
                payment["user_id"],
                payment["screenshot_file_id"],
                caption=success_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=join_button,
            )
        else:
            await context.bot.send_message(
                payment["user_id"],
                bold(success_caption),
                parse_mode=ParseMode.HTML,
                reply_markup=join_button,
            )
    except TelegramError as exc:
        log_event("Purchase success notification failed", f"{payment['user_id']}: {exc}")

    # ------------------------------------------------------------
    # AUTOMATIC PURCHASE BROADCAST
    # Send the VERIFIED payment screenshot + purchase details + the
    # same clickable plan buttons shown under GET PREMIUM. Run it as
    # a background task so approval itself is not delayed by broadcast.
    # ------------------------------------------------------------
    asyncio.create_task(
        send_purchase_broadcast(
            context.bot,
            payment_id,
            payment["plan_name"],
            payment["screenshot_file_id"],
        )
    )


def admin_plan_menu():
    return InlineKeyboardMarkup([
        [style_button("🎯 CHOOSE PLAN", "admin:plans:choose", style="success")],
        [style_button("💎 PRO PLAN", "admin:plans:pro", style="success")],
        [style_button("🔙 Back", "admin:menu", style="primary")]
    ])

async def admin_plans(q):
    if not admin_only(q): return
    await answer(q)
    await edit_or_send(q, "💎 PLANS\n\nChoose Plan and Pro Plan are completely separate.", admin_plan_menu())


async def category_photo_start(q, context):
    if not admin_only(q): return ConversationHandler.END
    category = "pro" if q.data.endswith(":pro") else "choose"
    context.user_data.clear(); context.user_data["photo_category"] = category
    state = PRO_PHOTO if category == "pro" else CHOOSE_PHOTO
    set_manual_state(context, state)
    await answer(q)
    await q.message.reply_text(bold(f"🖼️ Send the {category.upper()} PLAN photo.\n\nOnly ONE photo is stored for this category."), parse_mode=ParseMode.HTML)
    return state

async def category_photo_save(update, context):
    if not admin_only(update) or not update.message.photo: return CHOOSE_PHOTO
    category = context.user_data.get("photo_category", "choose")
    set_setting("pro_photo" if category == "pro" else "choose_photo", update.message.photo[-1].file_id)
    context.user_data.clear(); clear_manual_state(context)
    await update.message.reply_text(bold(f"✅ {category.upper()} PLAN photo saved.\nOnly one photo is active."), parse_mode=ParseMode.HTML, reply_markup=admin_main_keyboard())
    return ConversationHandler.END

async def add_plan_start(q, context):
    if not admin_only(q):
        return ConversationHandler.END

    category = "pro" if q.data.endswith(":pro") else "choose"
    context.user_data.clear()
    context.user_data["plan_category"] = category
    set_manual_state(context, PLAN_NAME)

    label = "CHOOSE PLAN" if category == "choose" else "PRO PLAN"
    await answer(q)
    await q.message.reply_text(
        bold(f"📝 Send Plan Name:\n\nCategory: {label}"),
        parse_mode=ParseMode.HTML,
    )
    return PLAN_NAME


async def plan_name(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text(
            bold("❌ Plan name cannot be empty.\n\n📝 Send Plan Name:"),
            parse_mode=ParseMode.HTML,
        )
        return PLAN_NAME

    context.user_data["plan_name"] = name
    set_manual_state(context, PLAN_PRICE)
    await update.message.reply_text(
        bold("💰 Send Plan Price:"),
        parse_mode=ParseMode.HTML,
    )
    return PLAN_PRICE


async def plan_price(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    try:
        price = float((update.message.text or "").strip())
        if price < 0:
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text(
            bold("❌ Invalid price.\n\n💰 Send Plan Price:"),
            parse_mode=ParseMode.HTML,
        )
        return PLAN_PRICE

    name = context.user_data.get("plan_name")
    category = context.user_data.get("plan_category", "choose")
    if not name:
        context.user_data.clear()
        await update.message.reply_text(
            bold("❌ Plan session expired. Please start again."),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_main_keyboard(),
        )
        return ConversationHandler.END

    con = db()
    try:
        con.execute("""
            INSERT INTO plans(
                name,price,validity_days,description,link,image_file_id,enabled,category
            )
            VALUES(?,?,?,?,?,?,1,?)
        """, (
            name,
            price,
            LIFETIME_DAYS,
            "Lifetime",
            "",
            None,
            category,
        ))
        con.commit()
    finally:
        con.close()

    context.user_data.clear()
    clear_manual_state(context)

    await update.message.reply_text(
        bold(
            "ADMIN PLAN CREATED SUCCESSFULLY\n\n"
            f"PLAN: {esc(name)}\n"
            f"PRICE: ₹{price:g}\n"
            f"VALIDITY: {LIFETIME_DAYS} days\n"
            f"CATEGORY: {'PRO PLAN' if category == 'pro' else 'CHOOSE PLAN'}"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_plan_menu(),
    )
    return ConversationHandler.END


async def edit_plan_start(q, context):
    if not admin_only(q):
        return ConversationHandler.END
    plan_id = int(q.data.rsplit(":", 1)[1])
    con = db()
    try:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    finally:
        con.close()
    if not plan:
        await answer(q, "Plan not found.", True)
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["edit_plan_id"] = plan_id
    set_manual_state(context, EDIT_PLAN_NAME)
    await answer(q)
    await q.message.reply_text(
        bold(f"✏️ EDIT PLAN\n\nCurrent name: {esc(plan['name'])}\n\n📝 Send new Plan Name:"),
        parse_mode=ParseMode.HTML,
    )
    return EDIT_PLAN_NAME


async def edit_plan_name(update, context):
    if not admin_only(update):
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text(bold("❌ Name cannot be empty.\n\n📝 Send new Plan Name:"), parse_mode=ParseMode.HTML)
        return EDIT_PLAN_NAME
    context.user_data["edit_plan_name"] = name
    set_manual_state(context, EDIT_PLAN_PRICE)
    await update.message.reply_text(bold("💰 Send new Plan Price:"), parse_mode=ParseMode.HTML)
    return EDIT_PLAN_PRICE


async def edit_plan_price(update, context):
    if not admin_only(update):
        return ConversationHandler.END
    try:
        price = float((update.message.text or "").strip())
        if price < 0:
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text(bold("❌ Invalid price.\n\n💰 Send new Plan Price:"), parse_mode=ParseMode.HTML)
        return EDIT_PLAN_PRICE
    plan_id = context.user_data.get("edit_plan_id")
    name = context.user_data.get("edit_plan_name")
    if not plan_id or not name:
        context.user_data.clear()
        await update.message.reply_text(bold("❌ Edit session expired."), parse_mode=ParseMode.HTML, reply_markup=admin_plan_menu())
        return ConversationHandler.END
    con = db()
    try:
        cur = con.execute("UPDATE plans SET name=?, price=?, validity_days=?, description=? WHERE id=?",
                          (name, price, LIFETIME_DAYS, "Lifetime", plan_id))
        con.commit()
        if cur.rowcount == 0:
            await update.message.reply_text(bold("❌ Plan not found."), parse_mode=ParseMode.HTML, reply_markup=admin_plan_menu())
            return ConversationHandler.END
    finally:
        con.close()
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold(f"✅ PLAN UPDATED SUCCESSFULLY\n\nPLAN: {esc(name)}\nPRICE: ₹{price:g}\nVALIDITY: {LIFETIME_DAYS} days"),
        parse_mode=ParseMode.HTML, reply_markup=admin_plan_menu())
    return ConversationHandler.END


async def delete_plan(q, context):
    if not admin_only(q):
        return

    plan_id = int(q.data.rsplit(":", 1)[1])
    con = db()
    try:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            await answer(q, "Plan not found.", True)
            return
        con.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        con.execute("DELETE FROM plan_media WHERE plan_id=?", (plan_id,))
        con.commit()
    finally:
        con.close()

    await answer(q, "Plan deleted")
    await edit_or_send(q, "💎 PLANS", admin_plan_menu())


async def view_plan(q, context):
    plan_id = int(q.data.rsplit(":", 1)[1])
    con = db()
    try:
        p = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    finally:
        con.close()

    if not p:
        await answer(q, "Plan not found.", True)
        return

    await answer(q)
    await edit_or_send(
        q,
        f"💎 {esc(p['name'])}\n\n"
        f"CATEGORY: {'PRO PLAN' if p['category']=='pro' else 'CHOOSE PLAN'}\n"
        f"PRICE: ₹{p['price']:g}\n"
        "VALIDITY: Lifetime",
        InlineKeyboardMarkup([[
            style_button(
                "🗑️ DELETE",
                f"admin:plan:delete:{p['id']}",
                style="danger",
            ),
            style_button("🔙 Back", "admin:plans", style="primary"),
        ]]),
    )


def upi_settings_keyboard():
    return InlineKeyboardMarkup([
        [style_button("➕ SET UPI ID", "admin:upi:set", style="success")],
        [style_button("🔗 SET PREMIUM CHANNEL", "admin:channel:set", style="success")],
        [style_button("📋 VIEW PAYMENT SETTINGS", "admin:upi:view", style="primary")],
        [style_button("🔙 Back", "admin:menu", style="primary")],
    ])


async def upi_menu(q):
    await answer(q)
    await edit_or_send(q, "🏦 UPI SETTINGS", upi_settings_keyboard())


async def upi_start(q, context):
    context.user_data.clear()
    set_manual_state(context, UPI_ID)
    await answer(q)
    await q.message.reply_text(
        bold("🏦 Send UPI ID:"),
        parse_mode=ParseMode.HTML,
    )
    return UPI_ID


async def upi_save(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    value = (update.message.text or "").strip()
    if "@" not in value or len(value) > 200:
        await update.message.reply_text(
            bold("❌ Invalid UPI ID.\n\n🏦 Send UPI ID:"),
            parse_mode=ParseMode.HTML,
        )
        return UPI_ID

    set_setting("upi_id", value)
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ UPI ID saved successfully."),
        parse_mode=ParseMode.HTML,
        reply_markup=upi_settings_keyboard(),
    )
    return ConversationHandler.END


async def channel_start(q, context):
    context.user_data.clear()
    set_manual_state(context, CHANNEL_LINK)
    await answer(q)
    await q.message.reply_text(
        bold("🔗 Send Premium Channel invite/link:"),
        parse_mode=ParseMode.HTML,
    )
    return CHANNEL_LINK


async def channel_save(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    value = (update.message.text or "").strip()
    if not value.startswith(("https://", "http://", "tg://")):
        await update.message.reply_text(
            bold("❌ Invalid channel link.\n\n🔗 Send Premium Channel invite/link:"),
            parse_mode=ParseMode.HTML,
        )
        return CHANNEL_LINK

    set_setting("premium_channel", value)
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ Premium Channel link saved successfully."),
        parse_mode=ParseMode.HTML,
        reply_markup=upi_settings_keyboard(),
    )
    return ConversationHandler.END


async def upi_view(q):
    upi = get_setting("upi_id", "Not configured") or "Not configured"
    channel = get_setting("premium_channel", "Not configured") or "Not configured"
    await answer(q)
    await edit_or_send(
        q,
        f"🏦 UPI ID: {esc(upi)}\n\n🔗 Premium Channel: {esc(channel)}",
        upi_settings_keyboard(),
    )


async def demo_menu(q):
    if not admin_only(q):
        return
    await answer(q)
    await edit_or_send(
        q,
        "🥵 Demo Videos",
        InlineKeyboardMarkup([
            [style_button("➕ ADD DEMO", "admin:demo:add", style="success")],
            [style_button("📋 VIEW DEMOS", "admin:demo:view", style="primary")],
            [style_button("🔙 Back", "admin:menu", style="primary")],
        ]),
    )


async def demo_add_start(q, context):
    if not admin_only(q):
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["demo_added_count"] = 0
    set_manual_state(context, DEMO_MEDIA)
    await answer(q)
    await q.message.reply_text(
        bold("🎥 Send demo videos (photos/documents also work).\n\nSend as many as you like, one at a time. When finished, send /done."),
        parse_mode=ParseMode.HTML,
    )
    return DEMO_MEDIA


async def demo_add(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    media_type = None
    file_id = None

    if update.message.video:
        media_type = "video"
        file_id = update.message.video.file_id
    elif update.message.photo:
        media_type = "photo"
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        media_type = "document"
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text(
            bold("❌ Please upload a video, photo, or document.\n\nOr send /done to finish."),
            parse_mode=ParseMode.HTML,
        )
        return DEMO_MEDIA

    con = db()
    try:
        next_order = con.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM demo_media"
        ).fetchone()["n"]
        con.execute(
            "INSERT INTO demo_media(media_type,file_id,sort_order) VALUES(?,?,?)",
            (media_type, file_id, next_order),
        )
        con.commit()
    finally:
        con.close()

    # Stay in the DEMO_MEDIA state so the admin can keep sending more videos
    # in the same session. The session only ends when /done is sent.
    context.user_data["demo_added_count"] = context.user_data.get("demo_added_count", 0) + 1
    count = context.user_data["demo_added_count"]
    await update.message.reply_text(
        bold(f"✅ Demo added. ({count} added this session)\n\nSend another, or /done to finish."),
        parse_mode=ParseMode.HTML,
    )
    return DEMO_MEDIA


async def demo_done(update, context):
    if not admin_only(update):
        return
    manual = context.user_data.get("_manual_state")
    if manual != DEMO_MEDIA:
        return
    count = context.user_data.get("demo_added_count", 0)
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold(f"✅ Done. {count} demo video(s) added this session."),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            style_button("🥵 DEMO", "admin:demo", style="primary")
        ]]),
    )


async def demo_view(q, context):
    con = db()
    try:
        rows = con.execute(
            "SELECT * FROM demo_media ORDER BY sort_order,id"
        ).fetchall()
    finally:
        con.close()

    if not rows:
        await edit_or_send(q, "🥵 No demo media found.", back_keyboard("admin:demo"))
        return

    await answer(q)
    for row in rows:
        try:
            if row["media_type"] == "video":
                await context.bot.send_video(q.message.chat_id, row["file_id"])
            elif row["media_type"] == "photo":
                await context.bot.send_photo(q.message.chat_id, row["file_id"])
            else:
                await context.bot.send_document(q.message.chat_id, row["file_id"])
        except TelegramError:
            pass

    await q.message.reply_text(
        bold("🥵 DEMO"),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("admin:demo"),
    )


async def welcome_menu(q):
    await answer(q)
    await edit_or_send(
        q,
        "🖼️🎥 Welcome Settings",
        InlineKeyboardMarkup([
            [style_button("🖼️ SET WELCOME IMAGE", "admin:welcome:image", style="primary")],
            [style_button("🎥 SET 5 WELCOME VIDEOS", "admin:welcome:videos", style="primary")],
            [style_button("✏️ SET WELCOME TEXT", "admin:welcome:text", style="primary")],
            [style_button("👁️ PREVIEW", "admin:welcome:preview", style="primary")],
            [style_button("🗑️ REMOVE IMAGE", "admin:welcome:remove_image", style="danger")],
            [style_button("🗑️ REMOVE 5 WELCOME VIDEOS", "admin:welcome:remove_videos", style="danger")],
            [style_button("🔙 Back", "admin:menu", style="primary")],
        ]),
    )


async def welcome_image_start(q, context):
    context.user_data.clear()
    set_manual_state(context, WELCOME_IMAGE)
    await answer(q)
    await q.message.reply_text(
        bold("🖼️ Send welcome image:"),
        parse_mode=ParseMode.HTML,
    )
    return WELCOME_IMAGE


async def welcome_image_save(update, context):
    if not admin_only(update):
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text(
            bold("❌ Send an image."),
            parse_mode=ParseMode.HTML,
        )
        return WELCOME_IMAGE

    set_setting("welcome_image", update.message.photo[-1].file_id)
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ Welcome image saved."),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("admin:welcome"),
    )
    return ConversationHandler.END


async def welcome_video_start(q, context):
    context.user_data.clear()
    set_manual_state(context, WELCOME_VIDEO)
    await answer(q)
    await q.message.reply_text(
        bold("🎥 Send welcome video:"),
        parse_mode=ParseMode.HTML,
    )
    return WELCOME_VIDEO


async def welcome_video_save(update, context):
    if not admin_only(update):
        return ConversationHandler.END
    if not update.message or not update.message.video:
        await update.message.reply_text(
            bold("❌ Send a video."),
            parse_mode=ParseMode.HTML,
        )
        return WELCOME_VIDEO

    set_setting("welcome_video", update.message.video.file_id)
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ Welcome video saved."),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("admin:welcome"),
    )
    return ConversationHandler.END


async def welcome_videos_start(q, context):
    if not admin_only(q):
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["welcome_videos"] = []
    set_manual_state(context, WELCOME_VIDEOS)
    await answer(q)
    await q.message.reply_text(
        bold("🎥 Send 5 welcome videos one by one.\n\n"
             "They will be saved together and shown to users as ONE album.\n"
             "Video 1/5 — send the first video:"),
        parse_mode=ParseMode.HTML,
    )
    return WELCOME_VIDEOS


async def welcome_videos_save(update, context):
    if not admin_only(update):
        return ConversationHandler.END
    if not update.message or not update.message.video:
        await update.message.reply_text(
            bold("❌ Please send a video.\n\n" +
                 f"Current: {len(context.user_data.get('welcome_videos', []))}/5"),
            parse_mode=ParseMode.HTML,
        )
        return WELCOME_VIDEOS

    videos = context.user_data.setdefault("welcome_videos", [])
    if len(videos) >= 5:
        return WELCOME_VIDEOS
    videos.append(update.message.video.file_id)

    if len(videos) < 5:
        await update.message.reply_text(
            bold(f"✅ Video {len(videos)}/5 received.\n\nSend video {len(videos)+1}/5:"),
            parse_mode=ParseMode.HTML,
        )
        return WELCOME_VIDEOS

    set_setting("welcome_videos", json.dumps(videos))
    # Keep the old single-video setting empty so the new five-video system is
    # the only active welcome-video source.
    set_setting("welcome_video", "")
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ All 5 welcome videos saved!\n\n"
             "They will appear together as ONE album when a user starts the bot."),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("admin:welcome"),
    )
    return ConversationHandler.END


async def welcome_remove_videos(q):
    set_setting("welcome_videos", "")
    set_setting("welcome_video", "")
    await answer(q)
    await edit_or_send(q, "🗑️ All welcome videos removed.", back_keyboard("admin:welcome"))


async def welcome_text_start(q, context):
    context.user_data.clear()
    set_manual_state(context, WELCOME_TEXT)
    await answer(q)
    await q.message.reply_text(
        bold("✏️ Send welcome text:"),
        parse_mode=ParseMode.HTML,
    )
    return WELCOME_TEXT


async def welcome_text_save(update, context):
    if not admin_only(update):
        return ConversationHandler.END
    set_setting("welcome_text", update.message.text or "")
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ Welcome text saved."),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("admin:welcome"),
    )
    return ConversationHandler.END


async def welcome_preview(q, context):
    await answer(q)
    await send_start_screen(context.bot, q.message.chat_id)


async def welcome_remove_image(q):
    set_setting("welcome_image", "")
    await answer(q)
    await edit_or_send(q, "🗑️ Welcome image removed.", back_keyboard("admin:welcome"))


async def welcome_remove_video(q):
    set_setting("welcome_video", "")
    await answer(q)
    await edit_or_send(q, "🗑️ Welcome video removed.", back_keyboard("admin:welcome"))


async def broadcast_start(q, context):
    if not admin_only(q):
        return ConversationHandler.END
    context.user_data.clear()
    await answer(q)
    await q.message.reply_text(bold("📢 BROADCAST\n\nChoose audience:"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
        [style_button("👥 ALL USERS", "admin:broadcast:target:all", style="primary")],
        [style_button("💎 PREMIUM USERS", "admin:broadcast:target:premium", style="success")],
        [style_button("🆓 NON-PREMIUM USERS", "admin:broadcast:target:free", style="primary")],
        [style_button("🔙 Back", "admin:menu", style="primary")],
    ]))
    return ConversationHandler.END

async def broadcast_target(q, context, target):
    if not admin_only(q): return
    context.user_data['broadcast_target'] = target
    set_manual_state(context, BROADCAST_CONTENT)
    await answer(q)
    await q.message.reply_text(bold("📢 Send broadcast text, photo, video or document now."), parse_mode=ParseMode.HTML)


def extract_broadcast(message):
    if message.text:
        return "text", None, message.text
    if message.photo:
        return "photo", message.photo[-1].file_id, message.caption
    if message.video:
        return "video", message.video.file_id, message.caption
    if message.document:
        return "document", message.document.file_id, message.caption
    if message.audio:
        return "audio", message.audio.file_id, message.caption
    if message.voice:
        return "voice", message.voice.file_id, message.caption
    if message.animation:
        return "animation", message.animation.file_id, message.caption
    if message.sticker:
        return "sticker", message.sticker.file_id, ""
    return None


async def broadcast_capture(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    data = extract_broadcast(update.message)
    if not data:
        await update.message.reply_text(
            bold("❌ Unsupported content. Send text or supported media."),
            parse_mode=ParseMode.HTML,
        )
        return BROADCAST_CONTENT

    context.user_data["broadcast_content"] = data
    context.user_data.setdefault('broadcast_target', 'all')
    clear_manual_state(context)

    # Show an actual preview of the captured content (not just a confirmation
    # line) before asking the admin to confirm the send.
    try:
        await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
        )
    except TelegramError:
        pass

    await update.message.reply_text(
        bold("📢 Broadcast Preview"),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [style_button("📤 SEND TO ALL", "admin:broadcast:send", style="success")],
            [style_button("❌ CANCEL", "admin:broadcast:cancel", style="danger")],
        ]),
    )
    return ConversationHandler.END


async def send_media(bot, chat_id, typ, file_id, text):
    caption = bold(esc(text or "")) if text else None
    kwargs = {"parse_mode": ParseMode.HTML} if caption else {}
    if typ == "photo":
        return await bot.send_photo(chat_id, file_id, caption=caption, **kwargs)
    if typ == "video":
        return await bot.send_video(chat_id, file_id, caption=caption, **kwargs)
    if typ == "document":
        return await bot.send_document(chat_id, file_id, caption=caption, **kwargs)
    if typ == "audio":
        return await bot.send_audio(chat_id, file_id, caption=caption, **kwargs)
    if typ == "voice":
        return await bot.send_voice(chat_id, file_id, caption=caption, **kwargs)
    if typ == "animation":
        return await bot.send_animation(chat_id, file_id, caption=caption, **kwargs)
    if typ == "sticker":
        return await bot.send_sticker(chat_id, file_id)
    raise ValueError("Unsupported broadcast type")


async def broadcast_send(q, context):
    if not admin_only(q):
        return

    data = context.user_data.get("broadcast_content")
    if not data:
        await answer(q, "No broadcast draft.", True)
        return

    typ, file_id, text = data
    target = context.user_data.get('broadcast_target', 'all')
    con = db()
    try:
        if target == 'premium':
            users = con.execute("SELECT id FROM users WHERE premium_until IS NOT NULL AND premium_until > ?", (now_iso(),)).fetchall()
        elif target == 'free':
            users = con.execute("SELECT id FROM users WHERE premium_until IS NULL OR premium_until <= ?", (now_iso(),)).fetchall()
        else:
            users = con.execute("SELECT id FROM users").fetchall()
        cur = con.execute(
            "INSERT INTO broadcasts(content_type,file_id,text,target,created_at,total) VALUES(?,?,?,?,?,?)",
            (typ, file_id, text, target, now_iso(), len(users)),
        )
        broadcast_id = cur.lastrowid
        con.commit()
    finally:
        con.close()

    success = failed = 0

    for user in users:
        try:
            if typ == "text":
                await context.bot.send_message(
                    user["id"], bold(esc(text)), parse_mode=ParseMode.HTML
                )
            else:
                await send_media(context.bot, user["id"], typ, file_id, text)
            success += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                if typ == "text":
                    await context.bot.send_message(
                        user["id"], bold(esc(text)), parse_mode=ParseMode.HTML
                    )
                else:
                    await send_media(context.bot, user["id"], typ, file_id, text)
                success += 1
            except TelegramError:
                failed += 1
        except (Forbidden, TelegramError):
            failed += 1

    con = db()
    try:
        con.execute(
            "UPDATE broadcasts SET success=?,failed=? WHERE id=?",
            (success, failed, broadcast_id),
        )
        con.commit()
    finally:
        con.close()

    context.user_data.pop("broadcast_content", None)
    clear_manual_state(context)
    await answer(q, "Broadcast complete")
    await edit_or_send(
        q,
        f"✅ Broadcast completed.\n\nTotal: {len(users)}\nSuccessful: {success}\nFailed: {failed}",
        back_keyboard("admin:menu"),
    )


async def export_database(q, context):
    if not admin_only(q):
        return
    await answer(q)
    tmp = DB_FILE + ".export.tmp"
    try:
        # Use SQLite's backup API so WAL data is included reliably.
        src = sqlite3.connect(DB_FILE)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        with open(tmp, "rb") as fh:
            data = io.BytesIO(fh.read())
        data.name = "bot.sqlite"
        await context.bot.send_document(
            q.message.chat_id, data,
            caption=bold("📤 DATABASE EXPORT"),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("database export failed")
        await q.message.reply_text(bold(f"❌ Export failed: {esc(exc)}"), parse_mode=ParseMode.HTML)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


async def import_database_start(q, context):
    if not admin_only(q):
        return ConversationHandler.END
    context.user_data.clear()
    set_manual_state(context, DB_IMPORT)
    await answer(q)
    await q.message.reply_text(
        bold("📥 IMPORT DATABASE\n\nSend the exported .sqlite or .db file now.\n\n⚠️ The imported database will replace the current database after integrity validation."),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("admin:menu"),
    )
    return DB_IMPORT


async def import_database_file(update, context):
    if not admin_only(update) or not update.message or not update.message.document:
        return ConversationHandler.END
    document = update.message.document
    name = (document.file_name or "").lower()
    if not (name.endswith(".sqlite") or name.endswith(".db")):
        await update.message.reply_text(bold("❌ Please send a .sqlite or .db database file."), parse_mode=ParseMode.HTML)
        return DB_IMPORT

    tmp = DB_FILE + ".import.tmp"
    try:
        tg_file = await document.get_file()
        await tg_file.download_to_drive(tmp)
        con = sqlite3.connect(tmp)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            if str(ok).lower() != "ok":
                raise ValueError("SQLite integrity check failed")
            required = {"users", "plans", "payments", "settings", "demo_media"}
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            missing = required - tables
            if missing:
                raise ValueError("Missing required tables: " + ", ".join(sorted(missing)))
        finally:
            con.close()
        # Keep a rollback copy until the imported DB is successfully initialized.
        backup = DB_FILE + ".before_import"
        if os.path.exists(DB_FILE):
            import shutil
            shutil.copy2(DB_FILE, backup)
        os.replace(tmp, DB_FILE)
        init_db()
        try:
            if os.path.exists(backup):
                os.remove(backup)
        except OSError:
            pass
        context.user_data.clear()
        clear_manual_state(context)
        await update.message.reply_text(
            bold("✅ DATABASE IMPORTED SUCCESSFULLY"),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_main_keyboard(),
        )
    except Exception as exc:
        logger.exception("database import failed")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        try:
            if os.path.exists(backup):
                if os.path.exists(DB_FILE):
                    os.remove(DB_FILE)
                os.replace(backup, DB_FILE)
        except OSError:
            pass
        await update.message.reply_text(bold(f"❌ Import failed: {esc(exc)}"), parse_mode=ParseMode.HTML)
        return DB_IMPORT
    return ConversationHandler.END


async def cancel_conversation(update, context):
    context.user_data.clear()
    clear_manual_state(context)
    if update.callback_query:
        await answer(update.callback_query)
    else:
        await update.message.reply_text(
            bold("❌ Cancelled."),
            parse_mode=ParseMode.HTML,
        )
    return ConversationHandler.END


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""

    # A callback begins a fresh action; clear any stale text/media input state.
    # Setup callbacks below explicitly set a new state when they need one.
    clear_manual_state(context)

    if data.startswith("admin:") and not admin_only(update):
        await answer(q, "⛔ Admin access required", True)
        return

    try:
        if not data.startswith('admin:'):
            uid = q.from_user.id
            con=db()
            try:
                blocked=con.execute('SELECT status FROM users WHERE id=?',(uid,)).fetchone()
            finally:
                con.close()
            if blocked and blocked['status']=='blocked':
                await answer(q, '⛔ Access blocked.', True)
                return
        if data == "user:premium":
            await show_choose_plans(q)
        elif data == "user:tutorial":
            await tutorial_user(q, context)
        elif data == "user:demo":
            await user_demo(q, context)
        elif data.startswith("user:choose"):
            await show_choose_plans(q)
        elif data.startswith("user:plan:"):
            await send_plan(q, context, int(data.rsplit(":", 1)[1]))
        elif data.startswith("user:getlink:"):
            await get_link(q, context, int(data.rsplit(":", 1)[1]))
        elif data.startswith("user:retry:"):
            await send_plan(q, context, int(data.rsplit(":", 1)[1]))
        elif data == "admin:menu":
            await answer(q)
            await edit_or_send(q, "🔐 Admin Panel", admin_main_keyboard())
        elif data.startswith("admin:plans:add:"):
            # Fallback for deployments where ConversationHandler does not claim the callback.
            await add_plan_start(q, context)
        elif data.startswith("admin:plan:edit:"):
            await edit_plan_start(q, context)
        elif data == "admin:db:import":
            await import_database_start(q, context)
        elif data == "admin:upi:set":
            await upi_start(q, context)
        elif data == "admin:channel:set":
            await channel_start(q, context)
        elif data == "admin:demo:add":
            await demo_add_start(q, context)
        elif data == "admin:welcome:image":
            await welcome_image_start(q, context)
        elif data == "admin:welcome:videos":
            await welcome_videos_start(q, context)
        elif data == "admin:welcome:video":
            await welcome_video_start(q, context)
        elif data == "admin:welcome:text":
            await welcome_text_start(q, context)
        elif data == "admin:broadcast:create":
            # Legacy callback name, kept as an alias so any old inline keyboard
            # still in a chat history continues to work.
            await broadcast_start(q, context)
        elif data.startswith("admin:payment:reject:"):
            await reject_payment_start(q, context)
        elif data == "admin:plans":
            await admin_plans(q)
        elif data in ("admin:plans:choose", "admin:plans:pro"):
            await send_category_plans(q.get_bot(), q.message.chat_id, "pro" if data.endswith(":pro") else "choose", admin_view=True)
        elif data.startswith("admin:photo:"):
            await category_photo_start(q, context)
        elif data == "admin:dashboard":
            await admin_dashboard(q)
        elif data == "admin:payments":
            await admin_payments(q)
        elif data == "admin:users":
            await admin_users(q)
            set_manual_state(context, USER_SEARCH)
        elif data == "admin:security":
            await security_menu(q)
        elif data == "admin:security:add":
            await admin_add_start(q, context)
        elif data == "admin:security:remove":
            await admin_remove_start(q, context)
        elif data.startswith('admin:user:add30:'):
            await user_add30(q, context, int(data.rsplit(':',1)[1]))
        elif data.startswith('admin:user:remove:'):
            await user_remove_premium(q, context, int(data.rsplit(':',1)[1]))
        elif data.startswith('admin:user:toggle:'):
            await user_toggle_block(q, context, int(data.rsplit(':',1)[1]))
        elif data.startswith('admin:broadcast:target:'):
            await broadcast_target(q, context, data.rsplit(':',1)[1])
        elif data == "admin:db:backup":
            await backup_database(q)
        elif data.startswith("admin:plan:delete:"):
            await delete_plan(q, context)
        elif data.startswith("admin:plan:view:"):
            await view_plan(q, context)
        elif data == "admin:db:export":
            await export_database(q, context)
        elif data == "admin:upi":
            await upi_menu(q)
        elif data == "admin:upi:view":
            await upi_view(q)
        elif data == "admin:welcome":
            await welcome_menu(q)
        elif data == "admin:welcome:preview":
            await welcome_preview(q, context)
        elif data == "admin:welcome:remove_image":
            await welcome_remove_image(q)
        elif data == "admin:welcome:remove_videos":
            await welcome_remove_videos(q)
        elif data == "admin:welcome:remove_video":
            await welcome_remove_video(q)
        elif data == "admin:welcome:remove":
            # Legacy callback alias.
            await welcome_remove_image(q)
        elif data == "admin:demo":
            await demo_menu(q)
        elif data == "admin:demo:view":
            await demo_view(q, context)
        elif data == "admin:broadcast":
            await broadcast_start(q, context)
        elif data == "admin:broadcast:send":
            await broadcast_send(q, context)
        elif data == "admin:broadcast:cancel":
            context.user_data.pop("broadcast_content", None)
            await answer(q)
            await edit_or_send(q, "❌ Broadcast cancelled.", back_keyboard("admin:broadcast"))
        elif data.startswith("admin:payment:approve:"):
            await approve_payment(q, context, int(data.rsplit(":", 1)[1]))
        else:
            await answer(q, "Invalid or expired action.", True)
    except Exception as exc:
        logger.exception("callback error")
        log_event("callback error", exc)
        await answer(q, "❌ Something went wrong.", True)


def get_premium_under_demo_keyboard():
    return InlineKeyboardMarkup([[
        style_button("💎 GET PREMIUM", "user:premium", style="success")
    ]])


async def user_demo(q, context):
    await answer(q)

    con = db()
    try:
        rows = con.execute(
            "SELECT * FROM demo_media ORDER BY sort_order,id"
        ).fetchall()
    finally:
        con.close()

    if not rows:
        await context.bot.send_message(
            q.message.chat_id,
            bold("🥵 No demo videos are configured yet."),
            parse_mode=ParseMode.HTML,
            reply_markup=get_premium_under_demo_keyboard(),
        )
        return

    for row in rows:
        try:
            if row["media_type"] == "video":
                await context.bot.send_video(
                    q.message.chat_id, row["file_id"],
                    reply_markup=get_premium_under_demo_keyboard(),
                )
            elif row["media_type"] == "photo":
                await context.bot.send_photo(
                    q.message.chat_id, row["file_id"],
                    reply_markup=get_premium_under_demo_keyboard(),
                )
            else:
                await context.bot.send_document(
                    q.message.chat_id, row["file_id"],
                    reply_markup=get_premium_under_demo_keyboard(),
                )
        except TelegramError:
            pass


async def message_router(update, context):
    if not update.message:
        return

    # Reliable fallback for admin setup flows when ConversationHandler is not active.
    manual = context.user_data.get("_manual_state")
    if admin_only(update) and manual is not None:
        if manual == PLAN_NAME:
            result = await plan_name(update, context)
            return result
        if manual == PLAN_PRICE:
            result = await plan_price(update, context)
            return result
        if manual == EDIT_PLAN_NAME:
            result = await edit_plan_name(update, context)
            return result
        if manual == EDIT_PLAN_PRICE:
            result = await edit_plan_price(update, context)
            return result
        if manual == UPI_ID:
            result = await upi_save(update, context)
            return result
        if manual == CHANNEL_LINK:
            result = await channel_save(update, context)
            return result
        if manual == DEMO_MEDIA:
            result = await demo_add(update, context)
            return result
        if manual == WELCOME_IMAGE:
            result = await welcome_image_save(update, context)
            return result
        if manual == WELCOME_VIDEOS:
            result = await welcome_videos_save(update, context)
            return result
        if manual == WELCOME_VIDEO:
            result = await welcome_video_save(update, context)
            return result
        if manual == WELCOME_TEXT:
            result = await welcome_text_save(update, context)
            return result
        if manual == BROADCAST_CONTENT:
            result = await broadcast_capture(update, context)
            return result
        if manual == REJECTION_REASON:
            result = await process_rejection(update, context)
            return result
        if manual == DB_IMPORT:
            result = await import_database_file(update, context)
            return result
        if manual == USER_SEARCH:
            if context.user_data.get('admin_manage_action'):
                return await admin_manage_capture(update, context)
            return await user_search_capture(update, context)
        if manual in (CHOOSE_PHOTO, PRO_PHOTO):
            result = await category_photo_save(update, context)
            return result

    # Reply-keyboard buttons are normal messages, so handle them explicitly.
    text = (update.message.text or "").strip()
    if text == "💎 GET PREMIUM":
        register_user(update.effective_user)
        await show_all_plans(context.bot, update.effective_chat.id)
        return

    if text == "🥵 DEMO":
        register_user(update.effective_user)
        # Reuse the exact same new-message demo flow without editing the welcome message.
        con = db()
        try:
            rows = con.execute(
                "SELECT * FROM demo_media ORDER BY sort_order,id"
            ).fetchall()
        finally:
            con.close()

        if not rows:
            await context.bot.send_message(
                update.effective_chat.id,
                bold("🥵 No demo videos are configured yet."),
                parse_mode=ParseMode.HTML,
                reply_markup=get_premium_under_demo_keyboard(),
            )
            return

        for row in rows:
            try:
                if row["media_type"] == "video":
                    await context.bot.send_video(
                        update.effective_chat.id, row["file_id"],
                        reply_markup=get_premium_under_demo_keyboard(),
                    )
                elif row["media_type"] == "photo":
                    await context.bot.send_photo(
                        update.effective_chat.id, row["file_id"],
                        reply_markup=get_premium_under_demo_keyboard(),
                    )
                else:
                    await context.bot.send_document(
                        update.effective_chat.id, row["file_id"],
                        reply_markup=get_premium_under_demo_keyboard(),
                    )
            except TelegramError:
                pass


async def admin_dashboard(q):
    if not admin_only(q): return
    con=db()
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        week = (datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
        month = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
        users=con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        new_today=con.execute("SELECT COUNT(*) c FROM users WHERE registered_at >= ?", (today,)).fetchone()["c"]
        premium=con.execute("SELECT COUNT(*) c FROM users WHERE premium_until IS NOT NULL AND premium_until> ?", (now_iso(),)).fetchone()["c"]
        pending=con.execute("SELECT COUNT(*) c FROM payments WHERE status='pending'").fetchone()["c"]
        approved=con.execute("SELECT COUNT(*) c FROM payments WHERE status='approved'").fetchone()["c"]
        rejected=con.execute("SELECT COUNT(*) c FROM payments WHERE status='rejected'").fetchone()["c"]
        revenue=con.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved'").fetchone()["s"]
        today_rev=con.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved' AND approved_at >= ?", (today,)).fetchone()["s"]
        week_rev=con.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved' AND approved_at >= ?", (week,)).fetchone()["s"]
        month_rev=con.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved' AND approved_at >= ?", (month,)).fetchone()["s"]
        best=con.execute("SELECT plan_name, COUNT(*) c FROM payments WHERE status='approved' GROUP BY plan_id ORDER BY c DESC LIMIT 1").fetchone()
    finally: con.close()
    best_name = best['plan_name'] if best else '-'
    text=(f"📊 <b>ADVANCED DASHBOARD</b>\n\n👥 Total Users: {users}\n🆕 New Today: {new_today}\n💎 Active Premium: {premium}\n\n💰 Total Revenue: ₹{float(revenue):g}\n📅 Today: ₹{float(today_rev):g}\n📆 7 Days: ₹{float(week_rev):g}\n🗓️ 30 Days: ₹{float(month_rev):g}\n\n⏳ Pending: {pending}\n✅ Approved: {approved}\n❌ Rejected: {rejected}\n🔥 Best Plan: {esc(best_name)}")
    await edit_or_send(q,text,InlineKeyboardMarkup([[style_button('🔄 REFRESH','admin:dashboard',style='primary')],[style_button('🔙 Back','admin:menu',style='primary')]]))

async def admin_payments(q):
    if not admin_only(q): return
    con=db()
    try: rows=con.execute("SELECT id,username,plan_name,amount,status,created_at FROM payments ORDER BY id DESC LIMIT 30").fetchall()
    finally: con.close()
    text="💳 <b>PAYMENT / ORDER HISTORY</b>\n\n" + ("\n".join(f"🧾 Order #{r['id']} | @{esc(r['username'] or '-')} | {esc(r['plan_name'])} | ₹{float(r['amount'] or 0):g} | {r['status'].upper()}" for r in rows) if rows else "No payments yet.")
    await edit_or_send(q,text,back_keyboard())

async def admin_users(q):
    if not admin_only(q): return
    await answer(q)
    await q.message.reply_text(bold("👥 USER MANAGEMENT\n\nSend Telegram User ID or @username to search."), parse_mode=ParseMode.HTML)

async def user_search_capture(update, context):
    if not admin_only(update): return ConversationHandler.END
    query=(update.message.text or '').strip().lstrip('@')
    con=db()
    try:
        if query.isdigit():
            rows=con.execute("SELECT * FROM users WHERE id=?",(int(query),)).fetchall()
        else:
            rows=con.execute("SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ? LIMIT 10",(f'%{query}%',f'%{query}%')).fetchall()
    finally: con.close()
    if not rows:
        await update.message.reply_text(bold("❌ User not found. Send another ID/username."),parse_mode=ParseMode.HTML)
        return USER_SEARCH
    for r in rows:
        premium=bool(r['premium_until'] and r['premium_until']>now_iso())
        text=(f"👤 <b>{esc(r['first_name'] or '-')}</b>\nID: <code>{r['id']}</code>\nUsername: @{esc(r['username'] or '-')}\nStatus: {esc(r['status'])}\nPremium: {'YES' if premium else 'NO'}\nExpiry: {esc(r['premium_until'] or '-')}")
        kb=InlineKeyboardMarkup([[style_button('💎 +30 DAYS',f'admin:user:add30:{r["id"]}',style='success'),style_button('❌ REMOVE PREMIUM',f'admin:user:remove:{r["id"]}',style='danger')],[style_button('🚫 BLOCK' if r['status']=='active' else '✅ UNBLOCK',f'admin:user:toggle:{r["id"]}',style='primary')]])
        await update.message.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=kb)
    clear_manual_state(context)
    return ConversationHandler.END

async def user_add30(q, context, user_id):
    if not admin_only(q): return
    con=db()
    try:
        row=con.execute('SELECT premium_until FROM users WHERE id=?',(user_id,)).fetchone()
        if not row: await answer(q,'User not found.',True); return
        now=datetime.now(timezone.utc)
        old=datetime.fromisoformat(row['premium_until']) if row['premium_until'] else now
        base=max(now,old)
        expiry=base+timedelta(days=30)
        con.execute('UPDATE users SET premium_until=?, status="active" WHERE id=?',(expiry.isoformat(),user_id)); con.commit()
    finally: con.close()
    log_event('Admin premium extended',f'{user_id} +30 days by {q.from_user.id}')
    await answer(q,'Premium extended')
    await q.message.reply_text(bold(f'✅ Premium extended for user <code>{user_id}</code> until {esc(expiry.isoformat())}'),parse_mode=ParseMode.HTML)

async def user_remove_premium(q, context, user_id):
    if not admin_only(q): return
    con=db()
    try: con.execute('UPDATE users SET premium_until=NULL WHERE id=?',(user_id,)); con.commit()
    finally: con.close()
    log_event('Admin premium removed',f'{user_id} by {q.from_user.id}')
    await answer(q,'Premium removed')
    await q.message.reply_text(bold(f'❌ Premium removed from <code>{user_id}</code>'),parse_mode=ParseMode.HTML)

async def user_toggle_block(q, context, user_id):
    if not admin_only(q): return
    con=db()
    try:
        row=con.execute('SELECT status FROM users WHERE id=?',(user_id,)).fetchone()
        if not row: await answer(q,'User not found.',True); return
        new='blocked' if row['status']=='active' else 'active'
        con.execute('UPDATE users SET status=? WHERE id=?',(new,user_id)); con.commit()
    finally: con.close()
    log_event('Admin user status changed',f'{user_id} -> {new} by {q.from_user.id}')
    await answer(q,new.upper())
    await q.message.reply_text(bold(f'✅ User <code>{user_id}</code> is now {new}.'),parse_mode=ParseMode.HTML)

async def security_menu(q):
    if not admin_only(q): return
    ids=sorted(current_admin_ids())
    await edit_or_send(q,'👑 <b>ADMIN MANAGEMENT</b>\n\nCurrent Admin IDs:\n'+('\n'.join(f'• <code>{i}</code>' for i in ids)),InlineKeyboardMarkup([[style_button('➕ ADD ADMIN','admin:security:add',style='success')],[style_button('➖ REMOVE ADMIN','admin:security:remove',style='danger')],[style_button('🔙 Back','admin:menu',style='primary')]]))

async def admin_add_start(q, context):
    if not admin_only(q): return
    set_manual_state(context, USER_SEARCH)
    context.user_data['admin_manage_action']='add'
    await answer(q); await q.message.reply_text(bold('➕ Send Telegram User ID to add as admin:'),parse_mode=ParseMode.HTML)

async def admin_remove_start(q, context):
    if not admin_only(q): return
    set_manual_state(context, USER_SEARCH)
    context.user_data['admin_manage_action']='remove'
    await answer(q); await q.message.reply_text(bold('➖ Send Telegram User ID to remove from admins:'),parse_mode=ParseMode.HTML)

async def admin_manage_capture(update, context):
    if not admin_only(update): return ConversationHandler.END
    raw=(update.message.text or '').strip()
    if not raw.isdigit():
        await update.message.reply_text(bold('❌ Send numeric Telegram User ID.'),parse_mode=ParseMode.HTML); return USER_SEARCH
    uid=int(raw); action=context.user_data.get('admin_manage_action')
    ids=current_admin_ids()
    if action=='add':
        ids.add(uid)
    elif action=='remove':
        if uid==ADMIN_ID:
            await update.message.reply_text(bold('❌ Primary configured admin cannot be removed.'),parse_mode=ParseMode.HTML); return ConversationHandler.END
        ids.discard(uid)
    extras=sorted(i for i in ids if i!=ADMIN_ID)
    set_setting('extra_admins',','.join(map(str,extras)))
    log_event('Admin list changed',f'{action} {uid} by {update.effective_user.id}')
    context.user_data.clear(); clear_manual_state(context)
    await update.message.reply_text(bold('✅ Admin list updated.'),parse_mode=ParseMode.HTML,reply_markup=admin_main_keyboard())
    return ConversationHandler.END

async def backup_database(q):
    if not admin_only(q): return
    con=db(); con.commit(); con.close()
    path=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite"
    import shutil
    shutil.copy2(DB_FILE,path)
    await answer(q,'Backup created')
    await q.message.reply_document(open(path,'rb'),caption='💾 Database backup')
    try: os.remove(path)
    except OSError: pass

async def send_expiry_reminders(bot):
    cutoff=datetime.now(timezone.utc)+timedelta(days=1)
    con=db()
    try:
        rows=con.execute("SELECT id,premium_until FROM users WHERE premium_until IS NOT NULL").fetchall()
    finally:
        con.close()
    for r in rows:
        try:
            exp=datetime.fromisoformat(r["premium_until"])
            if datetime.now(timezone.utc)<exp<=cutoff:
                await bot.send_message(r["id"], "⚠️ Your premium access expires within 24 hours. Renew to continue.")
        except (ValueError,TelegramError):
            pass


async def expiry_reminder_loop(app):
    # Use the asyncio event loop instead of PTB JobQueue so the bot works
    # even when python-telegram-bot was installed without the job-queue extra.
    while True:
        try:
            await send_expiry_reminders(app.bot)
        except Exception as exc:
            logger.exception("Expiry reminder task failed: %s", exc)
        await asyncio.sleep(86400)

async def post_init(app):
    init_db()
    try:
        import shutil
        backup_dir='backups'
        os.makedirs(backup_dir, exist_ok=True)
        daily=os.path.join(backup_dir, datetime.now().strftime('auto_%Y%m%d_%H%M%S.sqlite'))
        src=sqlite3.connect(DB_FILE); dst=sqlite3.connect(daily)
        try: src.backup(dst)
        finally: dst.close(); src.close()
        files=sorted([os.path.join(backup_dir,f) for f in os.listdir(backup_dir) if f.endswith('.sqlite')], key=os.path.getmtime, reverse=True)
        for old in files[7:]:
            try: os.remove(old)
            except OSError: pass
    except Exception as exc:
        log_event('Automatic backup failed', exc)
    # Schedule expiry reminders without touching app.job_queue.
    # This avoids PTB's "No JobQueue set up" warning on minimal installs.
    app.create_task(expiry_reminder_loop(app))
    log_event("Bot Started")
    try:
        me = await app.bot.get_me()
        logger.info("Bot started as @%s", me.username)
    except TelegramError as exc:
        log_event("Telegram API error", exc)


def build_app():

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Command handlers MUST be registered before the catch-all message router.
    # The previous build accidentally omitted /start and /admin, so Telegram
    # accepted updates but the bot never produced a response.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("done", demo_done))

    # All callbacks are handled centrally. Admin input flows use the
    # explicit _manual_state router below, which avoids ConversationHandler
    # entry-point/state conflicts on deployments and keeps every admin button
    # deterministic.
    app.add_handler(CallbackQueryHandler(callback))

    # Admin setup flows must accept the actual content type requested by the
    # current state (text, photo, document, video, etc.). Put this before the
    # payment screenshot handler so admin media is never misrouted.
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_router), group=0)
    # User payment screenshots.
    app.add_handler(MessageHandler(filters.PHOTO, handle_payment_photo), group=5)
    return app


if __name__ == "__main__":
    init_db()
    application = build_app()
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
