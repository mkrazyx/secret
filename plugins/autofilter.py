import asyncio
import logging
import math
import re
import time as _time
from html import escape

from pyrogram import Client, enums, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from info import *
from plugins.index import PAGE_SIZE, get_file_details, get_search_results
from utils import db, delete_later, get_settings, get_shortlink, get_size, is_group_connected, is_subscribed, search_gagala, temp

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)
SPELL_CHECK = {}
GLOBAL_SEM = asyncio.Semaphore(12)
USER_COOLDOWN = {}

def tutorial_url():
    return TUTORIAL if TUTORIAL.startswith("http") else f"https://telegram.me/{TUTORIAL}"

TUTORIAL_BUTTON = InlineKeyboardButton(
    "❓ How To Download ❓",
    url=tutorial_url()
)

EXPIRED = '♻ Link Expired, Please Request in Group Again!'
ALRT_TXT = '🔒 This option belongs to another user. '
REMOVES = [
    "in", "series", "4k", "kdrama", "ott",
    "movies", "webseries", "language", "hd", "hollywood",
    "and", "bollywood", "dub", "anime",
    "dubbed", "file", "download", "movie", "film",
    "netflix", "link", "subtitles", "dubbing",
    "full movie", "korean drama", "web series", "k drama",
    "tv series", "television series", "tv show", "with subtitles"
]

def remove_words(text):
    text = " ".join(text.split())
    for x in sorted(REMOVES, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(x)}\b", " ", text, flags=re.I)
    return " ".join(text.split())

def is_spam(uid, cooldown=2):
    now = _time.monotonic()
    last = USER_COOLDOWN.get(uid, 0)
    if now - last < cooldown:
        return True
    USER_COOLDOWN[uid] = now
    return False

