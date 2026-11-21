import asyncio
import logging
import os
import re
import secrets
import sys
from html import escape
from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait, MessageNotModified, MessageTooLong, PeerIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from info import *
from utils import broadcast_messages, connected_group, db, field, get_settings, is_group_connected, save_group_settings, user_link
from plugins.index import Media, count_bad_files, delete_bad_files, normalize, unpack_new_file_id
BROADCAST_CANCEL = set()

def get_settings_keyboard(settings: dict):
    spell = bool(settings.get("spell_check", SPELL_CHECK_REPLY))
    short = bool(settings.get("is_shortlink", IS_SHORTLINK))

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Spell Check",
                callback_data=f"setgs#spell_check#{spell}"
            ),
            InlineKeyboardButton(
                "✔ On" if spell else "✖ Off",
                callback_data=f"setgs#spell_check#{spell}"
            )
        ],
        [
            InlineKeyboardButton(
                "ShortLink",
                callback_data=f"setgs#is_shortlink#{short}"
            ),
            InlineKeyboardButton(
                "✔ On" if short else "✖ Off",
                callback_data=f"setgs#is_shortlink#{short}"
            )
        ],
    ])

@Client.on_message(
    filters.command("broadcast") & filters.user(ADMINS) & filters.reply & connected_group
)
async def broadcast_users(bot, message):
    b_msg = message.reply_to_message
    total_users = await db.total_users_count()
    admin_id = message.from_user.id

    BROADCAST_CANCEL.discard(("users", admin_id))

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "❌ Cancel Broadcast",
            callback_data=f"cancel_broadcast#{admin_id}"
        )
    ]])

    sts = await message.reply_text(
        f"<b>📢 Broadcast Started\n\n"
        f"👥 Total Users: {total_users:,}</b>",
        reply_markup=markup,
    )

    success = 0
    failed = 0
    processed = 0

    users = await db.get_all_users()

    async for user in users:
        if ("users", admin_id) in BROADCAST_CANCEL:
            break

        try:
            sent, _ = await broadcast_messages(int(user["id"]), b_msg)
            if sent:
                success += 1
            else:
                failed += 1
        except Exception:
            logging.exception("Broadcast failed for %s", user.get("id"))
            failed += 1

        processed += 1

        if processed % 20 == 0:
            await edit_text_safely(
                sts,
                f"<b>📡 Broadcasting...\n\n"
                f"👥 Total: {total_users:,}\n"
                f"📥 Processed: {processed:,}\n"
                f"✅ Sent: {success:,}\n"
                f"❌ Failed: {failed:,}</b>",
                reply_markup=markup,
            )

        await asyncio.sleep(0.1)

    cancelled = ("users", admin_id) in BROADCAST_CANCEL
    BROADCAST_CANCEL.discard(("users", admin_id))

    await edit_text_safely(
        sts,
        f"<b>{'🛑 Broadcast Cancelled' if cancelled else '✅ Broadcast Completed'}\n\n"
        f"👥 Total: {total_users:,}\n"
        f"📥 Processed: {processed:,}\n"
        f"✅ Sent: {success:,}\n"
        f"❌ Failed: {failed:,}</b>",
    )

@Client.on_callback_query(filters.regex(r"^cancel_broadcast#"))
async def cancel_broadcast(bot, query):
    try:
        _, admin_id = query.data.split("#", 1)
        admin_id = int(admin_id)
    except ValueError:
        return await query.answer(
            "❌ Invalid request",
            show_alert=True
        )

    if query.from_user.id != admin_id:
        return await query.answer(
            "⛔ This is not your broadcast",
            show_alert=True
        )

    BROADCAST_CANCEL.add(("users", admin_id))

    await query.answer("🛑 Cancelling broadcast...")

DB_STORAGE_LIMIT = 512 * 1024 * 1024

@Client.on_message(filters.command("stats") & filters.incoming & filters.user(ADMINS) & connected_group)
async def get_stats(bot, message):
    files = await Media.count_documents()
    users = await db.total_users_count()
    chats = await db.total_chat_count()
    used = await db.get_db_size()
    free_percent = max(0, DB_STORAGE_LIMIT - used) * 100 / DB_STORAGE_LIMIT
    await message.reply(
        f"<b>🗃 Fɪʟᴇs: {files:,} | 👤 Usᴇʀs: {users:,}\n"
        f"🗨 Cʜᴀᴛs: {chats:,} | 💾 Sᴛᴏʀᴀɢᴇ: {free_percent:.0f}% Fʀᴇᴇ</b>"
    )

