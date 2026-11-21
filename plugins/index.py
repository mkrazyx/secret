import asyncio
import base64
import logging
import re
from html import escape
from struct import pack
from marshmallow.exceptions import ValidationError
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
from pyrogram import Client, enums, filters
from pyrogram.errors import ChannelInvalid, ChannelPrivate, FloodWait, PeerIdInvalid, UsernameInvalid, UsernameNotModified
from pyrogram.file_id import FileId
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from umongo import Document, Instance, fields
from info import *
from utils import connected_group, extract_v2, field, pretty_name, temp

PAGE_SIZE = 10

client = AsyncIOMotorClient(DATABASE_URI)
mongo_db = client[DATABASE_NAME]
instance = Instance.from_db(mongo_db)

def normalize(text: str) -> list:
    text = text.casefold()
    text = re.sub(r"@[^\s.-]+", " ", text)
    text = re.sub(r"[_]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text.split()

def normalize_basic_episode(text: str) -> str:
    text = text.casefold()
    text = re.sub(r'\bs(\d{2})\s*e(\d{2})\b', r's\1e\2', text)
    text = re.sub(r'\bs(\d{2})\s*ep\s*(\d{2})\b', r's\1e\2', text)
    return text

LANG_CODES = {
    "eng": "english", "hin": "hindi", "tam": "tamil", "tel": "telugu",
    "kan": "kannada", "mal": "malayalam", "kor": "korean",
}

async def normalize_for_search(text: str) -> str:
    text = normalize_basic_episode(str(text or ""))
    return " ".join(LANG_CODES.get(word, word) for word in normalize(text))

async def index_name(text: str) -> str:
    return pretty_name(await normalize_for_search(text))

@instance.register
class Media(Document):
    file_id = fields.StrField(attribute="_id")
    file_name = fields.StrField(required=True)
    file_size = fields.IntField(required=True)
    file_type = fields.StrField(allow_none=True)
    mime_type = fields.StrField(allow_none=True)

    class Meta:
        collection_name = COLLECTION_NAME
        strict = False

async def save_file(media):
    file_id, _ = unpack_new_file_id(media.file_id)

    original_name = str(media.file_name or "")
    source_text = original_name

    if (
        getattr(media, "chat_id", None) == CAPTION_INDEX_CHANNEL
        and getattr(media, "caption", None)
    ):
        source_text = media.caption

    source_text = str(source_text)[:1000]
    normalized_name = await index_name(source_text)

    try:
        file = Media(
            file_id=file_id,
            file_name=normalized_name,
            file_size=media.file_size,
            file_type=media.file_type,
            mime_type=media.mime_type,
        )
        await file.commit()
    except ValidationError:
        logger.exception("Validation error while saving file")
        return False, 2
    except DuplicateKeyError:
        if getattr(media, "chat_id", None) == CAPTION_INDEX_CHANNEL and getattr(media, "caption", None):
            try:
                await Media.collection.update_one(
                    {"_id": file_id},
                    {"$set": {"file_name": normalized_name}},
                )
                logger.info("%s updated using caption indexing", original_name)
                return True, 1
            except Exception:
                logger.exception("Failed updating duplicate file")
        return False, 0

    logger.info("%s indexed", original_name)
    return True, 1

async def get_search_results(query, offset=0):
    max_results = PAGE_SIZE

    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    query = (await extract_v2(query)).strip()
    words = normalize(query)

    if not words:
        return [], "", 0

    strict_conditions = [
        {
            "file_name": {
                "$regex": rf"\b{re.escape(word)}\b",
                "$options": "i",
            }
        }
        for word in words
    ]
    strict_filter = {"$and": strict_conditions}

    fuzzy_conditions = [
        {
            "file_name": {
                "$regex": re.escape(word),
                "$options": "i",
            }
        }
        for word in words
    ]
    fuzzy_filter = {
        "$and": fuzzy_conditions,
        "$nor": [{"$and": strict_conditions}],
    }

    strict_count_task = Media.count_documents(strict_filter)
    fuzzy_count_task = Media.count_documents(fuzzy_filter)
    strict_total, fuzzy_total = await asyncio.gather(
        strict_count_task,
        fuzzy_count_task,
    )

    total_results = strict_total + fuzzy_total

    if total_results == 0 or offset >= total_results:
        return [], "", total_results

    if offset < strict_total:
        strict_cursor = (
            Media.find(strict_filter)
            .sort("$natural", -1)
            .skip(offset)
            .limit(max_results)
        )
        files = await strict_cursor.to_list(length=max_results)

        if len(files) < max_results and offset + len(files) < total_results:
            remaining = max_results - len(files)
            fuzzy_cursor = (
                Media.find(fuzzy_filter)
                .sort("$natural", -1)
                .skip(0)
                .limit(remaining)
            )
            fuzzy_files = await fuzzy_cursor.to_list(length=remaining)
            files.extend(fuzzy_files)
    else:
        fuzzy_offset = offset - strict_total
        fuzzy_cursor = (
            Media.find(fuzzy_filter)
            .sort("$natural", -1)
            .skip(fuzzy_offset)
            .limit(max_results)
        )
        files = await fuzzy_cursor.to_list(length=max_results)

    next_offset = offset + len(files)
    if next_offset >= total_results:
        next_offset = ""

    return files, next_offset, total_results

def bad_files_filter(query):
    words = normalize(query)
    if not words:
        return None
    return {"$and": [{"file_name": {"$regex": re.escape(word), "$options": "i"}} for word in words]}

async def count_bad_files(query):
    mongo_filter = bad_files_filter(query)
    return await Media.count_documents(mongo_filter) if mongo_filter else 0

async def delete_bad_files(query):
    mongo_filter = bad_files_filter(query)
    if not mongo_filter:
        return 0
    result = await Media.collection.delete_many(mongo_filter)
    return result.deleted_count

async def get_file_details(file_id):
    return await Media.find({"_id": file_id}).to_list(length=1)

def encode_file_id(s: bytes) -> str:
    r = b""
    n = 0
    for i in s + bytes([22]) + bytes([4]):
        if i == 0:
            n += 1
        else:
            if n:
                r += b"\x00" + bytes([n])
                n = 0
            r += bytes([i])
    return base64.urlsafe_b64encode(r).decode().rstrip("=")

def encode_file_ref(file_ref: bytes) -> str:
    return base64.urlsafe_b64encode(file_ref).decode().rstrip("=")

def unpack_new_file_id(new_file_id):
    decoded = FileId.decode(new_file_id)
    file_id = encode_file_id(
        pack(
            "<iiqq",
            int(decoded.file_type),
            decoded.dc_id,
            decoded.media_id,
            decoded.access_hash,
        )
    )
    file_ref = encode_file_ref(decoded.file_reference)
    return file_id, file_ref

media_filter = filters.document | filters.video

@Client.on_message(filters.chat(CHANNELS) & media_filter)
async def media(bot, message):
    for file_type in ("document", "video"):
        media = getattr(message, file_type, None)
        if media is not None:
            break
    else:
        return

    media.file_type = file_type
    media.caption = message.caption
    media.chat_id = message.chat.id
    await save_file(media)

logger = logging.getLogger(__name__)

logger.setLevel(logging.INFO)

lock = asyncio.Lock()

index_state_col = mongo_db["index_state"]

async def get_index_state():
    return await index_state_col.find_one({"_id": "active_index"})

async def save_index_state(chat_id, lst_msg_id, current, total_files, duplicate, deleted, access_hash=None):
    state = {
        "chat_id": chat_id,
        "lst_msg_id": lst_msg_id,
        "current": current,
        "total_files": total_files,
        "duplicate": duplicate,
        "deleted": deleted,
    }
    if access_hash:
        state["access_hash"] = access_hash
    await index_state_col.update_one({"_id": "active_index"}, {"$set": state}, upsert=True)

async def clear_index_state():
    await index_state_col.delete_one({"_id": "active_index"})

async def get_access_hash(bot, chat):
    try:
        return getattr(await bot.resolve_peer(chat), "access_hash", None)
    except Exception:
        return None

async def restore_peer(bot, chat, access_hash):
    if not access_hash or not str(chat).startswith("-100"):
        return
    try:
        await bot.storage.update_peers([(int(chat), access_hash, "channel", None, None)])
    except Exception:
        logger.exception("Could not restore the saved peer for %s", chat)

async def open_index_chat(bot, chat, access_hash=None, attempts=1):
    await restore_peer(bot, chat, access_hash)
    for attempt in range(1, attempts + 1):
        try:
            return await bot.get_chat(chat)
        except FloodWait as e:
            error = e
            await asyncio.sleep(e.value + 1)
        except (PeerIdInvalid, ChannelInvalid, ChannelPrivate) as e:
            error = e
            if attempt < attempts:
                await asyncio.sleep(3 * attempt)
    raise error

async def notify_owner(bot, text, reply_markup=None):
    for _ in range(2):
        try:
            return await bot.send_message(OWNER_ID, text, reply_markup=reply_markup)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception:
            logger.exception("Could not message the owner")
            return

@Client.on_callback_query(filters.regex(r'^index'))
async def index_files(bot, query):
    if not query.from_user or query.from_user.id not in ADMINS:
        return await query.answer("⛔ Unauthorized", show_alert=True)
    if query.data.startswith('index_cancel'):
        temp.CANCEL = True
        await clear_index_state()
        return await query.answer("🚫 Cancelling Indexing...")

    if query.data == 'index_status':
        return await query.answer(
            "📊 Live Index Status\n━━━━━━━━━━━━━━\n"
            f"📥 Processed: {temp.CURRENT:,}\n"
            f"🗃 Saved: {temp.SAVED:,}\n"
            f"♻️ Duplicates: {temp.DUP:,}\n"
            f"🗑 Deleted: {temp.DEL:,}",
            show_alert=True
        )

    try:
        _, _, chat, lst_msg_id = query.data.split("#")
    except (ValueError, AttributeError):
        return await query.answer("❌ Invalid index request", show_alert=True)
    if lock.locked():
        return await query.answer("⏳ Wait until the previous index is finished", show_alert=True)
    msg = query.message
    await query.answer("⏳ Processing...", show_alert=True)
    temp.CANCEL = False
    await msg.edit(
        "<b>⏳ Starting Indexing...</b>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton('📊 Check Live Status', callback_data='index_status')],
             [InlineKeyboardButton('❌ Cancel ❌', callback_data='index_cancel')]]
        )
    )
    try:
        chat = int(chat)
    except:
        pass
    asyncio.create_task(index_files_to_db(int(lst_msg_id), chat, msg, bot))

