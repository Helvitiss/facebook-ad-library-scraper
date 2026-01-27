import asyncio
from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from loguru import logger

from src.core.config import config_instance as config
from src.bot.utils import process_task
from src.bot.settings import router as settings_router, is_user
from src.bot.keyboards import get_main_settings_kb

router = Router()
router.include_router(settings_router)

queue = asyncio.Queue()

async def worker():
    """Фоновый воркер, обрабатывающий очередь задач (URL)."""
    logger.info("Task queue worker started")
    while True:
        message, status_message, url = await queue.get()
        try:
            await process_task(message, status_message, url)
        except Exception as e:
            logger.exception(f"Worker process error: {e}")
        finally:
            queue.task_done()

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start. Приветствие и инструкции."""
    if not is_user(message.from_user.id): return
    help_text = (
        "<b>Facebook Ad Library Parser</b>\n\n"
        "Бот для сбора и анализа рекламных объявлений.\n\n"
        "<b>Как использовать:</b>\n"
        "1. Отправьте ссылку на библиотеку рекламы (facebook.com/ads/library/...)\n"
        "2. Бот соберет креативы, статистику и транскрибирует видео.\n"
        "3. В ответ вы получите ZIP-архив с результатами.\n\n"
        "<b>Доступные команды:</b>\n"
        "/settings - Настройки (потоки, фильтры, доступы)\n"
        "/cleanup - Очистка временных файлов на сервере\n"
        "/start - Показать это сообщение"
    )
    await message.answer(help_text, parse_mode="HTML")

@router.message(Command("settings"))
async def cmd_settings(message: Message):
    """Обработчик команды /settings. Открывает меню настроек."""
    if not is_user(message.from_user.id): return
    await message.answer("Настройки:\nВыберите категорию:", reply_markup=get_main_settings_kb(), parse_mode="HTML")

@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message):
    """Служебная команда /cleanup для очистки папок с результатами."""
    if not is_user(message.from_user.id): return
    import shutil
    from pathlib import Path
    count = 0
    for d in [config.data.exporter.results_base_dir, "Parser_Results"]:
        p = Path(d)
        if p.exists():
            for item in p.iterdir():
                try:
                    if item.is_dir(): shutil.rmtree(item)
                    else: item.unlink()
                    count += 1
                except: pass
    await message.answer(f"Очищено: {count}")

@router.message(F.text.contains("facebook.com/ads/library"))
async def handle_url(message: Message):
    """Перехватчик ссылок на Facebook Ad Library. Добавляет задачу в очередь."""
    if not is_user(message.from_user.id): return
    url = message.text.strip()
    
    q_size = queue.qsize()
    initial_text = f"Добавлено в очередь. Перед вами задач: {q_size}" if q_size > 0 else "Добавлено в очередь. Скоро начнем..."
    status = await message.answer(initial_text)
    
    await queue.put((message, status, url))

async def start_worker():
    """Запускает асинхронный воркер обработки очереди."""
    asyncio.create_task(worker())
