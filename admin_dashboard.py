"""Admin dashboard for Telegram bot with inline keyboard navigation."""

import asyncio
from datetime import datetime
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import settings, ADMIN_ID, RATE_LIMIT_PER_MINUTE, RATE_LIMIT_PER_HOUR, MAX_DOWNLOADS_PER_USER_PER_DAY, ALLOWED_USERS
from logging_config import get_logger
from database import get_database
from download_queue import get_download_queue

logger = get_logger("admin_dashboard")

# In-memory state for broadcast
_broadcast_state: Dict[int, str] = {}


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        return
    await show_admin_dashboard(update, context)


async def show_admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = [
        [InlineKeyboardButton("📊 آمار", callback_data="admin_cb_stats")],
        [InlineKeyboardButton("👥 کاربران", callback_data="admin_cb_users")],
        [InlineKeyboardButton("📋 لاگ‌ها", callback_data="admin_cb_logs")],
        [InlineKeyboardButton("📦 صف دانلود", callback_data="admin_cb_queue")],
        [InlineKeyboardButton("📢 پیام همگانی", callback_data="admin_cb_broadcast")],
        [InlineKeyboardButton("⚙️ تنظیمات", callback_data="admin_cb_settings")],
    ]
    msg = "🛠️ **داشبورد مدیریت ربات**\n\nلطفاً یک گزینه را انتخاب کنید:"
    await update.effective_message.reply_text(
        msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
    )