INDEX_LINK_RE = re.compile(r"^(https?://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|(?!c/)[a-zA-Z_0-9]+)/(\d+)$")

@Client.on_message(
    (filters.forwarded | (filters.regex(INDEX_LINK_RE) & filters.text)) & filters.private & filters.incoming
)
async def send_for_index(bot, message):
    if message.from_user.id not in ADMINS:
        return

    if message.text:
        match = INDEX_LINK_RE.match(message.text)
        if not match:
            return await message.reply("<b>❌ Invalid link</b>")
        chat_id = match.group(4)
        last_msg_id = int(match.group(5))
        if chat_id.isnumeric():
            chat_id = int("-100" + chat_id)
    elif message.forward_from_chat and message.forward_from_chat.type == enums.ChatType.CHANNEL:
        last_msg_id = message.forward_from_message_id
        chat_id = message.forward_from_chat.username or message.forward_from_chat.id
    else:
        return

    try:
        await bot.get_chat(chat_id)
    except ChannelInvalid:
        return await message.reply(
            "<b>🔒 This channel is private. Make me an admin there to index its files</b>"
        )
    except (UsernameInvalid, UsernameNotModified):
        return await message.reply("<b>❌ Invalid link specified</b>")
    except Exception as e:
        logger.exception(e)
        return await message.reply(field("❌ Error", e))

    try:
        k = await bot.get_messages(chat_id, last_msg_id)
    except:
        return await message.reply(
            "<b>🔒 Make sure I am an admin in the channel if it is private</b>"
        )

    if k.empty:
        return await message.reply(
            "<b>⚠️ This may be a group and I am not an admin of it</b>"
        )
    buttons = [[
        InlineKeyboardButton(
            '✅ Accept',
            callback_data=f'index#accept#{chat_id}#{last_msg_id}'
        ),
        InlineKeyboardButton('❌ Reject', callback_data='close_data')
    ]]

    return await message.reply(
        "<b>❓ Index This Channel's Files?\n━━━━━━━━━━━━━━</b>\n"
        f"{field('🆔 Chat', chat_id)}",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

SKIP_LINK_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:c/\d+|(?!c/)[a-z_]\w*)(?:/\d+)?/(\d+)/?(?:\?\S*)?$", re.I
)

