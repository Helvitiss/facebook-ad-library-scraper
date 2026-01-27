from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from src.core.config import config_instance as config

# --- Константы и псевдонимы для настроек ---
KEY_ALIASES = {
    "concurrent_requests": "Потоки (запросы)",
    "retries_per_creative": "Попыток на креатив",
    "url_workers": "Потоки (URL)",
    "proxy_url": "Прокси (Scraper)",
    "proxy_change_url": "URL смены IP",
    "max_retries": "Попыток скачивания",
    "exporter_workers": "Потоки экспорта",
    "results_base_dir": "Папка результатов",
    "retry_delay_seconds": "Задержка (сек)",
    "min_reaches": "Мин. охват (Total)",
    "min_reaches_eu": "Мин. охват (EU)",
    "min_reaches_uk": "Мин. охват (UK)",
    "video_filename": "Имя видео",
    "image_filename": "Имя фото",
    "token": "Токен бота",
    "user_ids": "Пользователи",
    "owner_ids": "Владельцы",
    "endpoint_url": "API Endpoint",
    "doc_ids": "Doc IDs (System)",
    "pagination": "DocID: Pagination",
    "collation": "DocID: Collation",
    "creative_info": "DocID: Creative"
}

SECTION_ALIASES = {
    "scraper": "Парсер",
    "exporter": "Экспортер",
    "telegram": "Telegram",
    "facebook_api": "Facebook API"
}

def get_main_settings_kb():
    """Генерирует клавиатуру главного меню настроек."""
    sections = ["scraper", "exporter", "telegram", "facebook_api"]
    buttons = []
    for sec in sections:
        label = SECTION_ALIASES.get(sec, sec.upper())
        buttons.append([InlineKeyboardButton(text=f"{label}", callback_data=f"set_sec:{sec}")])
    buttons.append([InlineKeyboardButton(text="Закрыть", callback_data="close_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_section_kb(section: str):
    """Генерирует клавиатуру для конкретной секции настроек (список параметров)."""
    section_data = getattr(config.data, section, None)
    if not section_data:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="settings_home")]
        ])
    
    buttons = []
    
    def fmt_val(v):
        s = str(v)
        return f"{s[:15]}..." if len(s) > 15 else s

    if hasattr(section_data, 'model_dump'):
        data = section_data.model_dump()
    else:
        data = section_data if isinstance(section_data, dict) else {}
    
    for k, v in data.items():
        if isinstance(v, dict) or k == "token":
            continue
        
        alias = KEY_ALIASES.get(k, k)
        val_str = fmt_val(v)
        btn_text = f"{alias}: {val_str}"
        
        if isinstance(v, list):
             buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"set_list_menu:{section}:{k}")])
        else:
             buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"set_edit:{section}:{k}")])
    
    buttons.append([InlineKeyboardButton(text="Назад", callback_data="settings_home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_list_menu_kb(section: str, key: str):
    """Генерирует меню управления списком (Добавить/Удалить)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить", callback_data=f"set_list_add:{section}:{key}")],
        [InlineKeyboardButton(text="Удалить", callback_data=f"set_list_rm_menu:{section}:{key}")],
        [InlineKeyboardButton(text="Назад", callback_data=f"set_sec:{section}")]
    ])

def get_list_remove_kb(section: str, key: str, items: list):
    """Генерирует клавиатуру для удаления элементов из списка."""
    buttons = []
    for item in items:
        buttons.append([InlineKeyboardButton(text=f"Удалить {item}", callback_data=f"set_list_rm:{section}:{key}:{item}")])
    buttons.append([InlineKeyboardButton(text="Назад", callback_data=f"set_list_menu:{section}:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cancel_kb():
    """Возвращает кнопку отмены."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_edit")]])