# ── Stats ──────────────────────────────────────────────────────────
async def admin_cb_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db = await get_database()
    if not db:
        users_str = ", ".join(str(u) for u in ALLOWED_USERS) if ALLOWED_USERS else "همه (محدودیتی نیست)"
        text = (
            "📊 **آمار کلی ربات** (حالت ساده)\n\n"
            f"👥 کاربران مجاز: `{users_str}`\n"
            f"📦 صف: ناموجود (Redis)\n"
            f"📋 لاگ: ناموجود (PostgreSQL)\n\n"
            "_برای آمار کامل، PostgreSQL را تنظیم کنید._"
        )
        kb = [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_cb_main_menu")]]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return
    stats = await db.get_bot_stats()
    queue = await get_download_queue()
    qs = await queue.get_queue_stats() if queue else {"pending": 0, "processing": 0}
    text = (
        "📊 **آمار کلی ربات**\n\n"
        f"👤 کل کاربران: `{stats['total_users']}`\n"
        f"📥 کل دانلودها: `{stats['total_downloads']}`\n"
        f"📅 دانلود امروز: `{stats['downloads_today']}`\n"
        f"⏰ دانلود ساعت اخیر: `{stats['downloads_this_hour']}`\n"
        f"📦 صف انتظار: `{qs['pending']}`\n"
        f"⚙️ در حال پردازش: `{qs['processing']}`"
    )
    kb = [
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data="admin_cb_stats")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_cb_main_menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


# ── Users ──────────────────────────────────────────────────────────
async def admin_cb_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db = await get_database()
    if not db:
        if ALLOWED_USERS:
            lines = ["👥 **کاربران مجاز (از ALLOWED_USERS):**\n"]
            for uid in ALLOWED_USERS:
                lines.append(f"✅ ID: `{uid}`")
            lines.append(f"\n_برای مدیریت کامل، PostgreSQL را تنظیم کنید._")
        else:
            lines = ["👥 **کاربران مجاز:**\n\nℹ️ `ALLOWED_USERS` خالی است — همه کاربران مجازند.\n\n_برای محدود کردن، IDها را در Railway اضافه کنید._"]
        kb = [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_cb_main_menu")]]
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return
    users = await db.get_allowed_users()
    if not users:
        text = "👥 **کاربران مجاز**\n\nهیچ کاربری ثبت نشده."
    else:
        lines = ["👥 **کاربران مجاز**\n"]
        for u in users:
            today = await db.get_user_downloads_today(u["user_id"])
            name = u["display_name"] or u["username"] or str(u["user_id"])
            status = "✅" if u["is_active"] else "❌"
            lines.append(
                f"{status} {name} (ID: `{u['user_id']}`)\n"
                f"   📥 {u['total_downloads']} | امروز: {today} | سقف روزانه: {u['daily_limit']}\n"
            )
        text = "\n".join(lines)
    kb = [
        [InlineKeyboardButton("➕ افزودن کاربر", callback_data="admin_cb_adduser_prompt")],
        [InlineKeyboardButton("🔄 مسدود/فعال", callback_data="admin_cb_toggle_prompt")],
        [InlineKeyboardButton("📊 جزئیات کاربر", callback_data="admin_cb_detail_prompt")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_cb_main_menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


# ── Logs ───────────────────────────────────────────────────────────
async def admin_cb_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db = await get_database()
    if not db:
        await q.edit_message_text("📋 **لاگ دانلودها**\n\n⚠️ برای نمایش لاگ، PostgreSQL را تنظیم کنید.")
        return
    logs = await db.get_download_history(limit=10)
    if not logs:
        text = "📋 **آخرین دانلودها**\n\nهیچ لاگی یافت نشد."
    else:
        lines = ["📋 **آخرین دانلودها**\n"]
        for log in logs:
            t = log.get("title", "N/A")
            p = log.get("platform", "?")
            s = log.get("status", "?")
            ts = log.get("created_at", datetime.now())
            if hasattr(ts, "strftime"):
                ts = ts.strftime("%m-%d %H:%M")
            lines.append(f"• {t[:40]} | {p} | {s} | {ts}")
        text = "\n".join(lines)
    kb = [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_cb_main_menu")]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


# ── Queue ──────────────────────────────────────────────────────────
async def admin_cb_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    queue = await get_download_queue()
    if not queue:
        await q.edit_message_text("❌ Redis فعال نیست – صف دانلود غیرفعال است.")
        return
    stats = await queue.get_queue_stats()
    pending = await queue.get_pending_tasks(limit=5)
    processing = await queue.get_processing_tasks()
    lines = [
        "📦 **وضعیت صف دانلود**\n",
        f"⏳ در انتظار: `{stats['pending']}`",
        f"⚙️ در حال پردازش: `{stats['processing']}`\n",
    ]
    if pending:
        lines.append("**اولویت‌های انتظار:**")
        for t in pending:
            lines.append(f"  • {t.title[:40]} (user: {t.user_id})")
    if processing:
        lines.append("\n**در حال پردازش:**")
        for t in processing:
            lines.append(f"  • {t.title[:40]} (user: {t.user_id})")
    text = "\n".join(lines)
    kb = [
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data="admin_cb_queue")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_cb_main_menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


# ── Broadcast ──────────────────────────────────────────────────────
async def admin_cb_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    _broadcast_state[q.from_user.id] = "pending"
    await q.edit_message_text(
        "📢 **پیام همگانی**\n\n"
        "متن پیام را تایپ کنید.\n"
        "برای لغو: /cancel",
        parse_mode="Markdown",
    )


async def handle_admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if this message was consumed as a broadcast."""
    uid = update.effective_user.id
    if not is_admin(uid) or _broadcast_state.get(uid) != "pending":
        return False
    del _broadcast_state[uid]
    text = update.message.text
    db = await get_database()
    if not db:
        await update.message.reply_text("📢 **پیام همگانی**\n\n⚠️ برای ارسال همگانی، PostgreSQL را تنظیم کنید.")
        return True
    user_ids = await db.get_all_user_ids()
    sent = failed = 0
    for tid in user_ids:
        if tid == uid:
            continue
        try:
            await context.bot.send_message(chat_id=tid, text=text, parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning("broadcast_fail", user_id=tid, error=str(e))
            failed += 1
    await update.message.reply_text(f"✅ ارسال شد. موفق: {sent} | ناموفق: {failed}")
    await show_admin_dashboard(update, context)
    return True


# ── Settings ───────────────────────────────────────────────────────
async def admin_cb_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    text = (
        "⚙️ **تنظیمات ربات**\n\n"
        f"🔑 Admin ID: `{ADMIN_ID}`\n"
        f"📊 Rate Limit /min: `{RATE_LIMIT_PER_MINUTE}`\n"
        f"📈 Rate Limit /hour: `{RATE_LIMIT_PER_HOUR}`\n"
        f"⬇️ سقف روزانه (پیش‌فرض): `{MAX_DOWNLOADS_PER_USER_PER_DAY}`\n"
        f"🕸️ Webhook: `{settings.webhook_mode}`\n"
        f"💾 Max Storage: `{settings.max_storage_mb} MB`\n"
        f"⏳ Max Video: `{settings.max_video_duration_sec}s`\n"
        f"📦 Compress Target: `{settings.video_compression_target_mb} MB`"
    )
    kb = [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_cb_main_menu")]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


# ── Prompts (inline → follow-up command) ──────────────────────────
async def _prompt(update: Update, text: str) -> None:
    await update.callback_query.edit_message_text(text, parse_mode="Markdown")


async def admin_cb_adduser_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _prompt(update, "➕ **افزودن کاربر**\n\n`/adduser <user_id> [daily_limit] [نام]`\nیا به پیام کاربر ریپلای کنید و `/adduser` بزنید.")


async def admin_cb_toggle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _prompt(update, "🔄 **مسدود/فعال کردن**\n\n`/toggleuser <user_id>`")


async def admin_cb_detail_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _prompt(update, "📊 **جزئیات کاربر**\n\n`/userinfo <user_id>`")


# ── Main menu callback ─────────────────────────────────────────────
async def admin_cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_admin_dashboard(update, context)


# ── Callback dispatcher ────────────────────────────────────────────
_ADMIN_CALLBACKS = {
    "admin_cb_stats": admin_cb_stats,
    "admin_cb_users": admin_cb_users,
    "admin_cb_logs": admin_cb_logs,
    "admin_cb_queue": admin_cb_queue,
    "admin_cb_broadcast": admin_cb_broadcast,
    "admin_cb_settings": admin_cb_settings,
    "admin_cb_main_menu": admin_cb_main_menu,
    "admin_cb_adduser_prompt": admin_cb_adduser_prompt,
    "admin_cb_toggle_prompt": admin_cb_toggle_prompt,
    "admin_cb_detail_prompt": admin_cb_detail_prompt,
}


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if handled."""
    data = update.callback_query.data
    handler = _ADMIN_CALLBACKS.get(data)
    if not handler:
        return False
    if not is_admin(update.callback_query.from_user.id):
        await update.callback_query.answer("❌ دسترسی ندارید.", show_alert=True)
        return True
    await handler(update, context)
    return True


# ═══════════════════════════════════════════════════════════════════
# Admin commands (text-based)
# ═══════════════════════════════════════════════════════════════════

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /adduser <user_id> [daily_limit] [display_name]  or  reply to user"""
    if not is_admin(update.effective_user.id):
        return
    db = await get_database()
    if not db:
        await update.message.reply_text("➕ **افزودن کاربر**\n\n⚠️ برای مدیریت کاربران از طریق ربات، PostgreSQL را تنظیم کنید.\n\n💡 یا ID کاربر را به `ALLOWED_USERS` در Railway اضافه کنید.")
        return

    target_id = None
    limit = MAX_DOWNLOADS_PER_USER_PER_DAY
    name = ""

    if context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ فرمت: `/adduser 123456789 100 Taha`", parse_mode="Markdown")
            return
        if len(context.args) > 1:
            try:
                limit = int(context.args[1])
            except ValueError:
                pass
        if len(context.args) > 2:
            name = " ".join(context.args[2:])
    elif update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
        name = update.message.reply_to_message.from_user.full_name
    else:
        await update.message.reply_text(
            "❌ آیدی کاربر را وارد کنید یا ریپلای کنید.\n`/adduser 123456789 100 Taha`",
            parse_mode="Markdown",
        )
        return

    if not target_id:
        return

    try:
        chat = await context.bot.get_chat(target_id)
        username = chat.username or ""
        if not name:
            name = chat.full_name
    except Exception:
        username = ""

    ok = await db.add_allowed_user(target_id, username, name, update.effective_user.id, limit)
    action = "اضافه شد ✅" if ok else "به‌روزرسانی شد 🔄"
    await update.message.reply_text(
        f"{action}\n👤 {name} (`{target_id}`)\n⚙️ سقف روزانه: `{limit}`",
        parse_mode="Markdown",
    )


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /removeuser <user_id>"""
    if not is_admin(update.effective_user.id):
        return
    db = await get_database()
    if not db:
        await update.message.reply_text("➖ **حذف کاربر**\n\n⚠️ برای حذف کاربر، PostgreSQL را تنظیم کنید.\n\n💡 یا ID کاربر را از `ALLOWED_USERS` در Railway حذف کنید.")
        return
    if not context.args:
        await update.message.reply_text("❌ `/removeuser 123456789`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ آیدی نامعتبر.")
        return
    ok = await db.remove_allowed_user(uid)
    if ok:
        await update.message.reply_text(f"✅ کاربر `{uid}` حذف شد.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"ℹ️ کاربر `{uid}` یافت نشد.", parse_mode="Markdown")


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /listusers"""
    if not is_admin(update.effective_user.id):
        return
    db = await get_database()
    if not db:
        if ALLOWED_USERS:
            lines = ["👥 **کاربران مجاز (از Railway):**\n"]
            for uid in ALLOWED_USERS:
                lines.append(f"✅ ID: `{uid}`")
        else:
            lines = ["👥 **کاربران مجاز:**\n\nℹ️ `ALLOWED_USERS` خالی است — همه کاربران مجازند."]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return
    users = await db.get_allowed_users()
    if not users:
        await update.message.reply_text("👥 هیچ کاربری ثبت نشده.")
        return
    lines = ["👥 **کاربران مجاز:**\n"]
    for u in users:
        n = u["display_name"] or u["username"] or str(u["user_id"])
        s = "✅" if u["is_active"] else "❌"
        lines.append(f"{s} {n} (`{u['user_id']}`) – 📥 {u['total_downloads']} | سقف: {u['daily_limit']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_toggleuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /toggleuser <user_id>"""
    if not is_admin(update.effective_user.id):
        return
    db = await get_database()
    if not db:
        await update.message.reply_text("🔄 **مسدود/فعال کردن**\n\n⚠️ برای تغییر وضعیت کاربر، PostgreSQL را تنظیم کنید.")
        return
    if not context.args:
        await update.message.reply_text("❌ `/toggleuser 123456789`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ آیدی نامعتبر.")
        return
    info = await db.get_user_stats(uid)
    if not info:
        await update.message.reply_text(f"ℹ️ کاربر `{uid}` یافت نشد.", parse_mode="Markdown")
        return
    new_status = not info["is_active"]
    await db.set_user_active(uid, new_status)
    label = "فعال ✅" if new_status else "مسدود ❌"
    n = info["display_name"] or info["username"] or str(uid)
    await update.message.reply_text(f"{n} (`{uid}`) → {label}", parse_mode="Markdown")


async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /userinfo <user_id>"""
    if not is_admin(update.effective_user.id):
        return
    db = await get_database()
    if not db:
        await update.message.reply_text("📊 **جزئیات کاربر**\n\n⚠️ برای نمایش جزئیات، PostgreSQL را تنظیم کنید.")
        return
    if not context.args:
        await update.message.reply_text("❌ `/userinfo 123456789`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ آیدی نامعتبر.")
        return
    info = await db.get_user_stats(uid)
    if not info:
        await update.message.reply_text(f"ℹ️ کاربر `{uid}` یافت نشد.", parse_mode="Markdown")
        return
    n = info["display_name"] or info["username"] or str(uid)
    last = info.get("last_download_at")
    last_str = last.strftime("%Y-%m-%d %H:%M") if last else "—"
    created = info.get("created_at")
    created_str = created.strftime("%Y-%m-%d %H:%M") if created else "—"
    status = "✅ فعال" if info["is_active"] else "❌ مسدود"
    text = (
        f"📊 **جزئیات کاربر: {n}**\n\n"
        f"🆔 ID: `{uid}`\n"
        f"👤 یوزرنیم: @{info.get('username', '')}\n"
        f"📌 وضعیت: {status}\n"
        f"📥 کل دانلودها: `{info['total_downloads']}`\n"
        f"📅 امروز: `{info['downloads_today']}`\n"
        f"⚙️ سقف روزانه: `{info['daily_limit']}`\n"
        f"🕐 آخرین دانلود: `{last_str}`\n"
        f"📋 تاریخ ثبت: `{created_str}`"
    )
    kb = [
        [InlineKeyboardButton("🔄 مسدود/فعال", callback_data="admin_cb_toggle_prompt")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_cb_users")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