@Client.on_message(filters.command("ban") & filters.user(ADMINS) & connected_group)
async def ban_user(bot, message):
    if len(message.command) == 1:
        return await message.reply("<b>⚠️ Give me a user ID or username\n\nExample:</b> <code>/ban 123456789 spam</code>")

    target = message.command[1]
    reason = message.text.split(None, 2)[2] if len(message.text.split(None, 2)) > 2 else "No reason provided"

    try:
        user = await bot.get_users(int(target) if target.lstrip("-").isdigit() else target)
    except PeerIdInvalid:
        return await message.reply("<b>❌ Invalid user</b>")
    except Exception as e:
        return await message.reply(field("❌ Error", e))

    status = await db.get_ban_status(user.id)
    if status["is_banned"]:
        return await message.reply(f"<b>⚠️ {user_link(user.id, user.first_name)} is already banned</b>")

    await db.ban_user(user.id, reason)
    try:
        await message.reply(
            "<b>🚫 User Banned\n━━━━━━━━━━━━━━\n"
            f"👤 Name: {user_link(user.id, user.first_name)}</b>\n"
            f"{field('🆔 ID', user.id)}\n"
            f"<b>📝 Reason: {escape(reason)}</b>"
        )
    except Exception:
        pass

@Client.on_message(filters.command("unban") & filters.user(ADMINS) & connected_group)
async def unban_user(bot, message):
    if len(message.command) == 1:
        return await message.reply("<b>⚠️ Give me a user ID or username\n\nExample:</b> <code>/unban 123456789</code>")

    target = message.command[1]
    try:
        user = await bot.get_users(int(target) if target.lstrip("-").isdigit() else target)
    except Exception as e:
        return await message.reply(field("❌ Error", e))

    status = await db.get_ban_status(user.id)
    if not status["is_banned"]:
        return await message.reply(f"<b>⚠️ {user_link(user.id, user.first_name)} is not banned</b>")

    await db.remove_ban(user.id)
    await message.reply(
        "<b>✅ User Unbanned\n━━━━━━━━━━━━━━\n"
        f"👤 Name: {user_link(user.id, user.first_name)}</b>\n"
        f"{field('🆔 ID', user.id)}"
    )

async def reply_list_or_file(message, text, plain_lines, filename, caption, max_items):
    if len(plain_lines) <= max_items:
        try:
            return await message.reply(text)
        except MessageTooLong:
            pass
    with open(filename, "w", encoding="utf-8") as outfile:
        outfile.write("\n".join(plain_lines))
    await message.reply_document(filename, caption=caption)

@Client.on_message(filters.command("users") & filters.user(ADMINS) & connected_group)
async def list_users(bot, message):
    lines, plain = [], []
    users = await db.get_all_users()
    async for user in users:
        banned = user.get("ban_status", {}).get("is_banned")
        lines.append(f"👤 {user_link(user['id'], user.get('name'))}{' 🚫' if banned else ''}")
        plain.append(f"{user['id']} | {user.get('name') or 'User'}{' | Banned' if banned else ''}")

    text = "<b>👥 Users Saved In DB\n━━━━━━━━━━━━━━\n\n" + "\n".join(lines) + "</b>"
    await reply_list_or_file(message, text, plain, "users.txt", f"<b>👥 Total Users: {len(plain):,}</b>", 90)

@Client.on_message(filters.command("chats") & filters.user(ADMINS) & connected_group)
async def list_chats(bot, message):
    lines, plain = [], []
    chats = await db.get_all_chats()
    async for chat in chats:
        title = chat.get("title", "Unknown")
        lines.append(f"<b>💬 {escape(str(title))}</b>\n{field('🆔 ID', chat['id'])}\n")
        plain.append(f"{chat['id']} | {title}")

    text = "<b>🗨 Connected Chats\n━━━━━━━━━━━━━━</b>\n\n" + "\n".join(lines)
    await reply_list_or_file(message, text, plain, "chats.txt", f"<b>🗨 Total Chats: {len(plain):,}</b>", 30)

