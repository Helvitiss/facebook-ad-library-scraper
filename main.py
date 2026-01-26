import asyncio
from aiogram import Bot, Dispatcher
from loguru import logger
import sys

# Ensure src is in path if running from root
from src.core.config import config_instance as config
from src.bot.handlers import router, start_worker

async def main():
    logger.remove()
    logger.add(sys.stdout, colorize=True, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")
    
    if not config.data.telegram.token:
        logger.error("Telegram token not found in config.json")
        return

    bot = Bot(token=config.data.telegram.token)
    dp = Dispatcher()
    dp.include_router(router)
    
    # Start the background task worker
    await start_worker()

    logger.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
