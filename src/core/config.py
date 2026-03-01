import os
import json
import asyncio
import httpx
from pathlib import Path
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError
from loguru import logger

# --- Модели Pydantic для валидации конфигурации ---

class ScraperConfig(BaseModel):
    """Конфигурация парсера (Scraper)."""
    concurrent_requests: int = 45
    retries_per_creative: int = 5
    url_workers: int = 3
    browser_type: str = "chromium"
    scroll_iterations: int = 3
    proxy_url: Optional[str] = ""
    proxy_change_url: Optional[str] = ""

class ExporterConfig(BaseModel):
    """Конфигурация экспортера данных."""
    max_retries: int = 5
    exporter_workers: int = 20
    details_filename: str = "Details.txt"
    transcription_filename: str = "Transcription.txt"
    video_filename: str = "Creative.mp4"
    image_filename: str = "Creative.jpg"
    results_base_dir: str = "Exporter_Results"
    retry_delay_seconds: int = 5
    min_reaches: int = 0
    min_reaches_eu: int = 0
    min_reaches_uk: int = 0

class FacebookApiConfig(BaseModel):
    """Настройки доступа к API Facebook."""
    endpoint_url: str = "https://www.facebook.com/api/graphql/"
    doc_ids: Dict[str, str]

class TelegramConfig(BaseModel):
    """Настройки Telegram бота."""
    token: str
    user_ids: List[int]
    owner_ids: List[int] = []

class AppConfig(BaseModel):
    """Главная схема конфигурации приложения."""
    scraper: ScraperConfig
    exporter: ExporterConfig
    facebook_api: FacebookApiConfig
    telegram: TelegramConfig
    video_extensions: List[str]

# --- Класс управления конфигурацией ---

class Config:
    """Класс-одиночка для управления загрузкой, валидацией и сохранением конфигурации."""
    def __init__(self, config_path: str | Path):
        self.path = Path(config_path).resolve()
        
        # Состояние для управления прокси
        self.IP_READY_EVENT = asyncio.Event()
        self.IP_READY_EVENT.set()
        self.IP_CHANGE_LOCK = asyncio.Lock()
        self.LAST_PROXY_IP: Optional[str] = None
        
        # Валидированная конфигурация
        self.data: Optional[AppConfig] = None
        
        # Загрузка переменных окружения
        load_dotenv(self.path.parent / ".env")
        self.reload()

    def reload(self):
        """Перезагружает конфигурацию из файла и переменных окружения."""
        try:
            if not self.path.exists():
                logger.error(f"Файл конфигурации не найден: {self.path}")
                return

            with open(self.path, "r", encoding="utf-8") as f:
                raw_cfg = json.load(f)
            
            # Маппинг ключей конфига на ENV переменные
            env_map = {
                ("telegram", "token"): "TG_TOKEN",
                ("telegram", "user_ids"): "TG_USER_IDS",
                ("telegram", "owner_ids"): "TG_OWNER_IDS",
                ("scraper", "proxy_url"): "PROXY_URL",
                ("scraper", "proxy_change_url"): "PROXY_CHANGE_URL",
            }
            
            # Загрузка актуальных значений из ENV
            for (section, key), env_var in env_map.items():
                val = os.getenv(env_var)
                if val:
                    sec_data = raw_cfg.setdefault(section, {})
                    if "ids" in key: # Обработка списков ID
                        sec_data[key] = [int(i.strip()) for i in val.split(",") if i.strip()]
                    else:
                        sec_data[key] = val
                else:
                    # Если переменной нет в ENV, а в конфиге стоит заглушка ENV_VAR — сбрасываем
                    sec_data = raw_cfg.setdefault(section, {})
                    current = sec_data.get(key)
                    if current == "ENV_VAR":
                        if "ids" in key:
                            sec_data[key] = []
                        else:
                            sec_data[key] = ""

            self.data = AppConfig(**raw_cfg)
            
            # Интеграция ShadowSocks
            from src.core.proxy_manager import proxy_manager_instance
            proxy_url = self.data.scraper.proxy_url
            if proxy_url and proxy_url.startswith("ss://"):
                if proxy_manager_instance.start_shadowsocks(proxy_url):
                    # Подменяем внутренний URL на локальный SOCKS5 мост
                    self.data.scraper.proxy_url = "socks5://127.0.0.1:1080"
                    logger.info("Трафик перенаправлен на локальный ShadowSocks мост")
            else:
                proxy_manager_instance.stop()

            logger.debug("Конфигурация загружена и валидирована")
        
        except ValidationError as e:
            logger.error(f"❌ Ошибка валидации конфигурации:\n{e}")
        except Exception as e:
            logger.error(f"Ошибка при загрузке конфигурации: {e}")

    def _update_env_file(self, env_vars: Dict[str, str]):
        """Обновляет значения переменных в .env файле, сохраняя комментарии и структуру."""
        env_path = self.path.parent / ".env"
        lines = []
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        
        new_lines = []
        updated_vars = set()
        
        for line in lines:
            if "=" in line and not line.strip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                if key in env_vars:
                    new_lines.append(f"{key}={env_vars[key]}\n")
                    updated_vars.add(key)
                    # Обновляем os.environ, чтобы изменения были видны сразу
                    os.environ[key] = str(env_vars[key])
                    continue
            new_lines.append(line)
            
        for key, val in env_vars.items():
            if key not in updated_vars:
                new_lines.append(f"{key}={val}\n")
                os.environ[key] = str(val)
                
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    def save(self, new_config: dict | AppConfig):
        """Сохраняет новую конфигурацию, разделяя секреты (.env) и публичные данные (json)."""
        try:
            if isinstance(new_config, AppConfig):
                dump = new_config.model_dump()
            else:
                # Глубокое копирование, чтобы не менять оригинал при маскировке
                dump = json.loads(json.dumps(new_config))
            
            env_map = {
                ("telegram", "token"): "TG_TOKEN",
                ("telegram", "user_ids"): "TG_USER_IDS",
                ("telegram", "owner_ids"): "TG_OWNER_IDS",
                ("scraper", "proxy_url"): "PROXY_URL",
                ("scraper", "proxy_change_url"): "PROXY_CHANGE_URL",
            }
            
            to_env = {}
            for (section, key), env_var in env_map.items():
                val = dump.get(section, {}).get(key)
                if val is not None:
                    if isinstance(val, list):
                        to_env[env_var] = ",".join(map(str, val))
                    else:
                        to_env[env_var] = str(val)
                    
                    # Маскируем в дампе для config.json
                    if section in dump:
                        dump[section][key] = "ENV_VAR"
            
            # Сохраняем в .env
            if to_env:
                self._update_env_file(to_env)
            
            # Сохраняем нечувствительные данные в config.json
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(dump, f, indent=2, ensure_ascii=False)
            
            self.reload()
            return True
        except Exception as e:
            logger.error(f"Ошибка при сохранении конфигурации: {e}")
            return False


    # Global instance
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
config_instance = Config(PROJECT_ROOT / "config.json")


if __name__ == "__main__":
    print(config_instance.data)