@Client.on_message(filters.command('channel') & filters.user(ADMINS))
async def channel_info(bot, message):
    channels = CHANNELS if isinstance(CHANNELS, list) else [CHANNELS]
    text = "<b>📑 Indexed Channels\n━━━━━━━━━━━━━━</b>\n"
    for channel in channels:
        try:
            chat = await bot.get_chat(channel)
        except Exception:
            continue
        name = escape(str(chat.title or chat.first_name or channel))
        text += f"\n<b>📢 {name}</b>"
        if chat.username:
            text += f" <code>@{chat.username}</code>"
    text += f"\n\n<b>📊 Total: {len(channels)}</b>"
    await message.reply(text)

@Client.on_message(filters.command('delete') & filters.user(ADMINS))
async def delete(bot, message):
    reply = message.reply_to_message
    if not reply or not reply.media:
        return await message.reply("<b>⚠️ Reply to a file with</b> <code>/delete</code> <b>to remove it</b>", quote=True)

    for file_type in ("document", "video", "audio"):
        media = getattr(reply, file_type, None)
        if media is not None:
            break
    else:
        return await message.reply("<b>❌ This file type is not supported</b>", quote=True)

    file_id, _ = unpack_new_file_id(media.file_id)
    result = await Media.collection.delete_one({'_id': file_id})

    if not result.deleted_count:
        file_name = re.sub(r"(_|\-|\.|\+)", " ", str(media.file_name))
        result = await Media.collection.delete_many({'file_name': file_name, 'file_size': media.file_size, 'mime_type': media.mime_type})

    if not result.deleted_count:
        result = await Media.collection.delete_many({'file_name': media.file_name, 'file_size': media.file_size, 'mime_type': media.mime_type})

    await message.reply("<b>🗑 File Deleted</b>" if result.deleted_count else "<b>❌ File not found in database</b>", quote=True)

DELETE_REQUEST_TTL = 600

async def edit_text_safely(message, text, **kwargs):
    for _ in range(2):
        try:
            return await message.edit_text(text, **kwargs)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except MessageNotModified:
            return
        except Exception:
            logging.getLogger(__name__).exception("Could not edit message")
            return

@Client.on_message(filters.command("deletefiles") & filters.user(ADMINS))
async def deletemultiplefiles(bot, message):
    parts = message.text.split(None, 1)
    keyword = parts[1].strip() if len(parts) > 1 else ""
    if not normalize(keyword):
        return await message.reply_text(
            "<b>⚠️ Give me a keyword along with the command\n\nExample:</b> <code>/deletefiles avatar 2009</code>"
        )

    status = await message.reply_text(field("⏳ Searching Files", keyword))
    total = await count_bad_files(keyword)
    if not total:
        return await edit_text_safely(status, field("❌ No Files Found", keyword))

    token = secrets.token_hex(4)
    await db.set_cache(f"deletefiles:{token}", {"keyword": keyword}, ttl=DELETE_REQUEST_TTL)
    await edit_text_safely(
        status,
        field(f"🗂 {total:,} Files Found", keyword),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛃 Delete Files!", callback_data=f"killfilesdq#{token}")],
            [InlineKeyboardButton("💢 Cancel 💢", callback_data="close_data")],
        ]),
    )

@Client.on_callback_query(filters.regex(r"^killfilesdq#"))
async def deletefiles_confirm(bot, query):
    if query.from_user.id not in ADMINS:
        return await query.answer("⛔ Only bot admins can delete files", show_alert=True)

    request = await db.pop_cache(f"deletefiles:{query.data.split('#', 1)[1]}")
    if not request:
        return await query.answer("⌛ This request expired or was already used. Run /deletefiles again", show_alert=True)

    keyword = request["keyword"]
    await query.answer("♻️ Deleting...")
    await edit_text_safely(query.message, field("♻️ Deleting Files", keyword))
    try:
        deleted = await delete_bad_files(keyword)
    except Exception:
        logging.getLogger(__name__).exception("Deleting files for %r failed", keyword)
        return await edit_text_safely(
            query.message,
            "<b>❌ Deleting Failed\n\nCheck</b> <code>/logs</code><b>, then run</b> <code>/deletefiles</code> <b>again</b>",
        )
    await edit_text_safely(query.message, field(f"✅ Deleted {deleted:,} Files", keyword))