def parse_skip(value):
    value = value.strip()
    if value.isdigit():
        return int(value)
    match = SKIP_LINK_RE.match(value)
    return int(match.group(1)) if match else None

@Client.on_message(filters.command('setskip') & filters.user(ADMINS) & connected_group)
async def set_skip_number(bot, message):
    if len(message.command) < 2:
        return await message.reply(
            "<b>⚠️ Give me a skip number or a message link\n\nExample:</b>\n"
            "<code>/setskip 624766</code>\n"
            "<code>/setskip https://t.me/c/1725696043/624766</code>"
        )
    skip = parse_skip(message.command[1])
    if skip is None:
        return await message.reply(
            "<b>❌ Skip should be a message number or a message link\n\nExample:</b>\n"
            "<code>https://t.me/c/1725696043/624766</code>"
        )
    temp.CURRENT = skip
    await message.reply(field("📲 Skip Number Set", skip))

async def iter_index_messages(bot, chat_id, limit, offset=0):
    current = offset
    while True:
        new_diff = min(200, limit - current)
        if new_diff <= 0:
            return
        messages = await bot.get_messages(
            chat_id, list(range(current, current + new_diff + 1))
        )
        for message in messages:
            yield message
            current += 1

async def index_files_to_db(lst_msg_id, chat, msg, bot, resume_current=None, resume_saved=0, resume_dup=0, resume_del=0, access_hash=None):
    total_files = resume_saved
    duplicate = resume_dup
    deleted = resume_del

    temp.SAVED = total_files
    temp.DUP = duplicate
    temp.DEL = deleted

    if resume_current is not None:
        temp.CURRENT = resume_current

    cancelled = False
    chat_info = None
    async with lock:
        try:
            try:
                chat_info = await open_index_chat(bot, chat, access_hash)
            except (PeerIdInvalid, ChannelInvalid, ChannelPrivate, UsernameInvalid, UsernameNotModified) as e:
                await clear_index_state()
                logger.error("Unable to resolve indexing chat %s: %s", chat, e)
                try:
                    await msg.edit("<b>❌ Indexing Stopped\n\nThe source channel is no longer accessible</b>")
                except Exception:
                    pass
                return
            access_hash = access_hash or await get_access_hash(bot, chat)

            current = temp.CURRENT
            last_edit = current
            async for message in iter_index_messages(bot, chat, lst_msg_id, temp.CURRENT):
                if temp.CANCEL:
                    try:
                        await msg.edit(
                            "<b>🚫 Indexing Cancelled\n━━━━━━━━━━━━━━\n"
                            f"🗃 Saved: {total_files:,}\n"
                            f"♻️ Duplicates: {duplicate:,}\n"
                            f"🗑 Deleted Messages: {deleted:,}</b>"
                        )
                    except FloodWait as e:
                        logger.warning(f"FloodWait while cancelling: {e.value} seconds.")
                    await clear_index_state()
                    cancelled = True
                    break

                current += 1
                temp.CURRENT = current

                if current % 50 == 0:
                    await save_index_state(chat, lst_msg_id, current, total_files, duplicate, deleted, access_hash)

                if current - last_edit >= 2000:
                    last_edit = current
                    await save_index_state(chat, lst_msg_id, current, total_files, duplicate, deleted, access_hash)
                    try:
                        await msg.edit_text(
                            text="<b>📊 Indexing In Progress\n━━━━━━━━━━━━━━\n"
                                 f"📥 Messages Fetched: {current:,}\n"
                                 f"🗃 Saved: {total_files:,}\n"
                                 f"♻️ Duplicates: {duplicate:,}\n"
                                 f"🗑 Deleted: {deleted:,}</b>",
                            reply_markup=InlineKeyboardMarkup(
                                [[InlineKeyboardButton('📊 Check Live Status', callback_data='index_status')],
                                 [InlineKeyboardButton('Cancel', callback_data='index_cancel')]]
                            )
                        )
                    except FloodWait as e:
                        logger.warning(f"FloodWait while updating progress: {e.value} seconds.")

                if message.empty:
                    deleted += 1
                    temp.DEL = deleted
                    continue
                elif not message.media:
                    continue
                elif message.media not in [
                    enums.MessageMediaType.VIDEO,
                    enums.MessageMediaType.DOCUMENT
                ]:
                    continue

                media = getattr(message, message.media.value, None)
                if not media:
                    continue

                media.file_type = message.media.value
                media.caption = message.caption
                media.chat_id = message.chat.id
                aynav, vnay = await save_file(media)

                if aynav:
                    total_files += 1
                    temp.SAVED = total_files
                elif vnay == 0:
                    duplicate += 1
                    temp.DUP = duplicate

        except Exception as e:
            logger.exception(e)
            error_text = str(e)
            try:
                await msg.edit(field("❌ Error", error_text))
            except FloodWait as flood:
                logger.warning(f"FloodWait while sending error: {flood.value} seconds.")
            await notify_owner(
                bot,
                "<b>❌ Indexing Stopped\n━━━━━━━━━━━━━━\n"
                f"📢 Channel: {escape(str(getattr(chat_info, 'title', None) or chat))}\n"
                f"📊 Processed: {temp.CURRENT:,}\n"
                f"❗ Error: {escape(error_text)}</b>"
            )
        else:
            temp.CURRENT = 0
            await clear_index_state()
            if cancelled:
                return
            try:
                await msg.edit(
                    "<b>✅ Indexing Completed\n━━━━━━━━━━━━━━\n"
                    f"🗃 Saved: {total_files:,}\n"
                    f"♻️ Duplicates: {duplicate:,}\n"
                    f"🗑 Deleted Messages: {deleted:,}</b>"
                )
            except FloodWait as e:
                logger.warning(f"FloodWait while sending final result: {e.value} seconds.")
            await notify_owner(
                bot,
                "<b>✅ Indexing Completed\n━━━━━━━━━━━━━━\n"
                f"📢 Channel: {escape(str(getattr(chat_info, 'title', None) or chat))}\n"
                f"🗃 Saved: {total_files:,}\n"
                f"♻️ Duplicates: {duplicate:,}\n"
                f"🗑 Deleted Messages: {deleted:,}</b>"
            )

