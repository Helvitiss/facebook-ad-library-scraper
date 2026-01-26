from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile
from loguru import logger
from pathlib import Path
from datetime import datetime
import shutil
import os
import json
import html
import asyncio
import re

from src.core.config import config_instance as config
from src.core.scraper import main as scraper_main
from src.core.exporter import main as exporter_main

router = Router()
queue = asyncio.Queue()

def is_owner(user_id: int) -> bool: return user_id in config.data.telegram.owner_ids
def is_admin(user_id: int) -> bool: return is_owner(user_id) or not config.data.telegram.admin_ids or user_id in config.data.telegram.admin_ids

class TelegramLogHandler:
    def __init__(self, message: Message):
        self.message = message
        self.loop = asyncio.get_running_loop()
        self.last_text = ""
        
    def write(self, log_record):
        # We need to run async edit in the event loop from the sync loguru call
        asyncio.run_coroutine_threadsafe(self.update_message(log_record), self.loop)

    async def update_message(self, log_record):
        try:
            # Clean ANSI codes if any (though loguru usually strips them for sinks unless requested)
            text = log_record
            clean_text = re.sub(r'<.*?>', '', text).strip() # Simple strip just in case
            
            # Extract level name often formatted like "INFO     | Message"
            # But here log_record is the formatted string from loguru
            
            emoji = "📝"
            if "INFO" in clean_text: emoji = "ℹ️"
            elif "SUCCESS" in clean_text: emoji = "✅"
            elif "WARNING" in clean_text: emoji = "⚠️"
            elif "ERROR" in clean_text: emoji = "❌"
            elif "DEBUG" in clean_text: emoji = "🐞"
            
            # Simple formatting: remove timestamp/level for the chat message to keep it short
            # Regex to match default format "{time} | {level} | {message}"
            # Look for the last pipe
            parts = clean_text.split('|')
            if len(parts) > 1:
                content = parts[-1].strip()
            else:
                content = clean_text

            new_text = f"{emoji} {content}"
            
            # Debounce: avoid editing if text hasn't changed meaningfully
            if new_text == self.last_text: return
            self.last_text = new_text

            # Aiogram message edit
            await self.message.edit_text(new_text)
        except Exception:
            pass # Ignore errors during log update to not crash the main process

async def worker():
    logger.info("Task queue worker started")
    while True:
        message, url = await queue.get()
        try:
            await process_task(message, url)
        except Exception as e:
            logger.exception(f"Worker process error: {e}")
        finally:
            queue.task_done()

async def process_task(message: Message, url: str):
    status = await message.answer("🚀 Запуск обработки (из очереди)...")
    
    # Setup Log Handler
    tg_handler = TelegramLogHandler(status)
    sink_id = logger.add(
        tg_handler.write,
        format="{level} | {message}", # Simple format for parsing
        level="INFO",
        filter=lambda record: "aiogram" not in record["name"]
    )
    
    try:
        await status.edit_text("🛰 Сбор данных...")
        res_dir = await scraper_main([url])
        
        if not res_dir:
            return await status.edit_text("❌ Нет результатов.")

        await status.edit_text("📥 Загрузка и транскрибация...")
        await exporter_main(res_dir)

        await status.edit_text("📦 Упаковка...")
        exp_base = Path(config.data.exporter.results_base_dir)
        subdirs = [d for d in exp_base.iterdir() if d.is_dir()]
        
        # Determine strict latest logic using modification time
        if not subdirs:
             return await status.edit_text("❌ Ошибка экспорта (нет папок).")
        
        latest = max(subdirs, key=lambda d: d.stat().st_mtime)
        zip_name = f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.make_archive(zip_name, 'zip', latest)
        zip_file = zip_name + ".zip"

        await status.edit_text("📤 Отправка...")
        await message.reply_document(document=FSInputFile(zip_file), caption=f"✅ Готово для:\n{url}")
        
        try:
            os.remove(zip_file)
            shutil.rmtree(res_dir)
            shutil.rmtree(latest)
        except: pass
        await status.delete()
        
    except Exception as e:
        logger.exception(f"Task processing error for {url}")
        await message.answer(f"❌ Ошибка обработки: {e}")
    finally:
        # Remove the sink so subsequent logs don't try to edit this old message
        logger.remove(sink_id)

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer(
        "👋 Бот готов. Отправь ссылку Facebook Ad Library.\n"
        "Задачи ставятся в очередь во избежание перегрузки."
    )

@router.message(Command("settings"))
async def cmd_settings(message: Message):
    if not is_admin(message.from_user.id): return
    is_owner_user = is_owner(message.from_user.id)
    with open(config.path, "r", encoding="utf-8") as f: cfg = json.load(f)
    def mask(t): return f"{t[:4]}...{t[-4:]}" if t and len(t) > 10 else "********"
    text = "⚙️ <b>Настройки:</b>\n\n"
    for sec, data in cfg.items():
        if not is_owner_user and sec in ["telegram", "facebook_api"]: continue
        text += f"<b>[{sec.upper()}]</b>\n"
        if isinstance(data, dict):
            for k, v in data.items():
                if not is_owner_user and f"{sec}.{k}" == "telegram.token": v = mask(v)
                text += f"• <code>{sec}.{k}</code>: {html.escape(str(v))}\n"
        else: text += f"• <code>{sec}</code>: {html.escape(str(data))}\n"
        text += "\n"
    await message.answer(text, parse_mode="HTML")

@router.message(Command("set"))
async def cmd_set(message: Message):
    if not is_admin(message.from_user.id): return
    args = message.text.split(maxsplit=2)
    if len(args) < 3: return await message.answer("Использование: `/set <ключ> <значение>`", parse_mode="Markdown")
    key, val_str = args[1], args[2]
    if any(key.startswith(p) for p in ["telegram", "facebook_api"]) and not is_owner(message.from_user.id):
        return await message.answer("❌ Нет прав.")
    try:
        try: val = json.loads(val_str)
        except: val = val_str
        with open(config.path, "r", encoding="utf-8") as f: cfg = json.load(f)
        keys = key.split('.')
        curr = cfg
        for k in keys[:-1]: curr = curr.setdefault(k, {})
        curr[keys[-1]] = val
        config.save(cfg)
        await message.answer(f"✅ <code>{key}</code> -> <code>{val}</code>", parse_mode="HTML")
    except Exception as e: await message.answer(f"❌ Ошибка: {html.escape(str(e))}", parse_mode="HTML")

@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message):
    if not is_admin(message.from_user.id): return
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
    await message.answer(f"🧹 Очищено: {count}")

@router.message(F.text.contains("facebook.com/ads/library"))
async def handle_url(message: Message):
    if not is_admin(message.from_user.id): return
    url = message.text.strip()
    
    q_size = queue.qsize()
    await queue.put((message, url))
    
    if q_size > 0:
        await message.answer(f"⏳ Добавлено в очередь. Перед вами задач: {q_size}")
    else:
        await message.answer("⏳ Добавлено в очередь. Скоро начнем...")

async def start_worker():
    asyncio.create_task(worker())
