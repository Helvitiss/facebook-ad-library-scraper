import asyncio
from aiogram import Bot, Dispatcher
from loguru import logger
import sys

# Импорт конфигурации и обработчиков
from src.core.config import config_instance as config
from src.bot.handlers import router, start_worker

async def main():
    # Настройка логирования
    logger.remove()
    logger.add(sys.stdout, colorize=True, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")
    
    if not config.data.telegram.token:
        logger.error("Telegram token not found in config.json")
        return

    bot = Bot(token=config.data.telegram.token)
    
    # Используем MemoryStorage для FSM и включаем параллельную обработку
    from aiogram.fsm.storage.memory import MemoryStorage
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    
    # Запуск фонового воркера для очереди задач
    await start_worker()

    logger.info("Bot started!")
    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user")