async def get_result_buttons(chat_id, req_user_id, cache_id, offset, total_results):
    current_page = (offset // PAGE_SIZE) + 1
    total_pages = max(1, math.ceil(total_results / PAGE_SIZE))

    btn = []

    if offset > 0:
        btn.append(
            InlineKeyboardButton(
                "⏪ Back",
                callback_data=f"next_{cache_id}_{offset - PAGE_SIZE}_{req_user_id}"
            )
        )

    btn.append(
        InlineKeyboardButton(
            f"📓 Page {current_page} / {total_pages}",
            callback_data="pages"
        )
    )

    if offset + PAGE_SIZE < total_results:
        btn.append(
            InlineKeyboardButton(
                "Next ⏩",
                callback_data=f"next_{cache_id}_{offset + PAGE_SIZE}_{req_user_id}"
            )
        )

    settings = await get_settings(chat_id)

    if settings.get("is_shortlink", IS_SHORTLINK):
        return [
            btn,
            [
                TUTORIAL_BUTTON
            ]
        ]

    return [btn]

def file_title(file):
    return " ".join(word for word in (file.file_name or "").split() if not word.startswith(("www.", "@")))

def build_results_caption(chat_id, search, files):
    cap = (
        f"<b>🔆 Results For ➔ ‛{escape(search)}’👇\n\n"
        f"🎬 Select Your Pick ↡\n\n"
    )

    for file in files:
        title = file_title(file)
        cap += (
            f"🍿 <a href=\"https://telegram.me/{temp.U_NAME}"
            f"?start=files_{chat_id}_{file.file_id}\">"
            f"[{get_size(file.file_size)}] {escape(title)}</a>\n\n"
        )

    cap += "</b>"
    return cap

async def store_file_links(chat_id, files):
    for file in files:
        await db.set_cache(
            f"file:{chat_id}:{file.file_id}",
            {"chat_id": chat_id},
            ttl=660,
        )

async def store_pagination(key, search):
    await db.set_cache(
        f"pagination:{key}",
        {"search": search},
        ttl=660,
    )

@Client.on_message(
    filters.group
    & filters.text
    & filters.incoming
    & ~filters.regex(r"^/")
)
async def give_filter(client, message):

    try:
        connected = await is_group_connected(message.chat.id)
    except Exception:
        logger.exception("Failed to check connected status for group %s", message.chat.id)
        return

    if not connected:
        return

    await auto_filter(client, message)

@Client.on_callback_query(filters.regex(r"^next_"))
async def next_page(bot, query):
    if not query.message or query.message.chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        return await query.answer(ALRT_TXT, show_alert=True)
    if not await is_group_connected(query.message.chat.id):
        return await query.answer("This group is not connected.", show_alert=True)
    if is_spam(query.from_user.id):
        return

    async with GLOBAL_SEM:
        try:
            _, key, offset, req = query.data.split("_")
            offset = int(offset)
            req = int(req)
        except Exception:
            return await query.answer(ALRT_TXT, show_alert=True)

        if req not in (query.from_user.id, 0):
            return await query.answer(ALRT_TXT, show_alert=True)

        cache = await db.get_cache(f"pagination:{key}")
        if not cache:
            return await query.answer(EXPIRED, show_alert=True)

        search = cache.get("search")
        if not search:
            return await query.answer(EXPIRED, show_alert=True)

        files, _, total = await get_search_results(search, offset=offset)
        if not files:
            return await query.answer(EXPIRED, show_alert=True)

        buttons = await get_result_buttons(
            query.message.chat.id, req, key, offset, total
        )
        await store_file_links(query.message.chat.id, files)
        cap = build_results_caption(query.message.chat.id, search, files)

        try:
            await query.message.edit_text(
                cap,
                reply_markup=InlineKeyboardMarkup(buttons),
                disable_web_page_preview=True,
                parse_mode=enums.ParseMode.HTML,
            )
            await query.answer()
        except MessageNotModified:
            await query.answer()
        except Exception:
            await query.answer(ALRT_TXT, show_alert=True)

@Client.on_callback_query(filters.regex(r"^spolling"))
async def advantage_spoll_choker(bot, query):
    if not query.from_user or not query.message:
        return await query.answer(ALRT_TXT, show_alert=True)
    if query.message.chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        return await query.answer(ALRT_TXT, show_alert=True)
    if not await is_group_connected(query.message.chat.id):
        return await query.answer("This group is not connected.", show_alert=True)
    if is_spam(query.from_user.id):
        return

    async with GLOBAL_SEM:
        try:
            _, user, movie_ = query.data.split('#')
            user = int(user)
        except:
            return await query.answer(ALRT_TXT, show_alert=True)

        if user != 0 and query.from_user.id != user:
            return await query.answer(ALRT_TXT, show_alert=True)

        if movie_ == "close":
            await query.message.delete()
            return await query.answer()

        movies = SPELL_CHECK.get(query.message.reply_to_message.id if query.message.reply_to_message else 0)
        if not movies:
            await query.answer(EXPIRED, show_alert=True)
            try:
                await query.message.edit_text(text="❗Link Expired, Request Again ♻", disable_web_page_preview=True)
            except:
                pass
            return

        try:
            movie = movies[int(movie_)]
        except:
            return await query.answer("❗Invalid Option", show_alert=True)

        files, offset, total_results = await get_search_results(movie, offset=0)
        if files:
            await auto_filter(bot, query, (movie, files, offset, total_results))
        else:
            try:
                msg = await query.message.edit_text(text=NO_RESULTS, disable_web_page_preview=True)
                await asyncio.sleep(60)
                await msg.delete()
            except Exception:
                pass

@Client.on_callback_query(filters.regex(r"^(close_data|pages)"))
async def cb_handler(client, query):
    if query.data == "close_data":
        try:
            await query.message.delete()
        except Exception:
            pass
        return await query.answer("Closed!")
    if query.data == "pages":
        return await query.answer()

async def auto_filter(client, msg, spoll=False):
    if not spoll:
        message = msg
        if (
            re.match(r"[,!.\U0001F900-\U000E007F]", message.text)
            or len(message.text) >= 100
        ):
            return

        search = remove_words(message.text.lower())
        search = re.sub(
            r"\b(complete|combined|all\s*episodes?|full\s*episodes?)\b",
            "com",
            search,
            flags=re.IGNORECASE,
        )
        search = re.sub(r"[-:–]+", " ", search)
        search = re.sub(r"\s+", " ", search).strip()
        search = re.sub(
            r"(?:(?:session|season)\s?)(\d+)",
            lambda x: f"s{x.group(1).zfill(2)}",
            search,
            flags=re.IGNORECASE,
        )
        search = re.sub(
            r"so(\d+)",
            lambda x: f"s{x.group(1).zfill(2)}",
            search,
            flags=re.IGNORECASE,
        )

        if not search.strip():
            k = await message.reply_text(
                NO_RESULTS,
                disable_web_page_preview=True
            )
            await asyncio.sleep(60)
            await k.delete()
            return

        files, _, total_results = await get_search_results(search, offset=0)
        if not files:
            settings = await get_settings(message.chat.id)
            if settings.get("spell_check", False):
                return await advantage_spell_chok(client, msg)
            return
    else:
        message = msg.message.reply_to_message
        if not message:
            return await msg.answer(EXPIRED, show_alert=True)
        search, files, _, total_results = spoll
        try:
            await msg.message.delete()
        except Exception:
            pass

    key = f"{message.chat.id}-{message.id}"
    req = message.from_user.id if message.from_user else 0
    await store_pagination(key, search)
    buttons = await get_result_buttons(
        message.chat.id, req, key, 0, total_results
    )
    await store_file_links(message.chat.id, files)
    cap = build_results_caption(message.chat.id, search, files)

    result = await message.reply_text(
        cap,
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML,
    )
    asyncio.create_task(delete_later(result))
    asyncio.create_task(delete_later(message))

JUNK_RE = re.compile(
    r"\b(imdb|wikipedia|reviews|full|all|episodes?|film|movie|tv\s*series|television\s*series|web\s*series|tv\s*show|show|series)\b",
    re.IGNORECASE,
)
WATCH_RE = re.compile(r"watch\s+([a-zA-Z0-9_\s\-\(\)]+)", re.IGNORECASE)

def tidy_title(text):
    return " ".join(re.sub(r"[()_\-]", " ", text).split())

async def advantage_spell_chok(client, msg):
    if not msg.from_user:
        return
    user_id = msg.from_user.id

    query = remove_words(msg.text) + " movie"
    first, second = await asyncio.gather(search_gagala(query), search_gagala(msg.text))
    g_s = first + second

    names = [tidy_title(JUNK_RE.sub(" ", t)) for t in g_s if re.search(r"imdb|wikipedia", t, re.IGNORECASE)]
    names = [n for n in names if n]
    if not names:
        names = [tidy_title(m.group(1)) for t in g_s if (m := WATCH_RE.search(t))]
    movielist = list(dict.fromkeys(filter(None, names)))[:3]

    if not movielist:
        k = await msg.reply(NO_RESULTS, disable_web_page_preview=True)
        await asyncio.sleep(60)
        return await k.delete()

    SPELL_CHECK[msg.id] = movielist
    btn = [[InlineKeyboardButton(text=movie, callback_data=f"spolling#{user_id}#{idx}")] for idx, movie in enumerate(movielist)]
    btn.append([InlineKeyboardButton("×××× ⟨ Close ⟩ ××××", callback_data=f"spolling#{user_id}#close")])
    k = await msg.reply("<b>🎬 Select Your Pick ↡</b>", reply_markup=InlineKeyboardMarkup(btn))
    await asyncio.sleep(60)
    await k.delete()
    SPELL_CHECK.pop(msg.id, None)

async def send_file_to_user(client, user_id, file_id):
    files = await get_file_details(file_id)
    if not files:
        return False
    file = files[0]
    title = file_title(file)
    caption = CAPTION.format(file_name=escape(title))

    await client.send_cached_media(
        chat_id=user_id,
        file_id=file_id,
        caption=caption,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('🔆彡⟨ HEROFLiX ⟩彡🔆', url=f'https://telegram.me/{CHNL_LNK}')]
        ]),
    )
    return True

