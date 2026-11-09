import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.methods import GetUpdates
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
)
from redis.exceptions import RedisError

from . import db
from .config import event, setup_logging
from .security import QUALITIES, public_url
from .telegram import (
    ACTIVE_DOWNLOADS, HELP, HISTORY, MAIN_KEYBOARD, NEW_DOWNLOAD, SETTINGS, cancel_keyboard, make_bot,
)

router = Router()
router.message.filter(F.chat.type == "private", F.from_user)
router.callback_query.filter(F.message.chat.type == "private")
COMMANDS = [
    BotCommand(command="start", description="Open Pro Downloader"),
    BotCommand(command="help", description="See how to download media"),
    BotCommand(command="settings", description="Set your default quality"),
    BotCommand(command="history", description="View recent downloads"),
    BotCommand(command="status", description="View or cancel active downloads"),
    BotCommand(command="support", description="Get help"),
]
PLATFORMS = {
    "YouTube": ("youtube.com", "youtu.be"), "TikTok": ("tiktok.com",),
    "Instagram": ("instagram.com",), "Facebook": ("facebook.com", "fb.watch"),
    "X": ("x.com", "twitter.com"), "Threads": ("threads.net", "threads.com"),
    "Pinterest": ("pinterest.com", "pin.it"), "Reddit": ("reddit.com", "redd.it"),
    "Snapchat": ("snapchat.com",), "Vimeo": ("vimeo.com",), "Dailymotion": ("dailymotion.com", "dai.ly"),
    "Twitch": ("twitch.tv",), "SoundCloud": ("soundcloud.com",), "Tumblr": ("tumblr.com",),
    "Bilibili": ("bilibili.com", "b23.tv"), "Douyin": ("douyin.com",),
    "Likee": ("likee.video",), "LinkedIn": ("linkedin.com",),
}


def platform_for(url):
    host = urlsplit(url).hostname
    for platform, domains in PLATFORMS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
    return "Other website"


def keyboard(prefix, default):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✓ " if value == default else "") + label,
                              callback_data=f"{prefix}:{value}")]
        for value, label in QUALITIES.items()
    ])


@router.message(CommandStart())
async def start(message, pool):
    await db.register(pool, message.from_user.id)
    await message.answer("Welcome to Pro Downloader. Paste one public media link and pick a quality or format.",
                         reply_markup=MAIN_KEYBOARD)


@router.message(F.text == NEW_DOWNLOAD)
async def new_download(message):
    await message.answer("Paste your next public media link here.", reply_markup=MAIN_KEYBOARD)


@router.message(Command("help"))
@router.message(F.text == HELP)
async def help_command(message):
    await message.answer("Send one public media link. Choose Original / best, a video resolution, MP3, "
                         "or All media / playlist for supported multi-item posts. Use /status to cancel "
                         "a download before delivery and /history for recent results.")


@router.message(Command("support"))
async def support(message, pool):
    contact = await pool.fetchval("SELECT value FROM bot_config WHERE key='support'")
    contact = contact or os.getenv("SUPPORT_CONTACT", "")
    await message.answer(f"Support\n{contact}" if contact else
                         "Use /status for active jobs or /help for download instructions.")


@router.message(Command("settings"))
@router.message(F.text == SETTINGS)
async def settings(message, pool):
    await db.register(pool, message.from_user.id)
    quality = await pool.fetchval("SELECT quality FROM users WHERE id=$1", message.from_user.id)
    await message.answer("Choose the quality to highlight for new downloads.", reply_markup=keyboard("set", quality))


@router.callback_query(F.data.startswith("set:"))
async def set_default(callback, pool):
    quality = callback.data.split(":", 1)[1]
    if quality not in QUALITIES:
        await callback.answer("Unknown quality.")
        return
    await db.register(pool, callback.from_user.id)
    await pool.execute("UPDATE users SET quality=$2 WHERE id=$1", callback.from_user.id, quality)
    await callback.answer("Preference saved")
    if callback.message.reply_markup != keyboard("set", quality):
        await callback.message.edit_reply_markup(reply_markup=keyboard("set", quality))


@router.message(Command("history"))
@router.message(F.text == HISTORY)
async def history(message, pool):
    rows = await pool.fetch(
        """SELECT platform,state,created_at FROM jobs WHERE user_id=$1
        AND state IN ('done','failed','cancelled') ORDER BY created_at DESC LIMIT 10""", message.from_user.id)
    lines = [f"{row['created_at']:%d %b} · {row['platform']} · {row['state'].title()}" for row in rows]
    await message.answer("Recent downloads\n" + "\n".join(lines) if lines else "Your download history is empty.")


@router.message(Command("status"))
@router.message(F.text == ACTIVE_DOWNLOADS)
async def status(message, pool):
    rows = await pool.fetch(
        """SELECT id,platform,state FROM jobs WHERE user_id=$1 AND state IN ('queued','running','delivering')
        ORDER BY created_at""", message.from_user.id)
    if not rows:
        await message.answer("You have no active downloads.")
    for row in rows:
        await message.answer(f"{row['platform']} · {row['state'].title()}",
                             reply_markup=cancel_keyboard(row["id"]) if row["state"] != "delivering" else None)


