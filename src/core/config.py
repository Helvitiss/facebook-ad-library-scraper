import asyncio
import json
import httpx
from pathlib import Path
from typing import Optional, List, Dict, Any
from loguru import logger

class Config:
    def __init__(self, config_path: str | Path):
        self.path = Path(config_path).resolve()
        self.lock = asyncio.Lock()
        
        # Default values and shared state
        self.ENDPOINT_URL = "https://www.facebook.com/api/graphql/"
        self.DOC_IDS = {
            "pagination": "123456789",
            "collation": "123456789",
            "creative_info": "123456789"
        }
        self.PROXY_URL = ""
        self.PROXY_CHANGE_URL = ""
        self.CONCURRENT_REQUESTS = 35
        self.RETRIES_PER_CREATIVE = 5
        self.URL_WORKERS = 1
        
        self.PROXY: Optional[httpx.Proxy] = None
        self.IP_CHANGE_LOCK = asyncio.Lock()
        self.LAST_PROXY_IP: Optional[str] = None
        self.IP_READY_EVENT = asyncio.Event()
        self.IP_READY_EVENT.set()
        
        self.MAX_RETRIES = 5
        self.EXPORTER_WORKERS = 20
        self.DETAILS_FILENAME = "Details.txt"
        self.TRANSCRIPTION_FILENAME = "Transcription.txt"
        self.VIDEO_FILENAME = "Creative.mp4"
        self.IMAGE_FILENAME = "Creative.jpg"
        self.RESULTS_BASE_DIR = "Exporter_Results"
        self.RETRY_DELAY_SECONDS = 5
        self.MIN_REACHES = 0
        self.MIN_REACHES_EU = 0
        self.MIN_REACHES_UK = 0
        
        self.TG_TOKEN = ""
        self.ADMIN_IDS: List[int] = []
        self.OWNER_IDS: List[int] = []
        self.VIDEO_EXTENSIONS = [".mp4", ".mov", ".avi", ".mkv"]
        
        self.reload()

    def reload(self):
        try:
            if not self.path.exists():
                logger.error(f"Config file not found: {self.path}")
                return

            with open(self.path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            
            fb = cfg.get("facebook_api", {})
            scr = cfg.get("scraper", {})
            exp = cfg.get("exporter", {})
            tg = cfg.get("telegram", {})

            self.ENDPOINT_URL = fb.get("endpoint_url", self.ENDPOINT_URL)
            self.DOC_IDS = fb.get("doc_ids", self.DOC_IDS)

            self.PROXY_URL = scr.get("proxy_url", self.PROXY_URL)
            self.PROXY_CHANGE_URL = scr.get("proxy_change_url", self.PROXY_CHANGE_URL)
            self.CONCURRENT_REQUESTS = scr.get("concurrent_requests", self.CONCURRENT_REQUESTS)
            self.RETRIES_PER_CREATIVE = scr.get("retries_per_creative", self.RETRIES_PER_CREATIVE)
            self.URL_WORKERS = scr.get("url_workers", self.URL_WORKERS)
            self.PROXY = httpx.Proxy(self.PROXY_URL) if self.PROXY_URL else None

            self.MAX_RETRIES = exp.get("max_retries", self.MAX_RETRIES)
            self.EXPORTER_WORKERS = exp.get("exporter_workers", self.EXPORTER_WORKERS)
            self.DETAILS_FILENAME = exp.get("details_filename", self.DETAILS_FILENAME)
            self.TRANSCRIPTION_FILENAME = exp.get("transcription_filename", self.TRANSCRIPTION_FILENAME)
            self.VIDEO_FILENAME = exp.get("video_filename", self.VIDEO_FILENAME)
            self.IMAGE_FILENAME = exp.get("image_filename", self.IMAGE_FILENAME)
            self.RESULTS_BASE_DIR = exp.get("results_base_dir", self.RESULTS_BASE_DIR)
            self.RETRY_DELAY_SECONDS = exp.get("retry_delay_seconds", self.RETRY_DELAY_SECONDS)
            self.MIN_REACHES = exp.get("min_reaches", self.MIN_REACHES)
            self.MIN_REACHES_EU = exp.get("min_reaches_eu", self.MIN_REACHES_EU)
            self.MIN_REACHES_UK = exp.get("min_reaches_uk", self.MIN_REACHES_UK)

            self.TG_TOKEN = tg.get("token", self.TG_TOKEN)
            self.ADMIN_IDS = tg.get("admin_ids", self.ADMIN_IDS)
            self.OWNER_IDS = tg.get("owner_ids", self.OWNER_IDS)
            self.VIDEO_EXTENSIONS = cfg.get("video_extensions", self.VIDEO_EXTENSIONS)
            
            logger.debug("Configuration reloaded")
        except Exception as e:
            logger.error(f"Error loading config: {e}")

    def save(self, new_config: dict):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(new_config, f, indent=2, ensure_ascii=False)
            self.reload()
            return True
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            return False

# Global instance for easy access
# Assuming config.json is in the root directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
config_instance = Config(PROJECT_ROOT / "config.json")