async def check_pending_index_on_startup(client):
    state = await get_index_state()
    if not state:
        return
    chat_id = state.get("chat_id")
    lst_msg_id = state.get("lst_msg_id")
    current = state.get("current", 0)
    saved = state.get("total_files", 0)
    dup = state.get("duplicate", 0)
    deleted = state.get("deleted", 0)
    access_hash = state.get("access_hash")

    logger.info(f"Found pending indexing task! Resuming from message count: {current}")

    try:
        await open_index_chat(client, chat_id, access_hash, attempts=3)
    except Exception as e:
        logger.exception("Could not resume the pending index for %s", chat_id)
        await notify_owner(
            client,
            "<b>⚠️ Could Not Resume Indexing\n━━━━━━━━━━━━━━\n"
            f"📊 Processed: {current:,}\n"
            f"❗ Reason: {escape(str(e))}</b>\n"
            f"{field('🆔 Chat', chat_id)}\n\n"
            f"<b>Make sure the bot is still in that channel, then run</b> <code>/setskip {current}</code> "
            "<b>and send the channel link again to continue from here</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🗑 Discard Pending Index', callback_data='index_cancel')]]),
        )
        return

    for admin_id in ADMINS:
        try:
            msg = await client.send_message(
                admin_id,
                "<b>🔄 Resuming Interrupted Indexing\n━━━━━━━━━━━━━━\n"
                f"📥 Processed: {current:,}\n"
                f"🗃 Saved: {saved:,}</b>",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton('📊 Check Live Status', callback_data='index_status')],
                     [InlineKeyboardButton('Cancel', callback_data='index_cancel')]]
                )
            )
        except Exception:
            logger.exception("Could not send the resume message to %s", admin_id)
            continue
        temp.CANCEL = False
        asyncio.create_task(index_files_to_db(lst_msg_id, chat_id, msg, client, current, saved, dup, deleted, access_hash))
        break
    else:
        logger.error("Pending index for %s was not resumed: no admin could be messaged", chat_id)

@Client.on_edited_message(filters.chat(CAPTION_INDEX_CHANNEL))
async def caption_edit_handler(client, message):
    media = message.document or message.video
    if not media:
        return

    source_text = (message.caption or getattr(media, "file_name", "") or "")[:1000]

    try:
        file_id, _ = unpack_new_file_id(media.file_id)
        normalized_name = await index_name(source_text)

        result = await Media.collection.update_one(
            {"_id": file_id},
            {"$set": {"file_name": normalized_name}},
        )

        if result.matched_count:
            logger.info("Caption index updated for %s", file_id)
    except Exception:
        logger.exception("Failed updating caption index")