@router.message(Command("admin"))
async def admin(message, pool, redis):
    if not await pool.fetchval("SELECT 1 FROM admins WHERE user_id=$1", message.from_user.id):
        return
    users = await pool.fetchval("SELECT count(*) FROM users")
    queue = await pool.fetch(
        """SELECT state,count(*) AS total FROM jobs WHERE state!='choice'
        AND (finished_at IS NULL OR finished_at>now()-interval '24 hours') GROUP BY state ORDER BY state""")
    failures = await pool.fetch(
        """SELECT platform,COALESCE(error,'unknown') AS error FROM jobs
        WHERE state='failed' ORDER BY finished_at DESC LIMIT 5""")
    top = await pool.fetch(
        """SELECT platform, state, sum(total) AS total FROM platform_stats
        WHERE day>=CURRENT_DATE-6 AND state IN ('done','failed')
        GROUP BY platform,state ORDER BY sum(total) DESC LIMIT 8""")
    try:
        workers = "Online" if await redis.get("prodownloader:worker-heartbeat") else "No recent heartbeat"
        redis_state = "Online"
    except RedisError:
        workers = "Unknown"
        redis_state = "Unavailable"
    lines = ["Pro Downloader admin", f"Users {users}", f"Workers {workers}", f"Queue signals {redis_state}",
             "Jobs now and in the last 24 hours"]
    lines += [f"{row['state'].title()} {row['total']}" for row in queue]
    lines += ["Latest failures", *[f"{row['platform']} · {row['error']}" for row in failures]]
    lines += ["Platform activity, last 7 days", *[f"{row['platform']} · {row['state']} {row['total']}" for row in top]]
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("cancel:"))
async def cancel(callback, pool):
    try:
        job_id = UUID(hex=callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid download.")
        return
    cancelled = await db.cancel(pool, callback.from_user.id, job_id)
    await callback.answer("Cancelled" if cancelled else "This job cannot be cancelled now.")
    if cancelled:
        await callback.message.edit_text("Download cancelled.")
        original = await pool.fetchval("SELECT message_id FROM jobs WHERE id=$1", job_id)
        if original and original != callback.message.message_id:
            try:
                await callback.bot.edit_message_text("Download cancelled.", chat_id=callback.from_user.id,
                                                     message_id=original)
            except TelegramBadRequest:
                pass


@router.callback_query(F.data.startswith("get:"))
async def select_quality(callback, pool, redis):
    _, identifier, quality = callback.data.split(":", 2)
    try:
        job_id = UUID(hex=identifier)
    except ValueError:
        await callback.answer("Invalid download.")
        return
    if quality not in QUALITIES:
        await callback.answer("Unknown quality.")
        return
    state = await db.enqueue(pool, callback.from_user.id, job_id, quality, callback.message.message_id)
    if state != "queued":
        await callback.answer({"expired": "This selection has expired. Send the link again.",
                               "duplicate": "This download is already active.",
                               "full": "The queue is full. Try again shortly."}[state], show_alert=True)
        return
    try:
        await callback.answer("Added to queue")
        await callback.message.edit_text("Queued for download…", reply_markup=cancel_keyboard(job_id))
    finally:
        await db.wake(redis)


@router.message(F.text)
async def link(message, pool, redis):
    links = re.findall(r"https?://[^\s<>]+", message.text, re.I)
    if len(links) != 1:
        await message.answer("Send one public media link.", reply_markup=MAIN_KEYBOARD)
        return
    try:
        url = public_url(links[0])
    except ValueError:
        await message.answer("Use a valid public HTTP or HTTPS media link.")
        return
    user = message.from_user.id
    try:
        key = f"prodownloader:rate:{user}"
        async with redis.pipeline(transaction=True) as pipe:
            count, _ = await pipe.incr(key).expire(key, 60, nx=True).execute()
        if count > 8:
            await message.answer("Too many links. Try again in a minute.")
            return
    except RedisError:
        await message.answer("Downloads are temporarily busy. Try again shortly.")
        return
    platform = platform_for(url)
    choice = await db.create_choice(pool, user, url, platform)
    if choice is None:
        await message.answer("You have three active downloads. Use /status to manage them.")
        return
    await message.answer(f"{platform}\nChoose a quality or format.",
                         reply_markup=keyboard(f"get:{choice['id'].hex}", choice["quality"]))


@router.errors()
async def on_error(error):
    event("bot_handler_error", error=type(error.exception).__name__)
    message = error.update.message
    if message and message.chat.type == "private":
        try:
            await message.answer("Something went wrong. Please try again.")
        except Exception:
            pass


async def configure(bot):
    profile = await bot.get_me()
    expected = os.getenv("BOT_USERNAME", "prodownloadersbot").removeprefix("@")
    if (profile.username or "").lower() != expected.lower():
        raise RuntimeError("BOT_TOKEN belongs to a different bot")
    await bot.set_my_commands(COMMANDS)
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await bot.set_my_description("Send a public media link and choose a quality, MP3, or supported album download.")
    await bot.set_my_short_description("Download public videos, audio and photos.")
    await bot.delete_webhook(drop_pending_updates=False)
    event("telegram_ready", username=profile.username, commands=len(await bot.get_my_commands()))


async def main():
    setup_logging()
    pool, redis = await db.connect()
    try:
        async with make_bot() as bot:
            @bot.session.middleware()
            async def polling_health(make_request, session_bot, method):
                result = await make_request(session_bot, method)
                if isinstance(method, GetUpdates):
                    Path("/tmp/prodownloader-polling").touch()
                return result

            await configure(bot)
            dispatcher = Dispatcher()
            dispatcher.include_router(router)
            await dispatcher.start_polling(bot, pool=pool, redis=redis, close_bot_session=False,
                                           tasks_concurrency_limit=32)
    finally:
        await pool.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
