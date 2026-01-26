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
    concurrent_requests: int = 45
    retries_per_creative: int = 5
    url_workers: int = 3
    proxy_url: Optional[str] = ""
    proxy_change_url: Optional[str] = ""

class ExporterConfig(BaseModel):
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
    endpoint_url: str = "https://www.facebook.com/api/graphql/"
    doc_ids: Dict[str, str]

class TelegramConfig(BaseModel):
    token: str
    user_ids: List[int]
    owner_ids: List[int] = []

class AppConfig(BaseModel):
    scraper: ScraperConfig
    exporter: ExporterConfig
    facebook_api: FacebookApiConfig
    telegram: TelegramConfig
    video_extensions: List[str]

# --- Класс управления конфигурацией ---

class Config:
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
        try:
            if not self.path.exists():
                logger.error(f"Config file not found: {self.path}")
                return

            with open(self.path, "r", encoding="utf-8") as f:
                raw_cfg = json.load(f)
            
            # Подстановка секретов из ENV (переменные окружения имеют приоритет)
            telegram_cfg = raw_cfg.setdefault("telegram", {})
            scraper_cfg = raw_cfg.setdefault("scraper", {})
            
            # Токен
            if os.getenv("TG_TOKEN"):
                telegram_cfg["token"] = os.getenv("TG_TOKEN")
            
            # User IDs - ENV имеет приоритет
            if os.getenv("TG_USER_IDS"):
                user_ids_str = os.getenv("TG_USER_IDS")
                user_ids = [int(uid.strip()) for uid in user_ids_str.split(",") if uid.strip()]
                telegram_cfg["user_ids"] = user_ids
            
            # Owner IDs - ENV имеет приоритет
            if os.getenv("TG_OWNER_IDS"):
                owner_ids_str = os.getenv("TG_OWNER_IDS")
                owner_ids = [int(oid.strip()) for oid in owner_ids_str.split(",") if oid.strip()]
                telegram_cfg["owner_ids"] = owner_ids
            
            # Прокси
            if os.getenv("PROXY_URL"):
                scraper_cfg["proxy_url"] = os.getenv("PROXY_URL")
            
            if os.getenv("PROXY_CHANGE_URL"):
                scraper_cfg["proxy_change_url"] = os.getenv("PROXY_CHANGE_URL")

            self.data = AppConfig(**raw_cfg)
            logger.debug("Configuration loaded and validated with Pydantic")
        
        except ValidationError as e:
            logger.error(f"❌ Configuration Validation Error:\n{e}")
            # Не прерываем работу, чтобы можно было увидеть ошибку
        except Exception as e:
            logger.error(f"Error loading config: {e}")

    def save(self, new_config: dict | AppConfig):
        try:
            if isinstance(new_config, AppConfig):
                dump = new_config.model_dump()
            else:
                dump = new_config
            
            # Mask secret token if it matches the one in ENV
            env_token = os.getenv("TG_TOKEN")
            if env_token and dump.get("telegram", {}).get("token") == env_token:
                 dump["telegram"]["token"] = "ENV_VAR"
            
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(dump, f, indent=2, ensure_ascii=False)
            
            self.reload()
            return True
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            return False


    # Global instance
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
config_instance = Config(PROJECT_ROOT / "config.json")


if __name__ == "__main__":
    print(config_instance.data)