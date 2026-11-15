import asyncio
import logging
import sys

from aiohttp import web
from pyrogram import Client, __version__
from pyrogram.raw.all import layer

from info import *
from plugins import web_server
from plugins.index import Media, check_pending_index_on_startup
from utils import close_http_session, db, temp

console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter(
    "%(asctime)s - %(lineno)d - %(name)s - %(module)s - %(levelname)s - %(message)s", "%I:%M:%S %p"))
errors_file = logging.FileHandler("TelegramBot.log", "w")
errors_file.setLevel(logging.ERROR)
errors_file.setFormatter(logging.Formatter(
    "[%(asctime)s:%(name)s:%(lineno)d:%(levelname)s] %(message)s", "%m/%d/%Y %I:%M:%S %p"))
logging.basicConfig(level=logging.INFO, handlers=[console, errors_file], force=True)
logging.getLogger("pyrogram").setLevel(logging.ERROR)

async def delete_later(message, seconds):
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception:
        pass

class Bot(Client):

    def __init__(self):
        super().__init__(
            name=SESSION,
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            workers=20,
            plugins={"root": "plugins"},
            sleep_threshold=5,
        )

    async def start(self):
        await db.ensure_cache_indexes()
        temp.BANNED_USERS = await db.get_banned_users()
        await super().start()
        await Media.ensure_indexes()
        me = await self.get_me()
        temp.ME = me.id
        temp.U_NAME = me.username
        temp.B_NAME = me.first_name
        self.username = '@' + me.username
        logging.info(f"{me.first_name} with for Pyrogram v{__version__} (Layer {layer}) started on {me.username}.")
        logging.info(LOGO)

        manual_restart = await db.consume_restart_flag()
        try:
            restart_msg = await self.send_message(OWNER_ID, "♻️ Bot Restarted!")
        except Exception:
            logging.exception("Failed to send startup notification to owner")
        else:
            if manual_restart:
                asyncio.create_task(delete_later(restart_msg, 10))

        app = web.AppRunner(await web_server())
        await app.setup()
        await web.TCPSite(app, "0.0.0.0", PORT).start()
        await check_pending_index_on_startup(self)

    async def stop(self, *args):
        await close_http_session()
        await super().stop()
        logging.info("Bot stopped. Bye.")

app = Bot()
app.run()
