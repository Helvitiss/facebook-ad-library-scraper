from aiogram import Router, types, F
from aiogram.filters import Command, StateFilter, CommandStart
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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

# --- Helpers ---
def is_owner(user_id: int) -> bool: return user_id in config.data.telegram.owner_ids
def is_user(user_id: int) -> bool: return is_owner(user_id) or not config.data.telegram.user_ids or user_id in config.data.telegram.user_ids

# --- FSM States ---
class SettingsState(StatesGroup):
    waiting_for_value = State()
    waiting_for_list_add = State()

# --- Constants & Aliases ---
KEY_ALIASES = {
    # Scraper
    "concurrent_requests": "Потоки (запросы)",
    "retries_per_creative": "Попыток на креатив",
    "url_workers": "Потоки (URL)",
    "proxy_url": "Прокси (Scraper)",
    "proxy_change_url": "URL смены IP",
    # Exporter
    "max_retries": "Попыток скачивания",
    "exporter_workers": "Потоки экспорта",
    "results_base_dir": "Папка результатов",
    "retry_delay_seconds": "Задержка (сек)",
    "min_reaches": "Мин. охват (Total)",
    "min_reaches_eu": "Мин. охват (EU)",
    "min_reaches_uk": "Мин. охват (UK)",
    "video_filename": "Имя видео",
    "image_filename": "Имя фото",
    # Telegram
    "token": "Токен бота",
    "user_ids": "Пользователи",
    "owner_ids": "Владельцы",
    # Facebook API
    "endpoint_url": "API Endpoint",
    "doc_ids": "Doc IDs (System)",
    "pagination": "DocID: Pagination",
    "collation": "DocID: Collation",
    "creative_info": "DocID: Creative"
}

SECTION_ALIASES = {
    "scraper": "🕷 Парсер",
    "exporter": "📦 Экспортер",
    "telegram": "✈️ Telegram",
    "facebook_api": "⚙️ Facebook API"
}

# --- Keyboards ---
def get_main_settings_kb():
    sections = ["scraper", "exporter", "telegram", "facebook_api"]
    buttons = []
    for sec in sections:
        label = SECTION_ALIASES.get(sec, sec.upper())
        buttons.append([InlineKeyboardButton(text=f"{label}", callback_data=f"set_sec:{sec}")])
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="close_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_section_kb(section: str):
    with open(config.path, "r", encoding="utf-8") as f: cfg = json.load(f)
    
    data = cfg.get(section, {})
    buttons = []
    
    def fmt_val(v):
        s = str(v)
        # Show only start of long values (like urls)
        return f"{s[:15]}..." if len(s) > 15 else s

    for k, v in data.items():
        if isinstance(v, dict): continue # Skip nested
        
        # Determine button text
        alias = KEY_ALIASES.get(k, k)
        val_str = fmt_val(v)
        
        # Format: "Alias: Value"
        btn_text = f"{alias}: {val_str}"
        
        # Use different callback for lists
        if isinstance(v, list):
             buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"set_list_menu:{section}:{k}")])
        else:
             buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"set_edit:{section}:{k}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="settings_home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_list_menu_kb(section: str, key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data=f"set_list_add:{section}:{key}")],
        [InlineKeyboardButton(text="➖ Удалить", callback_data=f"set_list_rm_menu:{section}:{key}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"set_sec:{section}")]
    ])