@Client.on_callback_query(filters.regex(r'^setgs#'))
async def settings_callback(client, callback):
    if callback.from_user.id not in ADMINS:
        return await callback.answer("⛔ Only bot admins can change settings", show_alert=True)
    try:
        _, setting, _ = callback.data.split("#")
    except Exception:
        return await callback.answer("❌ Invalid setting", show_alert=True)
    if setting not in {"spell_check", "is_shortlink"}:
        return await callback.answer("❌ Invalid setting", show_alert=True)

    chat_id = callback.message.chat.id
    if chat_id and callback.message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        if not await is_group_connected(chat_id):
            return await callback.answer("⚠️ This group is disconnected", show_alert=True)
    settings = await get_settings(chat_id)
    current = bool(settings.get(setting))
    await save_group_settings(chat_id, setting, not current)
    settings = await get_settings(chat_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(settings))
    except Exception:
        pass
    await callback.answer("✅ Updated")

@Client.on_message(filters.command('settings') & filters.user(ADMINS))
async def settings(client, message):
    if message.chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return await message.reply_text("<b>⚠️ Use</b> <code>/settings</code> <b>inside a connected group</b>")
    if not await is_group_connected(message.chat.id):
        return
    settings = await get_settings(message.chat.id)
    await message.reply_text(
        f"<b>⚙️ Settings For {escape(str(message.chat.title))}</b>",
        reply_markup=get_settings_keyboard(settings),
        parse_mode=enums.ParseMode.HTML,
        reply_to_message_id=message.id,
    )

@Client.on_message(filters.command("shortlink1") & filters.user(ADMINS))
async def update_shortlink1(bot, message):
    if message.chat.type == enums.ChatType.PRIVATE:
        return await message.reply_text("<b>⚠️ Only works in groups!</b>")
    if not await is_group_connected(message.chat.id):
        return await message.reply_text("<b>⚠️ Connect this group first with</b> <code>/connect</code>")
    try:
        _, shortlink_url, api = message.text.split(" ", 2)
    except Exception:
        return await message.reply_text("<b>❌ Wrong Format\n\nExample:</b> <code>/shortlink1 softurl.in YOUR_API</code>")

    shortlink_url = re.sub(r"[:/]", "", re.sub(r"https?://?", "", shortlink_url))
    await save_group_settings(message.chat.id, 'shortlink', shortlink_url)
    await save_group_settings(message.chat.id, 'shortlink_api', api)
    await save_group_settings(message.chat.id, 'is_shortlink', True)
    reply = await message.reply_text(
        "<b>🔆 Primary Shortener Updated:-\n"
        "━━━━━━━━━━━━━━</b>\n"
        f"{field('🗃 Site', shortlink_url)}\n"
        f"{field('🔐 API', api)}"
    )
    await asyncio.sleep(10)
    await reply.delete()

@Client.on_message(filters.command("shortlink2") & filters.user(ADMINS))
async def update_shortlink2(bot, message):
    if message.chat.type == enums.ChatType.PRIVATE:
        return await message.reply_text("<b>⚠️ Only works in groups!</b>")
    if not await is_group_connected(message.chat.id):
        return await message.reply_text("<b>⚠️ Connect this group first with</b> <code>/connect</code>")
    try:
        _, shortlink_url, api = message.text.split(" ", 2)
    except Exception:
        return await message.reply_text("<b>❌ Wrong Format\n\nExample:</b> <code>/shortlink2 nowshort.com YOUR_API</code>")

    shortlink_url = re.sub(r"[:/]", "", re.sub(r"https?://?", "", shortlink_url))
    await save_group_settings(message.chat.id, 'second_shortlink', shortlink_url)
    await save_group_settings(message.chat.id, 'second_shortlink_api', api)
    await save_group_settings(message.chat.id, 'is_shortlink', True)
    reply = await message.reply_text(
        "<b>🔆 Backup Shortener Updated\n"
        "━━━━━━━━━━━━━━</b>\n"
        f"{field('🗃 Site', shortlink_url)}\n"
        f"{field('🔐 API', api)}"
    )
    await asyncio.sleep(10)
    await reply.delete()

SHORTENER_KEYS = ("shortlink", "shortlink_api", "second_shortlink", "second_shortlink_api")
SHORTENERS_VISIBLE_SECONDS = 30