async def send_shortlink_page(client, user_id, file_id, chat_id):
    files = await get_file_details(file_id)
    if not files:
        return False
    file = files[0]
    title = file_title(file)

    try:
        short_url = await get_shortlink(
            chat_id,
            f"https://telegram.me/{temp.U_NAME}?start=short_{chat_id}_{file_id}",
            client=client,
        )
    except Exception:
        logger.exception("Shortlink generation failed for %s", file_id)
        return None

    msg = await client.send_message(
        chat_id=user_id,
        text=f'<b>🔆 [ {get_size(file.file_size)} ] <a href="https://telegram.me/{CHNL_LNK}">{escape(title)}</a>\n\n📥 Download Link↓\n{short_url}</b>',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("♻️ Download Link ♻️", url=short_url)],
            [TUTORIAL_BUTTON],
        ]),
        parse_mode=enums.ParseMode.HTML,
    )
    asyncio.create_task(delete_later(msg))
    return True

@Client.on_message(filters.command("start") & filters.incoming)
async def start(client, message):

    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        if not await is_group_connected(message.chat.id):
            return

        buttons = [[
            TUTORIAL_BUTTON
        ]]

        return await message.reply(
            START_TXT,
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )

    if not message.from_user:
        return

    ban_status = await db.get_ban_status(message.from_user.id)
    if ban_status.get("is_banned"):
        return await message.reply_text(
            "<b>🚫 You are banned from using this bot\n"
            f'📝 Reason: {escape(str(ban_status.get("ban_reason", "No Reason")))}</b>'
        )

    if not await db.is_user_exist(message.from_user.id):
        await db.add_user(
            message.from_user.id,
            message.from_user.first_name
        )
        user = message.from_user
        profile = f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"
        try:
            await client.send_message(
                LOG_CHANNEL,
                f'#NewUser\nID - <code>{user.id}</code>\nNᴀᴍᴇ - <a href="{profile}">{escape(user.first_name or "User")}</a>',
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    start_buttons = [
        [
            InlineKeyboardButton(
                "🌟 Paid (No Ads)",
                url="https://telegram.me/HeroFlixx/49"
            ),
            InlineKeyboardButton(
                "🍿 Free (With Ads)",
                url="https://telegram.me/addlist/X5k2lnJLIGAyZjQ1"
            )
        ],
        [
            InlineKeyboardButton(
                "👤 Admin",
                url=f"https://telegram.me/{SUPPORT_CHAT}"
            ),
            InlineKeyboardButton(
                "⚜ Updates",
                url=FORCE
            )
        ]
    ]

    if len(message.command) != 2:
        return await message.reply_photo(
            photo=PICS,
            caption=START_TXT,
            reply_markup=InlineKeyboardMarkup(start_buttons),
            parse_mode=enums.ParseMode.HTML,
        )

    data = message.command[1]

    if AUTH_CHANNEL and not await is_subscribed(client, message):
        retry = f"https://telegram.me/{temp.U_NAME}?start={data}"

        return await client.send_message(
            message.from_user.id,
            "<b>🔆 First Join Our Main Channel &amp; Click Try Again ♻</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏮 Main Channel ⟨Click Here⟩ 🏮", url=FORCE)],
                [InlineKeyboardButton("🔄 Try Again", url=retry)],
            ]),
            parse_mode=enums.ParseMode.HTML,
        )

    if data in {"subscribe", "error", "okay", "help"}:
        return await message.reply_photo(
            photo=PICS,
            caption=START_TXT,
            reply_markup=InlineKeyboardMarkup(start_buttons),
            parse_mode=enums.ParseMode.HTML,
        )

    parts = data.split("_", 2)
    if len(parts) != 3 or parts[0] not in {"files", "short"}:
        return
    pre, payload_chat_id, file_id = parts

    cache = await db.get_cache(f"file:{payload_chat_id}:{file_id}")
    chat_id = cache.get("chat_id") if cache else None
    if chat_id is None:
        return await message.reply_text(
            "<b>⌛ Link Expired, Search Again in Group!</b>"
        )

    if pre == "short":
        if not await send_file_to_user(client, message.from_user.id, file_id):
            await message.reply("<b>❌ No such file exists</b>")
        return

    settings = await get_settings(chat_id)

    if settings.get("is_shortlink", IS_SHORTLINK):
        result = await send_shortlink_page(
            client,
            message.from_user.id,
            file_id,
            chat_id
        )

        if result is None:
            return await message.reply(
                "<b>❌ Link generation failed. Please try again later</b>"
            )

        if result is False:
            return await message.reply(
                "<b>❌ No such file exists</b>"
            )

        return

    if not await send_file_to_user(
        client,
        message.from_user.id,
        file_id
    ):
        await message.reply("<b>❌ No such file exists</b>")
