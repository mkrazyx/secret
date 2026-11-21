import asyncio
import logging
import sys

from aiohttp import web
from pyrogram import Client, __version__
from pyrogram.raw.all import layer

from info import *
from plugins.index import check_pending_index_on_startup
from utils import close_http_session, db, delete_later, temp

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

async def health(request):
    return web.json_response("BenFilterBot")

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
        await super().start()
        me = await self.get_me()
        temp.U_NAME = me.username
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

        web_app = web.Application()
        web_app.router.add_get("/", health)
        runner = web.AppRunner(web_app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        await check_pending_index_on_startup(self)

    async def stop(self, *args):
        await close_http_session()
        await super().stop()
        logging.info("Bot stopped. Bye.")

app = Bot()
app.run()