def shorteners_text(settings: dict) -> str:
    return (
        "<b>🎛 Shortener Settings:-\n"
        "━━━━━━━━━━━</b>\n"
        f"{field('🗃 Primary', settings.get('shortlink') or SHORT1_URL)}\n"
        f"{field('🔐 API', settings.get('shortlink_api') or SHORT1_API)}\n"
        f"{field('🗃 Backup', settings.get('second_shortlink') or SHORT2_URL)}\n"
        f"{field('🔐 API', settings.get('second_shortlink_api') or SHORT2_API)}"
    )

def shorteners_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Switch Shorteners", callback_data="swap_shorteners")]])

@Client.on_message(filters.command("shorteners") & filters.user(ADMINS))
async def view_shorteners(bot, message):
    if message.chat.type == enums.ChatType.PRIVATE:
        return await message.reply_text("<b>⚠️ Only works in groups!</b>")
    if not await is_group_connected(message.chat.id):
        return await message.reply_text("<b>⚠️ Connect this group first with</b> <code>/connect</code>")
    settings = await get_settings(message.chat.id)

    sent_message = await message.reply_text(
        shorteners_text(settings),
        reply_markup=shorteners_keyboard(),
        parse_mode=enums.ParseMode.HTML,
    )

    await asyncio.sleep(SHORTENERS_VISIBLE_SECONDS)
    try:
        await sent_message.delete()
    except Exception:
        pass

@Client.on_callback_query(filters.regex(r"^swap_shorteners$"))
async def swap_shorteners(client, callback):
    if callback.from_user.id != OWNER_ID:
        return await callback.answer("⛔ Only the bot owner can switch shorteners", show_alert=True)

    chat = callback.message.chat
    if chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP) and not await is_group_connected(chat.id):
        return await callback.answer("⚠️ This group is disconnected", show_alert=True)

    settings = await get_settings(chat.id)
    old = (
        settings.get("shortlink") or SHORT1_URL,
        settings.get("shortlink_api") or SHORT1_API,
        settings.get("second_shortlink") or SHORT2_URL,
        settings.get("second_shortlink_api") or SHORT2_API,
    )
    new = (old[2], old[3], old[0], old[1])

    try:
        for key, value in zip(SHORTENER_KEYS, new):
            await save_group_settings(chat.id, key, value)
    except Exception:
        logging.getLogger(__name__).exception("Switching shorteners failed in chat %s", chat.id)
        for key, value in zip(SHORTENER_KEYS, old):
            try:
                await save_group_settings(chat.id, key, value)
            except Exception:
                pass
        return await callback.answer("❌ Could not switch the shorteners. Please try again", show_alert=True)

    settings = await get_settings(chat.id)
    try:
        await callback.message.edit_text(
            shorteners_text(settings),
            reply_markup=shorteners_keyboard(),
            parse_mode=enums.ParseMode.HTML,
        )
    except MessageNotModified:
        pass
    await callback.answer("♻️ Primary and backup switched")

@Client.on_message(filters.command('logs') & filters.user(ADMINS))
async def log_file(bot, message):
    try:
        await message.reply_document('TelegramBot.log', caption="<b>📄 Error Logs</b>")
    except Exception as e:
        await message.reply(field("❌ Error", e))

@Client.on_message(filters.command('clearindex') & filters.user(ADMINS))
async def delete_all_index(bot, message):
    await message.reply_text(
        "<b>⚠️ Delete All Indexed Files?\n\nEvery file will be removed from the database and this cannot be undone</b>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text="🛃 Delete Files!", callback_data="clear_index")],
            [InlineKeyboardButton(text="💢 Cancel 💢", callback_data="close_data")],
        ]),
        quote=True,
    )

@Client.on_callback_query(filters.regex(r'^clear_index$'))
async def delete_all_index_confirm(bot, callback):
    if callback.from_user.id not in ADMINS:
        return await callback.answer("⛔ Unauthorized", show_alert=True)
    await Media.collection.drop()
    await callback.answer("✅ Done")
    await callback.message.edit("<b>✅ All indexed files deleted</b>")

@Client.on_message(filters.command("restart") & filters.user(ADMINS))
async def stop_button(bot, message):
    await bot.send_message(
        chat_id=message.chat.id,
        text="<b>🔄 Bot Is Restarting...</b>",
    )
    await db.set_restart_flag()
    os.execl(sys.executable, sys.executable, *sys.argv)
