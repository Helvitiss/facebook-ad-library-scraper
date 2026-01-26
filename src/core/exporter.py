import json
import time
import httpx
import pycountry
import re
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator
from langdetect import detect
from typing import Optional, List, Dict, Any

from src.core.config import config_instance as config

class Exporter:
    def __init__(self):
        self.results_dir: Optional[Path] = None
        self.http_client: Optional[httpx.Client] = None

    async def run(self, input_path: str | Path):
        self._create_results_dir()
        log_sink = logger.add(self.results_dir / "process.log", format="{time} | {level} | {message}")
        
        try:
            path_obj = Path(input_path)
            json_files = list(path_obj.glob("*.json")) if path_obj.is_dir() else [path_obj] if path_obj.is_file() else []
            
            if not json_files:
                logger.error(f"No JSON files found at {input_path}")
                return

            for jf in json_files:
                logger.info(f"Обработка дампа: {jf.name}")
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                if not data:
                    logger.warning("Файл JSON пуст.")
                    continue

                # Structure: { "url": { "group_id": { ... } } }
                # We need to count items
                ad_data = next(iter(data.values()), {})
                
                count = len(ad_data)
                logger.info(f"Найдено {count} объявлений в дампе.")
                
                if count == 0:
                    logger.warning("Структура данных не содержит объявлений.")
                    continue

                with httpx.Client(follow_redirects=True, timeout=30) as client:
                    self.http_client = client
                    with ThreadPoolExecutor(max_workers=config.data.exporter.exporter_workers) as executor:
                        futures = [executor.submit(self._process_ad, url, details) for url, details in ad_data.items()]
                        processed_count = 0
                        for future in as_completed(futures):
                            try:
                                res = future.result()
                                if res: 
                                    logger.success(f"Экспортировано: {Path(res).name}")
                                    processed_count += 1
                            except Exception as e: logger.exception(f"Ошибка обработки объявления: {e}")
                        
                        logger.info(f"Итого экспортировано {processed_count} из {count}.")

            logger.info("Транскрибация видео...")
            self._process_transcriptions()
        finally:
            logger.remove(log_sink)

    def _process_ad(self, ad_url: str, data: dict) -> Optional[str]:
        reaches = data.get("total_reaches", {"eu": {}, "uk": {}})
        eu_s = sum(reaches.get("eu", {}).values())
        uk_s = sum(reaches.get("uk", {}).values())
        total = eu_s + uk_s

        if (total < config.data.exporter.min_reaches or 
            eu_s < config.data.exporter.min_reaches_eu or 
            uk_s < config.data.exporter.min_reaches_uk):
            # Debug log for skipped items
            # logger.debug(f"Skipped {ad_url}: Reach {total} < Min {config.data.exporter.min_reaches}")
            return None

        folder_name = self._get_folder_name(reaches, data.get("ad_texts", []))
        path = self._create_creative_folder(folder_name)
        
        self._save_details(path, ad_url, data, eu_s, uk_s, total)

        v_urls = list({u.split("oe=")[1].split('&')[0]: u for u in data.get("video_urls", []) if "oe=" in u}.values()) if data.get("video_urls") else data.get("video_urls", [])
        i_urls = data.get("img_urls", [])
        
        media_urls = v_urls + i_urls
        if not media_urls:
            logger.warning(f"Медиа-файлы не найдены для {ad_url}")
            # Still return path to indicate it was processed? Or skip?
            # If we return None, it won't be counted as exported.
            # But details are saved. Let's return path.
            return str(path)

        for idx, url in enumerate(media_urls, 1):
            m_path = path / f"Creative #{idx}"
            m_path.mkdir(exist_ok=True)
            if url in v_urls: self._download(url, m_path / config.data.exporter.video_filename)
            else: self._download(url, m_path / config.data.exporter.image_filename)
        
        return str(path)

    def _download(self, url: str, path: Path):
        for i in range(config.data.exporter.max_retries):
            try:
                with self.http_client.stream("GET", url) as r:
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        for chunk in r.iter_bytes(): f.write(chunk)
                return
            except Exception as e:
                logger.warning(f"Ошибка загрузки (попытка {i+1}): {e}")
                if i < config.data.exporter.max_retries - 1: time.sleep(config.data.exporter.retry_delay_seconds)

    def _save_details(self, path: Path, url: str, data: dict, eu_s, uk_s, total):
        with open(path / config.data.exporter.details_filename, "w", encoding="utf-8") as f:
            f.write(f"Link: {url}\n\nTexts:\n")
            for i, t in enumerate(data.get("ad_texts", []), 1):
                f.write(f"--- Text #{i} ---\n{t}\n")
            
            s_date = datetime.fromtimestamp(data["start_date"]).strftime("%Y-%m-%d") if data.get("start_date") else "N/A"
            f.write(f"\nMetadata:\nStart: {s_date}\nSpend: ${(total/1000)*10:.2f}\n")
            f.write(f"\nReaches:\nTotal: {total}\nEU: {eu_s}\nUK: {uk_s}\n")
            
            def _w_r(label, r_data):
                f.write(f"\n[{label}]\n")
                if not r_data:
                    f.write("-\n")
                    return
                for c, r in sorted(r_data.items(), key=lambda x: x[1], reverse=True):
                    name = pycountry.countries.get(alpha_2=c).name if pycountry.countries.get(alpha_2=c) else c
                    f.write(f"{name}: {r}\n")
            
            _w_r("EU", data.get("total_reaches", {}).get("eu", {}))
            _w_r("UK", data.get("total_reaches", {}).get("uk", {}))
            
            f.write("\nRSOC:\n" + ("\n".join(f"- {k}" for k in data.get("rsoc_keywords", [])) if data.get("rsoc_keywords") else "None"))

    def _get_folder_name(self, reaches: dict, texts: list) -> str:
        summary = ""
        if texts:
            t = texts[0]
            try:
                if detect(t) != 'ru': t = GoogleTranslator(source='auto', target='ru').translate(t)
            except: pass
            t = re.sub(r'[<>:\"/\\|?*]', '', t)
            summary = f"[{t[:50]}...] "
        eu_s = sum(reaches.get("eu", {}).values())
        uk_s = sum(reaches.get("uk", {}).values())
        return f"{summary}EU_{eu_s} UK_{uk_s} Total_{eu_s+uk_s}"

    def _create_results_dir(self):
        self.results_dir = Path(__file__).parent.parent.parent / config.data.exporter.results_base_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _create_creative_folder(self, name: str) -> Path:
        p = self.results_dir / name
        c = 1
        while p.exists(): p = self.results_dir / f"{name} ({c})"; c += 1
        p.mkdir(parents=True); return p

    def _process_transcriptions(self):
        model = WhisperModel("base", device="cpu", compute_type="int8")
        videos = [f for f in self.results_dir.rglob("*") if f.suffix.lower() in config.data.video_extensions and f.stat().st_size > 1024]
        if not videos: 
            logger.info("Видео для транскрибации не найдено.")
            return
        
        logger.info(f"Обработка {len(videos)} видео для транскрибации...")
        with ThreadPoolExecutor(max_workers=config.data.exporter.exporter_workers) as executor:
            [executor.submit(self._transcribe, v, model) for v in videos]

    def _transcribe(self, path: Path, model: WhisperModel):
        try:
            segments, _ = model.transcribe(str(path))
            text = "\n".join(s.text.strip() for s in segments)
            trans = GoogleTranslator(source='auto', target='en').translate(text)
            with open(path.parent / config.data.exporter.transcription_filename, "w", encoding="utf-8") as f:
                f.write(f"Original:\n{text}\n\nTranslation:\n{trans}")
        except Exception as e: logger.error(f"Ошибка транскрибации {path.name}: {e}")

async def main(input_path: str | Path):
    await Exporter().run(input_path)