def get_list_remove_kb(section: str, key: str, items: list):
    buttons = []
    for item in items:
        # Each button removes one item
        buttons.append([InlineKeyboardButton(text=f"🗑 {item}", callback_data=f"set_list_rm:{section}:{key}:{item}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"set_list_menu:{section}:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Отмена", callback_data="cancel_edit")]])

# --- Log Handler (Kept same) ---
class TelegramLogHandler:
    def __init__(self, message: Message):
        self.message = message
        self.loop = asyncio.get_running_loop()
        self.last_text = ""
        
    def write(self, log_record):
        try:
            asyncio.run_coroutine_threadsafe(self.update_message(log_record), self.loop)
        except Exception:
            pass

    async def update_message(self, log_record):
        try:
            text = log_record
            clean_text = re.sub(r'<.*?>', '', text).strip()
            emoji = "📝"
            if "INFO" in clean_text: emoji = "ℹ️"
            elif "SUCCESS" in clean_text: emoji = "✅"
            elif "WARNING" in clean_text: emoji = "⚠️"
            elif "ERROR" in clean_text: emoji = "❌"
            elif "DEBUG" in clean_text: emoji = "🐞"
            
            parts = clean_text.split('|')
            content = parts[-1].strip() if len(parts) > 1 else clean_text
            new_text = f"{emoji} {content}"
            
            if new_text == self.last_text: return
            self.last_text = new_text
            await self.message.edit_text(new_text)
        except Exception:
            pass

async def worker():
    logger.info("Task queue worker started")
    while True:
        item = await queue.get()
        try:
            if len(item) == 3:
                msg, status, url = item
                await process_task(msg, status, url)
        except Exception as e:
            logger.exception(f"Worker process error: {e}")
        finally:
            queue.task_done()

async def process_task(original_message: Message, status_message: Message, url: str):
    tg_handler = TelegramLogHandler(status_message)
    sink_id = logger.add(tg_handler.write, format="{level} | {message}", level="INFO", filter=lambda r: "aiogram" not in r["name"])
    
    try:
        await status_message.edit_text("🛰 Сбор данных...")
        res_dir = await scraper_main([url])
        
        if not res_dir:
            return await status_message.edit_text("❌ Нет результатов.")

        await status_message.edit_text("📥 Загрузка и транскрибация...")
        await exporter_main(res_dir)

        await status_message.edit_text("📦 Упаковка...")
        exp_base = Path(config.data.exporter.results_base_dir)
        subdirs = [d for d in exp_base.iterdir() if d.is_dir()]
        
        if not subdirs:
             return await status_message.edit_text("❌ Ошибка экспорта (нет папок).")
        
        latest = max(subdirs, key=lambda d: d.stat().st_mtime)
        zip_name = f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.make_archive(zip_name, 'zip', latest)
        zip_file = zip_name + ".zip"

        await status_message.edit_text("📤 Отправка...")
        await original_message.reply_document(document=FSInputFile(zip_file), caption=f"✅ Готово для:\n{url}")
        
        try:
            os.remove(zip_file)
            shutil.rmtree(res_dir)
            shutil.rmtree(latest)
        except: pass
        await status_message.delete()
        
    except Exception as e:
        logger.exception(f"Task processing error for {url}")
        await original_message.answer(f"❌ Ошибка обработки: {e}")
    finally:
        logger.remove(sink_id)

# --- Handlers ---

@router.message(CommandStart())
async def cmd_start(message: Message):
    if not is_user(message.from_user.id): return
    await message.answer(
        "👋 Бот готов. Отправь ссылку Facebook Ad Library.\n"
        "Очередь активна. Настройки (только для админов/пользователей): /settings"
    )

@router.message(Command("settings"))
async def cmd_settings(message: Message):
    if not is_user(message.from_user.id): return
    await message.answer("⚙️ <b>Настройки:</b>\nВыберите категорию:", reply_markup=get_main_settings_kb(), parse_mode="HTML")

# --- Settings Callbacks ---

@router.callback_query(F.data == "settings_home")
async def cb_settings_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚙️ <b>Настройки:</b>\nВыберите категорию:", reply_markup=get_main_settings_kb(), parse_mode="HTML")

@router.callback_query(F.data == "close_settings")
async def cb_close_settings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()

@router.callback_query(F.data.startswith("set_sec:"))
async def cb_section(callback: CallbackQuery):
    section = callback.data.split(":")[1]
    is_owner_user = is_owner(callback.from_user.id)
    if section in ["telegram", "facebook_api"] and not is_owner_user:
        return await callback.answer("🔒 Требуются права владельца", show_alert=True)
    
    label = SECTION_ALIASES.get(section, section.upper())
    await callback.message.edit_text(f"📂 <b>{label}</b>\nВыберите параметр для изменения:", 
                                     reply_markup=get_section_kb(section), parse_mode="HTML")

# --- Value Editor ---

@router.callback_query(F.data.startswith("set_edit:"))
async def cb_edit_value(callback: CallbackQuery, state: FSMContext):
    try: _, section, key = callback.data.split(":")
    except ValueError: parts = callback.data.split(":"); section = parts[1]; key = ":".join(parts[2:])

    with open(config.path, "r", encoding="utf-8") as f: cfg = json.load(f)
    curr_val = cfg.get(section, {}).get(key, "N/A")
    
    alias = KEY_ALIASES.get(key, key)
    msg_text = (
        f"📝 Изменение <b>{alias}</b>\n"
        f"(<code>{key}</code>)\n"
        f"Текущее значение: <code>{curr_val}</code>\n\n"
        "Отправьте новое значение сообщением (или нажмите Отмена)."
    )
    
    await state.update_data(section=section, key=key)
    await state.set_state(SettingsState.waiting_for_value)
    await callback.message.edit_text(msg_text, reply_markup=get_cancel_kb(), parse_mode="HTML")

@router.callback_query(F.data == "cancel_edit")
async def cb_cancel_edit(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    section = data.get("section")
    # if list op check ->
    if not section: return await cb_settings_home(callback, state)
    
    # If returned from list menu context, might need specific key
    # But usually cancel just goes back to section
    label = SECTION_ALIASES.get(section, section.upper())
    await state.clear()
    await callback.message.edit_text(f"📂 <b>{label}</b>\nВыберите параметр:", 
                                     reply_markup=get_section_kb(section), parse_mode="HTML")

@router.message(SettingsState.waiting_for_value)
async def process_new_value(message: Message, state: FSMContext):
    data = await state.get_data()
    section = data.get("section")
    key = data.get("key")
    
    val_str = message.text.strip()
    try: val = json.loads(val_str)
    except: val = val_str
    
    try:
        with open(config.path, "r", encoding="utf-8") as f: cfg = json.load(f)
        cfg[section][key] = val
        config.save(cfg)
        await message.answer(f"✅ Сохранено: <code>{section}.{key}</code> = <code>{val}</code>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка сохранения: {e}")
        
    await state.clear()

# --- List Management ---

@router.callback_query(F.data.startswith("set_list_menu:"))
async def cb_list_menu(callback: CallbackQuery):
    _, section, key = callback.data.split(":")
    with open(config.path, "r", encoding="utf-8") as f: cfg = json.load(f)
    items = cfg.get(section, {}).get(key, [])
    
    alias = KEY_ALIASES.get(key, key)
    text = f"📋 <b>{alias}</b>\nТекущий список:\n"
    if items:
        text += "\n".join([f"• <code>{i}</code>" for i in items])
    else:
        text += "(пусто)"
        
    await callback.message.edit_text(text, reply_markup=get_list_menu_kb(section, key), parse_mode="HTML")

@router.callback_query(F.data.startswith("set_list_add:"))
async def cb_list_add(callback: CallbackQuery, state: FSMContext):
    _, section, key = callback.data.split(":")
    await state.update_data(section=section, key=key)
    await state.set_state(SettingsState.waiting_for_list_add)
    
    alias = KEY_ALIASES.get(key, key)
    await callback.message.edit_text(f"➕ Добавление в <b>{alias}</b>.\nВведите значение (ID или текст):", 
                                     reply_markup=get_cancel_kb(), parse_mode="HTML")

@router.message(SettingsState.waiting_for_list_add)
async def process_list_add(message: Message, state: FSMContext):
    data = await state.get_data()
    section, key = data.get("section"), data.get("key")
    val_str = message.text.strip()
    
    try: val = int(val_str) # Try int first for IDs
    except: val = val_str
    
    try:
        with open(config.path, "r", encoding="utf-8") as f: cfg = json.load(f)
        current_list = cfg[section].get(key, [])
        if val not in current_list:
            current_list.append(val)
            cfg[section][key] = current_list
            config.save(cfg)
            await message.answer(f"✅ Добавлено: <code>{val}</code>", parse_mode="HTML")
        else:
            await message.answer(f"⚠️ Значение уже есть в списке.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        
    await state.clear()

@router.callback_query(F.data.startswith("set_list_rm_menu:"))
async def cb_list_rm_menu(callback: CallbackQuery):
    _, section, key = callback.data.split(":")
    with open(config.path, "r", encoding="utf-8") as f: cfg = json.load(f)
    items = cfg.get(section, {}).get(key, [])
    
    if not items:
        return await callback.answer("Список пуст", show_alert=True)
        
    alias = KEY_ALIASES.get(key, key)
    await callback.message.edit_text(f"➖ Удаление из <b>{alias}</b>.\nНажмите, чтобы удалить:", 
                                     reply_markup=get_list_remove_kb(section, key, items), parse_mode="HTML")

@router.callback_query(F.data.startswith("set_list_rm:"))
async def cb_list_rm(callback: CallbackQuery):
    parts = callback.data.split(":") 
    # format: set_list_rm:section:key:value
    section, key = parts[1], parts[2]
    val_str = parts[3]
    
    try: val = int(val_str)
    except: val = val_str
    
    try:
        with open(config.path, "r", encoding="utf-8") as f: cfg = json.load(f)
        current_list = cfg[section].get(key, [])
        if val in current_list:
            current_list.remove(val)
            cfg[section][key] = current_list
            config.save(cfg)
            # Update the menu immediately
            await cb_list_rm_menu(callback) 
        else:
            await callback.answer("Уже удалено", show_alert=True)
            await cb_list_rm_menu(callback)
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)

@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message):
    if not is_user(message.from_user.id): return
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
    if not is_user(message.from_user.id): return
    url = message.text.strip()
    
    q_size = queue.qsize()
    initial_text = f"⏳ Добавлено в очередь. Перед вами задач: {q_size}" if q_size > 0 else "⏳ Добавлено в очередь. Скоро начнем..."
    status = await message.answer(initial_text)
    
    await queue.put((message, status, url))

async def start_worker():
    asyncio.create_task(worker())
