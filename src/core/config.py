import os
import json
import asyncio
import httpx
from pathlib import Path
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError
from loguru import logger

# --- Pydantic Models ---

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

# --- Config Class ---

class Config:
    def __init__(self, config_path: str | Path):
        self.path = Path(config_path).resolve()
        
        # Runtime State
        self.IP_READY_EVENT = asyncio.Event()
        self.IP_READY_EVENT.set()
        self.IP_CHANGE_LOCK = asyncio.Lock()
        self.LAST_PROXY_IP: Optional[str] = None
        
        # Helper to expose the pydantic model
        self.data: Optional[AppConfig] = None
        
        # Load env vars
        load_dotenv(self.path.parent / ".env")
        self.reload()

    def reload(self):
        try:
            if not self.path.exists():
                logger.error(f"Config file not found: {self.path}")
                # Create default dummy config to prevent crash if needed, or just fail
                return

            with open(self.path, "r", encoding="utf-8") as f:
                raw_cfg = json.load(f)
            
            # Inject ENV secrets (Env vars override config.json)
            if os.getenv("TG_TOKEN"):
                raw_cfg.setdefault("telegram", {})["token"] = os.getenv("TG_TOKEN")
            
            # You can add more env overrides here if needed
            # if os.getenv("PROXY_URL"):
            #     raw_cfg.setdefault("scraper", {})["proxy_url"] = os.getenv("PROXY_URL")

            self.data = AppConfig(**raw_cfg)
            logger.debug("Configuration loaded and validated with Pydantic")
        
        except ValidationError as e:
            logger.error(f"❌ Configuration Validation Error:\n{e}")
            # We don't raise here to allow the bot to start and maybe log the error, 
            # but in production, you might want to crash.
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
