import json
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from src.core.config import config_instance as config
from src.bot.keyboards import (
    get_main_settings_kb, get_section_kb, get_list_menu_kb, 
    get_list_remove_kb, get_cancel_kb, SECTION_ALIASES, KEY_ALIASES
)

router = Router()

class SettingsState(StatesGroup):
    """Состояния FSM для процесса редактирования настроек."""
    waiting_for_value = State()
    waiting_for_list_add = State()

def is_owner(user_id: int) -> bool: 
    return user_id in (config.data.telegram.owner_ids or [])

def is_user(user_id: int) -> bool: 
    return is_owner(user_id) or not config.data.telegram.user_ids or user_id in config.data.telegram.user_ids

@router.callback_query(F.data == "settings_home")
async def cb_settings_home(callback: CallbackQuery, state: FSMContext):
    """Возвращает пользователя в главное меню настроек."""
    await state.clear()
    await callback.message.edit_text("Настройки:\nВыберите категорию:", reply_markup=get_main_settings_kb(), parse_mode="HTML")

@router.callback_query(F.data == "close_settings")
async def cb_close_settings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()

@router.callback_query(F.data.startswith("set_sec:"))
async def cb_section(callback: CallbackQuery):
    """Обрабатывает выбор секции настроек (например, Scraper, Telegram)."""
    section = callback.data.split(":")[1]
    if section in ["telegram", "facebook_api"] and not is_owner(callback.from_user.id):
        return await callback.answer("Требуются права владельца", show_alert=True)
    
    label = SECTION_ALIASES.get(section, section.upper())
    await callback.message.edit_text(f"<b>{label}</b>\nВыберите параметр для изменения:", 
                                     reply_markup=get_section_kb(section), parse_mode="HTML")

@router.callback_query(F.data.startswith("set_edit:"))
async def cb_edit_value(callback: CallbackQuery, state: FSMContext):
    """Инициирует процесс редактирования конкретного параметра."""
    parts = callback.data.split(":")
    section, key = parts[1], ":".join(parts[2:])

    # Получаем текущее значение из загруженного конфига
    section_data = getattr(config.data, section, {})
    curr_val = getattr(section_data, key, "N/A") if hasattr(section_data, key) else "N/A"
    
    alias = KEY_ALIASES.get(key, key)
    msg_text = (
        f"Изменение <b>{alias}</b>\n"
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
    if not section: return await cb_settings_home(callback, state)
    
    label = SECTION_ALIASES.get(section, section.upper())
    await state.clear()
    await callback.message.edit_text(f"<b>{label}</b>\nВыберите параметр:", 
                                     reply_markup=get_section_kb(section), parse_mode="HTML")

@router.message(SettingsState.waiting_for_value)
async def process_new_value(message: Message, state: FSMContext):
    """Сохраняет новое значение параметра, введенное пользователем."""
    data = await state.get_data()
    section, key = data.get("section"), data.get("key")
    
    val_str = message.text.strip()
    try: val = json.loads(val_str)
    except: val = val_str
    
    try:
        cfg = config.data.model_dump()
        cfg[section][key] = val
        config.save(cfg)
        await message.answer(f"Сохранено: <code>{section}.{key}</code> = <code>{val}</code>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"Ошибка сохранения: {e}")
        
    await state.clear()

@router.callback_query(F.data.startswith("set_list_menu:"))
async def cb_list_menu(callback: CallbackQuery):
    """Отображает меню управления списком (например, user_ids)."""
    parts = callback.data.split(":")
    section, key = parts[1], parts[2]
    
    section_data = getattr(config.data, section, None)
    data = section_data.model_dump() if hasattr(section_data, 'model_dump') else (section_data or {})
    items = data.get(key, [])
    
    alias = KEY_ALIASES.get(key, key)
    text = f"<b>{alias}</b>\nТекущий список:\n"
    text += "\n".join([f"• <code>{i}</code>" for i in items]) if items else "(пусто)"
        
    await callback.message.edit_text(text, reply_markup=get_list_menu_kb(section, key), parse_mode="HTML")

@router.callback_query(F.data.startswith("set_list_add:"))
async def cb_list_add(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    section, key = parts[1], parts[2]
    await state.update_data(section=section, key=key)
    await state.set_state(SettingsState.waiting_for_list_add)
    
    alias = KEY_ALIASES.get(key, key)
    await callback.message.edit_text(f"Добавление в <b>{alias}</b>.\nВведите значение (ID или текст):", 
                                     reply_markup=get_cancel_kb(), parse_mode="HTML")

@router.message(SettingsState.waiting_for_list_add)
async def process_list_add(message: Message, state: FSMContext):
    data = await state.get_data()
    section, key = data.get("section"), data.get("key")
    val_str = message.text.strip()
    
    try: val = int(val_str)
    except: val = val_str
    
    try:
        cfg = config.data.model_dump()
        current_list = cfg[section].get(key, [])
        if val not in current_list:
            current_list.append(val)
            cfg[section][key] = current_list
            config.save(cfg)
            await message.answer(f"Добавлено: <code>{val}</code>", parse_mode="HTML")
        else:
            await message.answer(f"Значение уже есть в списке.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        
    await state.clear()

@router.callback_query(F.data.startswith("set_list_rm_menu:"))
async def cb_list_rm_menu(callback: CallbackQuery):
    parts = callback.data.split(":")
    section, key = parts[1], parts[2]
    
    section_data = getattr(config.data, section, None)
    data = section_data.model_dump() if hasattr(section_data, 'model_dump') else (section_data or {})
    items = data.get(key, [])
    
    if not items:
        return await callback.answer("Список пуст", show_alert=True)
        
    alias = KEY_ALIASES.get(key, key)
    await callback.message.edit_text(f"Удаление из <b>{alias}</b>.\nНажмите, чтобы удалить:", 
                                     reply_markup=get_list_remove_kb(section, key, items), parse_mode="HTML")

@router.callback_query(F.data.startswith("set_list_rm:"))
async def cb_list_rm(callback: CallbackQuery):
    parts = callback.data.split(":") 
    section, key, val_str = parts[1], parts[2], parts[3]
    
    try: val = int(val_str)
    except: val = val_str
    
    try:
        cfg = config.data.model_dump()
        current_list = cfg[section].get(key, [])
        if val in current_list:
            current_list.remove(val)
            cfg[section][key] = current_list
            config.save(cfg)
            await cb_list_rm_menu(callback) 
        else:
            await callback.answer("Уже удалено", show_alert=True)
            await cb_list_rm_menu(callback)
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)
