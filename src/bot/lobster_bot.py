#!/usr/bin/env python3
"""
Lobster Bot v2 - File-based message passing to master Claude session

Instead of spawning Claude processes, this bot:
1. Writes incoming messages to ~/messages/inbox/
2. Watches ~/messages/outbox/ for replies
3. Sends replies back to Telegram

The master Claude session processes inbox messages and writes to outbox.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from onboarding import is_user_onboarded, mark_user_onboarded, get_onboarding_message

# Configuration from environment
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USERS = [int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if x.strip()]

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
if not ALLOWED_USERS:
    raise ValueError("TELEGRAM_ALLOWED_USERS environment variable is required")

INBOX_DIR = Path.home() / "messages" / "inbox"
OUTBOX_DIR = Path.home() / "messages" / "outbox"
AUDIO_DIR = Path.home() / "messages" / "audio"
IMAGES_DIR = Path.home() / "messages" / "images"

# Ensure directories exist
INBOX_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# Logging
LOG_DIR = Path.home() / "lobster-workspace" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "telegram-bot.log"),
    ],
)
log = logging.getLogger("lobster")

# Global reference to the bot app and event loop for sending replies
bot_app = None
main_loop = None


class OutboxHandler(FileSystemEventHandler):
    """Watches outbox for reply files and sends them via Telegram."""

    def on_created(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith('.json'):
            # Schedule on the bot's event loop from watchdog thread
            if bot_app and main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.process_reply(event.src_path),
                    main_loop
                )

    async def process_reply(self, filepath):
        try:
            await asyncio.sleep(0.1)  # Brief delay to ensure file is written
            with open(filepath, 'r') as f:
                reply = json.load(f)

            chat_id = reply.get('chat_id')
            text = reply.get('text', '')
            buttons = reply.get('buttons')

            if chat_id and text and bot_app:
                reply_markup = build_inline_keyboard(buttons) if buttons else None
                try:
                    await bot_app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=reply_markup
                    )
                except Exception:
                    # Fallback to plain text if Markdown parsing fails
                    await bot_app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=reply_markup
                    )
                log.info(f"Sent reply to {chat_id}: {text[:50]}...")

            # Remove processed file
            os.remove(filepath)

        except Exception as e:
            log.error(f"Error processing reply {filepath}: {e}")


async def process_existing_outbox():
    """Process any outbox files that exist on startup."""
    handler = OutboxHandler()
    existing_files = list(OUTBOX_DIR.glob("*.json"))
    if existing_files:
        log.info(f"Processing {len(existing_files)} existing outbox file(s)...")
        for filepath in existing_files:
            await handler.process_reply(str(filepath))


def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USERS


def build_inline_keyboard(buttons: list) -> InlineKeyboardMarkup | None:
    """
    Build an InlineKeyboardMarkup from a buttons specification.

    Supported formats:
    1. Simple row format: [["Button 1", "Button 2"], ["Button 3"]]
       - Each string becomes a button with text=callback_data

    2. Object format: [[{"text": "Option A", "callback_data": "opt_a"}], ...]
       - Explicit text and callback_data per button

    3. Mixed format: [["Simple"], [{"text": "Complex", "callback_data": "complex"}]]
    """
    if not buttons or not isinstance(buttons, list):
        return None

    keyboard = []
    for row in buttons:
        if not isinstance(row, list):
            continue
        keyboard_row = []
        for button in row:
            if isinstance(button, str):
                # Simple format: text is also the callback_data
                keyboard_row.append(InlineKeyboardButton(text=button, callback_data=button))
            elif isinstance(button, dict):
                # Object format: explicit text and callback_data
                text = button.get('text', '')
                callback_data = button.get('callback_data', text)
                if text:
                    keyboard_row.append(InlineKeyboardButton(text=text, callback_data=callback_data))
        if keyboard_row:
            keyboard.append(keyboard_row)

    return InlineKeyboardMarkup(keyboard) if keyboard else None


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    user = query.from_user

    if not is_authorized(user.id):
        await query.answer("Unauthorized", show_alert=True)
        return

    # Acknowledge the button press
    await query.answer()

    msg_id = f"{int(time.time() * 1000)}_{query.id}"
    callback_data = query.data

    # Create message file in inbox for the button press
    msg_data = {
        "id": msg_id,
        "source": "telegram",
        "type": "callback",
        "chat_id": query.message.chat_id,
        "user_id": user.id,
        "username": user.username,
        "user_name": user.first_name,
        "text": f"[Button pressed: {callback_data}]",
        "callback_data": callback_data,
        "callback_query_id": query.id,
        "original_message_id": query.message.message_id,
        "original_message_text": query.message.text,
        "timestamp": datetime.utcnow().isoformat(),
    }

    inbox_file = INBOX_DIR / f"{msg_id}.json"
    with open(inbox_file, 'w') as f:
        json.dump(msg_data, f, indent=2)

    log.info(f"Button press from {user.first_name}: {callback_data}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    # /start triggers onboarding for new users, otherwise shows short greeting
    if not is_user_onboarded(user.id):
        await send_onboarding(update, user)
    else:
        await update.message.reply_text(
            f"👋 Hey {user.first_name}!\n\n"
            "I'm Lobster. Messages you send here go to the master Claude session.\n\n"
            "The session will process them and reply back here."
        )


async def onboarding_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /onboarding command — always shows the full onboarding message."""
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    await send_onboarding(update, user)


