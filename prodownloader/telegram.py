import os

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from .config import event

ERRORS = {
    "protected": "This media is protected and cannot be downloaded.",
    "inaccessible": "This media is unavailable without access to the original post.",
    "temporary": "The source is temporarily unavailable. Try again later.",
    "unsupported": "No downloadable public media was found at this link.",
    "quality": "That format or quality is unavailable. Try Original / best.",
    "too_large": "The media exceeds the download limit. Try a lower quality or a single post.",
    "delivery": "Telegram could not deliver the files. Check the chat before trying again.",
    "delivery_interrupted": "Delivery was interrupted. Check the chat for any files already sent.",
    "worker_lost": "The download was interrupted. Please try again.",
}


NEW_DOWNLOAD = "⬇️ New download"
HISTORY = "History"
ACTIVE_DOWNLOADS = "Active downloads"
SETTINGS = "⚙️ Settings"
HELP = "Help"
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=NEW_DOWNLOAD)],
        [KeyboardButton(text=HISTORY), KeyboardButton(text=ACTIVE_DOWNLOADS)],
        [KeyboardButton(text=SETTINGS), KeyboardButton(text=HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Paste a public media link",
)


def make_bot():
    return Bot(os.environ["BOT_TOKEN"], session=AiohttpSession(timeout=90))


def cancel_keyboard(job_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Cancel", callback_data=f"cancel:{job_id.hex}")]
    ])


async def update(bot, job, text, cancel=False):
    try:
        if job["message_id"]:
            await bot.edit_message_text(text=text, chat_id=job["user_id"], message_id=job["message_id"],
                                        reply_markup=cancel_keyboard(job["id"]) if cancel else None)
        else:
            await bot.send_message(job["user_id"], text)
    except TelegramAPIError as error:
        event("progress_update_failed", job=str(job["id"]), error=type(error).__name__)
