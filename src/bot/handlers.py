import asyncio
import re
from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from loguru import logger

from src.core.config import config_instance as config
from src.bot.utils import process_task, process_kw_task
from src.bot.settings import router as settings_router, is_user
from src.bot.keyboards import get_main_settings_kb, get_cancel_kb
from src.bot.states import KWState
from aiogram.fsm.context import FSMContext

router = Router()
router.include_router(settings_router)

queue = asyncio.Queue()
worker_tasks: list[asyncio.Task] = []
worker_limit = 1
worker_semaphore = asyncio.Semaphore(1)

async def worker():
    """Фоновый воркер, обрабатывающий очередь задач (URL или список URL)."""
    logger.info("Task queue worker started")
    while True:
        message, status_message, urls = await queue.get()
        try:
            async with worker_semaphore:
                await process_task(message, status_message, urls)
        except Exception as e:
            logger.exception(f"Worker process error: {e}")
        finally:
            queue.task_done()

def _target_worker_limit() -> int:
    if config.data.debug_mode:
        return max(1, int(getattr(config.data.scraper, "url_workers", 1) or 1))
    return 1

async def _reduce_tokens(count: int):
    for _ in range(count):
        await worker_semaphore.acquire()

async def set_worker_limit(target: int):
    """Динамически меняет лимит параллельных воркеров."""
    global worker_limit
    target = max(1, int(target or 1))
    if target > len(worker_tasks):
        for _ in range(target - len(worker_tasks)):
            worker_tasks.append(asyncio.create_task(worker()))

    diff = target - worker_limit
    if diff > 0:
        for _ in range(diff):
            worker_semaphore.release()
    elif diff < 0:
        asyncio.create_task(_reduce_tokens(-diff))

    worker_limit = target

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start. Приветствие и инструкции."""
    if not is_user(message.from_user.id): return
    help_text = (
        "<b>Facebook Ad Library Parser</b>\n\n"
        "Бот для сбора и анализа рекламных объявлений.\n\n"
        "<b>Как использовать:</b>\n"
        "1. Отправьте ссылку на библиотеку рекламы (facebook.com/ads/library/...)\n"
        "2. Бот соберет креативы и статистику.\n"
        "3. В ответ вы получите ZIP-архив с результатами.\n\n"
        "<b>Доступные команды:</b>\n"
        "/settings - Настройки (потоки, фильтры, доступы)\n"
        "/debug - Вкл/выкл быстрый отладочный режим\n"
        "/cleanup - Очистка временных файлов на сервере\n"
        "/start - Показать это сообщение"
    )
    await message.answer(help_text, parse_mode="HTML")

@router.message(Command("debug"))
async def cmd_debug(message: Message):
    """Обработчик команды /debug. Переключает режим быстрой отладки."""
    if not is_user(message.from_user.id): return
    
    current_state = config.data.debug_mode
    config.data.debug_mode = not current_state
    
    # Сохраняем изменения в файл
    if config.save(config.data):
        await set_worker_limit(_target_worker_limit())
        new_state_str = "ВКЛЮЧЕН 🟢 (Лимит 50 объявлений, только проверка RSOC-ключей)" if config.data.debug_mode else "ВЫКЛЮЧЕН 🔴 (Полный масштабный парсинг)"
        await message.answer(f"Отладочный режим <b>{new_state_str}</b>.", parse_mode="HTML")
    else:
        # Откат в случае ошибки сохранения
        config.data.debug_mode = current_state
        await message.answer("❌ Ошибка при сохранении конфигурации.")

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

def _extract_ad_library_urls(text: str) -> list[str]:
    """Извлекает и нормализует ссылки на Facebook Ad Library из текста."""
    raw = text or ""
    tokens = re.split(r"\s+", raw)
    candidates: list[str] = []

    # 1) Ссылки с протоколом
    candidates.extend(re.findall(r"https?://[^\s<>()]+", raw))
    # 2) Ссылки без протокола (facebook.com/ads/library/...)
    candidates.extend([t for t in tokens if "facebook.com/ads/library" in t.lower()])

    seen = set()
    result = []
    for url in candidates:
        cleaned = url.strip().strip("<>[](){}").rstrip(".,;)\"'")
        if "facebook.com/ads/library" not in cleaned.lower():
            continue
        if not cleaned.lower().startswith("http"):
            cleaned = "https://" + cleaned
        cleaned = cleaned.replace("&amp;", "&")
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _validate_ad_library_url(url: str) -> tuple[bool, str]:
    """Базовая валидация URL, чтобы отсеять обрезанные ссылки до запуска парсинга."""
    from urllib.parse import urlparse, parse_qs

    try:
        parsed = urlparse(url)
        if "facebook.com" not in parsed.netloc.lower() or "/ads/library/" not in parsed.path.lower():
            return False, "не похожа на ссылку Facebook Ad Library"

        # Частый симптом обрезки: sort_data[directi... без полного ключа direction]
        lowered = url.lower()
        if "sort_data[directi" in lowered and "sort_data[direction]" not in lowered:
            return False, "ссылка обрезана в параметре sort_data[direction]"

        qs = parse_qs(parsed.query)
        if not qs:
            return False, "нет query-параметров"
        return True, ""
    except Exception as e:
        return False, f"ошибка разбора URL: {e}"

@router.message(F.text.contains("facebook.com/ads/library"))
async def handle_url(message: Message):
    """Перехватчик ссылок на Facebook Ad Library. Добавляет задачу в очередь."""
    if not is_user(message.from_user.id): return
    urls = _extract_ad_library_urls(message.text or "")
    if not urls:
        return await message.answer("Не удалось найти корректные ссылки на Facebook Ad Library.")
    
    valid_urls: list[str] = []
    invalid_reasons: list[str] = []
    for u in urls:
        ok, reason = _validate_ad_library_url(u)
        if ok:
            valid_urls.append(u)
        else:
            invalid_reasons.append(f"{u} ({reason})")

    if not valid_urls:
        preview = "\n".join(invalid_reasons[:2])
        return await message.answer(
            "Ссылка выглядит некорректной или обрезанной. Отправьте полный URL Facebook Ad Library одной строкой.\n"
            f"Пример проблемы:\n{preview}"
        )
    urls = valid_urls

    q_size = queue.qsize()
    count = len(urls)
    initial_text = (
        f"Добавлено {count} ссылок в очередь. Перед вами задач: {q_size}" if q_size > 0
        else f"Добавлено {count} ссылок в очередь. Скоро начнем..."
    )
    status = await message.answer(initial_text)
    
    await queue.put((message, status, urls))

@router.message(Command("kw"))
async def cmd_kw(message: Message, state: FSMContext):
    """Обработчик команды /kw. Запрашивает поиск ключевых слов."""
    if not is_user(message.from_user.id): return
    
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        # Если URL передан сразу
        url = args[1].strip()
        await process_kw_task(message, url)
    else:
        # Если URL не передан, запрашиваем через FSM
        await state.set_state(KWState.waiting_for_url)
        await message.answer(
            "Отправьте ссылку на сайт, из которого нужно извлечь ключевые слова.",
            reply_markup=get_cancel_kb()
        )

@router.message(F.text.regexp(r'(?i)^kw\s+(https?://\S+)'))
async def handle_kw_text(message: Message):
    """Перехватчик сообщений вида 'KW http...'."""
    if not is_user(message.from_user.id): return
    match = re.search(r'kw\s+(https?://\S+)', message.text, re.I)
    if match:
        url = match.group(1).strip()
        await process_kw_task(message, url)

@router.message(KWState.waiting_for_url)
async def process_kw_url_step(message: Message, state: FSMContext):
    """Шаг FSM: получение URL для извлечения KW."""
    if not is_user(message.from_user.id): return
    
    url = message.text.strip()
    if not url.startswith("http"):
        return await message.answer("Пожалуйста, отправьте корректную ссылку (начинающуюся с http/https).")
    
    await state.clear()
    await process_kw_task(message, url)

async def start_worker():
    """Запускает асинхронный воркер обработки очереди."""
    await set_worker_limit(_target_worker_limit())