async def send_onboarding(update: Update, user) -> None:
    """Send the onboarding message and mark the user as onboarded."""
    message_text = get_onboarding_message(user.first_name)
    try:
        await update.message.reply_text(message_text, parse_mode="Markdown")
    except Exception:
        # Fallback to plain text if Markdown fails
        await update.message.reply_text(message_text)
    mark_user_onboarded(user.id)
    log.info(f"Sent onboarding to user {user.id} ({user.first_name})")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not is_authorized(user.id):
        log.warning(f"Unauthorized: {user.id}")
        return

    # First-message detection: send onboarding to new users
    if not is_user_onboarded(user.id):
        await send_onboarding(update, user)

    msg_id = f"{int(time.time() * 1000)}_{message.message_id}"

    # Handle voice messages
    if message.voice:
        await handle_voice_message(update, context, msg_id)
        return

    # Handle photo messages
    if message.photo:
        await handle_photo_message(update, context, msg_id)
        return

    # Handle document/file messages (including images sent as files)
    if message.document:
        await handle_document_message(update, context, msg_id)
        return

    text = message.text
    if not text:
        return

    # Create message file in inbox
    msg_data = {
        "id": msg_id,
        "source": "telegram",
        "chat_id": message.chat_id,
        "user_id": user.id,
        "username": user.username,
        "user_name": user.first_name,
        "text": text,
        "timestamp": datetime.utcnow().isoformat(),
    }

    inbox_file = INBOX_DIR / f"{msg_id}.json"
    with open(inbox_file, 'w') as f:
        json.dump(msg_data, f, indent=2)

    log.info(f"Wrote message to inbox: {msg_id}")

    # Send acknowledgment
    await message.reply_text("📨 Message received. Processing...")


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_id: str):
    """Handle voice messages: download audio and save to inbox with metadata."""
    user = update.effective_user
    message = update.message
    voice = message.voice

    try:
        # Download voice file from Telegram
        file = await context.bot.get_file(voice.file_id)
        audio_filename = f"{msg_id}.ogg"
        audio_path = AUDIO_DIR / audio_filename

        await file.download_to_drive(audio_path)
        log.info(f"Downloaded voice message to: {audio_path}")

        # Create message file in inbox with voice metadata
        msg_data = {
            "id": msg_id,
            "source": "telegram",
            "type": "voice",
            "chat_id": message.chat_id,
            "user_id": user.id,
            "username": user.username,
            "user_name": user.first_name,
            "text": "[Voice message - pending transcription]",
            "audio_file": str(audio_path),
            "audio_duration": voice.duration,
            "audio_mime_type": voice.mime_type or "audio/ogg",
            "file_id": voice.file_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

        inbox_file = INBOX_DIR / f"{msg_id}.json"
        with open(inbox_file, 'w') as f:
            json.dump(msg_data, f, indent=2)

        log.info(f"Wrote voice message to inbox: {msg_id}")
        await message.reply_text("🎤 Voice message received. Transcribing...")

    except Exception as e:
        log.error(f"Error handling voice message: {e}")
        await message.reply_text("❌ Failed to process voice message.")


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_id: str):
    """Handle photo messages: download image and save to inbox with metadata."""
    user = update.effective_user
    message = update.message

    try:
        # Get the largest photo (last in the array)
        photo = message.photo[-1]

        # Download photo file from Telegram
        file = await context.bot.get_file(photo.file_id)
        image_filename = f"{msg_id}.jpg"
        image_path = IMAGES_DIR / image_filename

        await file.download_to_drive(image_path)
        log.info(f"Downloaded photo to: {image_path}")

        # Get caption if any
        caption = message.caption or ""

        # Create message file in inbox with photo metadata
        msg_data = {
            "id": msg_id,
            "source": "telegram",
            "type": "photo",
            "chat_id": message.chat_id,
            "user_id": user.id,
            "username": user.username,
            "user_name": user.first_name,
            "text": caption if caption else "[Photo - see image_file]",
            "image_file": str(image_path),
            "image_width": photo.width,
            "image_height": photo.height,
            "file_id": photo.file_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

        inbox_file = INBOX_DIR / f"{msg_id}.json"
        with open(inbox_file, 'w') as f:
            json.dump(msg_data, f, indent=2)

        log.info(f"Wrote photo message to inbox: {msg_id}")
        await message.reply_text("📷 Image received. Processing...")

    except Exception as e:
        log.error(f"Error handling photo message: {e}")
        await message.reply_text("❌ Failed to process image.")


async def handle_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_id: str):
    """Handle document messages: download file and save to inbox with metadata."""
    user = update.effective_user
    message = update.message
    document = message.document

    try:
        # Check if it's an image sent as document
        mime_type = document.mime_type or ""
        is_image = mime_type.startswith("image/")
        original_name = document.file_name or "file"
        file_size_mb = (document.file_size or 0) / (1024 * 1024)

        log.info(f"Receiving document: name={original_name}, mime={mime_type}, size={file_size_mb:.1f}MB, file_id={document.file_id}")

        if file_size_mb > 20:
            log.warning(f"File {original_name} is {file_size_mb:.1f}MB — exceeds Telegram Bot API 20MB download limit")
            await message.reply_text(f"⚠️ File too large ({file_size_mb:.1f}MB). Telegram Bot API limit is 20MB. Please send a smaller file or upload it elsewhere and share a link.")
            return

        # Download file from Telegram
        file = await context.bot.get_file(document.file_id)

        # Determine extension and save location
        ext = Path(original_name).suffix or (".jpg" if is_image else "")

        if is_image:
            save_path = IMAGES_DIR / f"{msg_id}{ext}"
        else:
            # For non-images, save to a general files directory
            files_dir = Path.home() / "messages" / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            save_path = files_dir / f"{msg_id}{ext}"

        await file.download_to_drive(save_path)
        log.info(f"Downloaded document to: {save_path}")

        # Get caption if any
        caption = message.caption or ""

        # Create message file in inbox
        msg_data = {
            "id": msg_id,
            "source": "telegram",
            "type": "image" if is_image else "document",
            "chat_id": message.chat_id,
            "user_id": user.id,
            "username": user.username,
            "user_name": user.first_name,
            "text": caption if caption else f"[Document: {original_name}]",
            "file_path": str(save_path),
            "file_name": original_name,
            "mime_type": mime_type,
            "file_size": document.file_size,
            "file_id": document.file_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if is_image:
            msg_data["image_file"] = str(save_path)

        inbox_file = INBOX_DIR / f"{msg_id}.json"
        with open(inbox_file, 'w') as f:
            json.dump(msg_data, f, indent=2)

        log.info(f"Wrote document message to inbox: {msg_id}")
        emoji = "📷" if is_image else "📎"
        await message.reply_text(f"{emoji} File received. Processing...")

    except Exception as e:
        file_name = document.file_name or "unknown"
        file_size_mb = (document.file_size or 0) / (1024 * 1024)
        log.error(f"Error handling document: name={file_name}, size={file_size_mb:.1f}MB, error={type(e).__name__}: {e}", exc_info=True)
        await message.reply_text(f"❌ Failed to process file: {type(e).__name__}. Check logs for details.")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.error(f"Error: {context.error}", exc_info=context.error)


async def run_bot():
    global bot_app, main_loop

    log.info("Starting Lobster Bot v2 (file-based)...")
    log.info(f"Inbox: {INBOX_DIR}")
    log.info(f"Outbox: {OUTBOX_DIR}")

    # Store the event loop for the outbox watcher
    main_loop = asyncio.get_running_loop()

    # Set up outbox watcher
    observer = Observer()
    observer.schedule(OutboxHandler(), str(OUTBOX_DIR), recursive=False)
    observer.start()
    log.info("Watching outbox for replies...")

    # Create bot application
    bot_app = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("onboarding", onboarding_command))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    bot_app.add_handler(MessageHandler(filters.VOICE, handle_message))
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    bot_app.add_handler(CallbackQueryHandler(handle_callback_query))
    bot_app.add_error_handler(error_handler)

    # Initialize and start
    await bot_app.initialize()
    await bot_app.start()
    log.info("Bot is now polling...")

    # Process any existing outbox files from before startup
    await process_existing_outbox()

    try:
        await bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        # Keep running until interrupted
        while True:
            await asyncio.sleep(1)
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        observer.stop()
        observer.join()


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
