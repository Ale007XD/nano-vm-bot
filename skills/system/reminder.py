"""
Example system skill: Reminder
Simple reminder functionality
"""

import asyncio
import re
from datetime import datetime, timedelta

SKILL_NAME = "reminder"
SKILL_DESCRIPTION = "Set simple reminders"
SKILL_CATEGORY = "productivity"
SKILL_VERSION = "1.0.0"
SKILL_AUTHOR = "Alex-Nano-Bot System"
SKILL_COMMANDS = ["/remind"]

# In-memory reminders storage (in production, use database)
reminders = {}


async def handle_command(command: str, args: list, message, bot):
    """Handle reminder commands"""
    if command == "remind":
        return await set_reminder(message, args, bot)
    return None


async def set_reminder(message, args, bot):
    """Set a reminder"""
    if not args:
        await message.reply(
            "⏰ <b>Reminder</b>\n\n"
            "Usage:\n"
            "<code>/remind 5m Call mom</code> - 5 minutes\n"
            "<code>/remind 1h Meeting</code> - 1 hour\n"
            "<code>/remind 30s Test</code> - 30 seconds\n\n"
            "Units: s (seconds), m (minutes), h (hours), d (days)"
        )
        return
    
    # Parse time and message
    time_str = args[0]
    reminder_text = " ".join(args[1:]) if len(args) > 1 else "Reminder!"
    
    try:
        seconds = parse_time(time_str)
        user_id = message.from_user.id
        chat_id = message.chat.id
        
        # Schedule reminder
        asyncio.create_task(
            send_reminder_later(
                bot, chat_id, user_id, seconds, reminder_text
            )
        )
        
        # Calculate reminder time
        reminder_time = datetime.now() + timedelta(seconds=seconds)
        time_str_formatted = reminder_time.strftime("%H:%M:%S")
        
        await message.reply(
            f"✅ <b>Reminder Set!</b>\n\n"
            f"⏰ Time: <b>{time_str_formatted}</b>\n"
            f"💬 Message: <i>{reminder_text}</i>\n\n"
            f"I'll remind you in {format_duration(seconds)}"
        )
        
    except ValueError as e:
        await message.reply(
            f"❌ <b>Invalid time format</b>\n\n"
            f"{str(e)}\n\n"
            f"Use: 5m, 1h, 30s, 2d"
        )


def parse_time(time_str: str) -> int:
    """Parse time string to seconds"""
    pattern = r'^(\d+)([smhd])$'
    match = re.match(pattern, time_str.lower())
    
    if not match:
        raise ValueError(f"Invalid time format: {time_str}")
    
    amount = int(match.group(1))
    unit = match.group(2)
    
    multipliers = {
        's': 1,
        'm': 60,
        'h': 3600,
        'd': 86400
    }
    
    return amount * multipliers[unit]


def format_duration(seconds: int) -> str:
    """Format seconds to human readable string"""
    if seconds < 60:
        return f"{seconds} seconds"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes > 1 else ''}"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours > 1 else ''}"
    else:
        days = seconds // 86400
        return f"{days} day{'s' if days > 1 else ''}"


async def send_reminder_later(bot, chat_id, user_id, delay, text):
    """Send reminder after delay"""
    await asyncio.sleep(delay)
    
    try:
        await bot.send_message(
            chat_id,
            f"⏰ <b>Reminder!</b>\n\n"
            f"💬 {text}\n\n"
            f"<i>Set by you</i>",
            reply_to_message_id=None
        )
    except Exception as e:
        print(f"Failed to send reminder: {e}")


def setup_handlers():
    """Setup command handlers for this skill"""
    return {
        "remind": handle_command
    }
