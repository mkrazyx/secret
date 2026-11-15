from html import escape

from pyrogram import Client, enums, filters

from info import *
from utils import connected_group, db, field, temp

def is_bot_admin_status(status):
    return status in (
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER,
    )

@Client.on_message(
    filters.group
    & filters.command("connect")
    & filters.user(ADMINS)
)
async def connect_group(client, message):
    bot_member = await client.get_chat_member(message.chat.id, "me")
    if not is_bot_admin_status(bot_member.status):
        return await message.reply("<b>⚠️ Make me an admin in this group first</b>")

    await db.connect_group(message.chat.id, message.chat.title)
    temp.GROUP_ACCESS[message.chat.id] = True

    await message.reply(
        "<b>✅ Group Connected\n━━━━━━━━━━━━━━\n"
        f"💬 Group: {escape(str(message.chat.title))}</b>\n"
        f"{field('🆔 ID', message.chat.id)}\n"
        "<b>🤖 The bot is now active in this group</b>"
    )

@Client.on_message(
    filters.group
    & filters.command("disconnect")
    & filters.user(ADMINS)
    & connected_group
)
async def disconnect_group(client, message):
    await db.disconnect_group(message.chat.id)
    temp.GROUP_ACCESS[message.chat.id] = False
    await message.reply(
        "<b>🔌 Group Disconnected\n━━━━━━━━━━━━━━\n"
        f"💬 Group: {escape(str(message.chat.title))}</b>\n"
        f"{field('🆔 ID', message.chat.id)}\n"
        "<b>🔇 The bot is now silent in this group</b>"
    )
