import asyncio
import logging
import sys
from datetime import date, datetime

# Logging (replaces logging.conf): INFO and up on the console, ERROR and up in TelegramBot.log.
# The /logs command sends TelegramBot.log, so keep that file name.
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

import pytz
from aiohttp import web
from pyrogram import Client, __version__
from pyrogram.raw.all import layer

from info import *
from plugins import web_server
from plugins.index import Media, check_pending_index_on_startup
from utils import close_http_session, db, temp

OWNER_ID = ADMINS[0]
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
        tz = pytz.timezone('Asia/Kolkata')
        today = date.today()
        now = datetime.now(tz)
        time = now.strftime("%H:%M:%S %p")

        manual_restart = await db.consume_restart_flag()

        if manual_restart:
            try:
                restart_msg = await self.send_message(
                    chat_id=OWNER_ID,
                    text=RESTART_TXT.format(today, time),
                )

                async def delete_restart_notification():
                    await asyncio.sleep(10)
                    try:
                        await restart_msg.delete()
                    except Exception:
                        pass

                asyncio.create_task(delete_restart_notification())
            except Exception:
                logging.exception("Failed to send restart notification to owner")
        else:
            try:
                await self.send_message(
                    chat_id=LOG_CHANNEL,
                    text=RESTART_TXT.format(today, time),
                )
            except Exception:
                logging.exception("Failed to send startup notification to log channel")

        app = web.AppRunner(await web_server())
        await app.setup()
        bind_address = "0.0.0.0"
        await web.TCPSite(app, bind_address, PORT).start()
        
        # Trigger check for pending index tasks right after bot starts up successfully
        await check_pending_index_on_startup(self)

    async def stop(self, *args):
        await close_http_session()
        await super().stop()
        logging.info("Bot stopped. Bye.")


app = Bot()
app.run()
