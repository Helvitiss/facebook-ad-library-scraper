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
import re

from src.core.config import config_instance as config
from src.core.scraper import main as scraper_main
from src.core.exporter import main as exporter_main

router = Router()

def is_owner(user_id: int) -> bool: return user_id in config.OWNER_IDS
def is_admin(user_id: int) -> bool: return is_owner(user_id) or not config.ADMIN_IDS or user_id in config.ADMIN_IDS

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer(
        "👋 Привет! Я бот для парсинга Facebook Ad Library.\n\n"
        "Отправь мне ссылку, и я соберу все данные.\n\n"
        "Команды:\n/settings - Настройки\n/set <ключ> <значение> - Изменить\n/cleanup - Очистка"
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
    for d in [config.RESULTS_BASE_DIR, "Parser_Results"]:
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
    status = await message.answer("🚀 Начинаю парсинг...")
    
    try:
        await status.edit_text("🛰 Сбор данных...")
        res_dir = await scraper_main([url])
        if not res_dir: return await status.edit_text("❌ Нет результатов.")

        await status.edit_text("📥 Загрузка и транскрибация...")
        await exporter_main(res_dir)

        await status.edit_text("📦 Упаковка...")
        exp_base = Path(config.RESULTS_BASE_DIR)
        latest = max([d for d in exp_base.iterdir() if d.is_dir()], key=lambda d: d.stat().st_mtime)
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
        logger.exception("Bot error")
        await message.answer(f"❌ Ошибка: {e}")
