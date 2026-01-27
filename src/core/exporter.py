import json
import time
import httpx
import pycountry
import re
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Dict, Any

from loguru import logger
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator
from langdetect import detect

from src.core.config import config_instance as config

class Exporter:
    """Класс для загрузки медиафайлов, генерации отчетов и транскрибации объявлений."""
    
    def __init__(self):
        self.results_dir: Optional[Path] = None
        self.http_client: Optional[httpx.Client] = None

    async def run(self, input_path: str | Path):
        """Запускает процесс экспорта данных из указанного JSON-дампа или папки с дампами."""
        self._create_results_dir()
        log_sink = logger.add(
            self.results_dir / "process.log", 
            format="{time} | {level} | {message}",
            level="INFO"
        )
        
        try:
            path_obj = Path(input_path)
            json_files = list(path_obj.glob("*.json")) if path_obj.is_dir() else [path_obj] if path_obj.is_file() else []
            
            if not json_files:
                logger.error(f"Файлы JSON не найдены по пути: {input_path}")
                return

            for jf in json_files:
                logger.info(f"Обработка дампа: {jf.name}")
                try:
                    with open(jf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception as e:
                    logger.error(f"Ошибка чтения JSON {jf}: {e}")
                    continue
                
                if not data:
                    logger.warning(f"Файл {jf} пуст.")
                    continue

                # Структура: { "url": { "group_id": { ... } } }
                ad_data = next(iter(data.values()), {})
                count = len(ad_data)
                
                if count == 0:
                    logger.warning(f"Дамп {jf} не содержит данных объявлений.")
                    continue

                logger.info(f"Найдено {count} объявлений для обработки.")

                with httpx.Client(follow_redirects=True, timeout=30) as client:
                    self.http_client = client
                    with ThreadPoolExecutor(max_workers=config.data.exporter.exporter_workers) as executor:
                        futures = [executor.submit(self._process_ad, url, details) for url, details in ad_data.items()]
                        processed_count = 0
                        for future in as_completed(futures):
                            try:
                                res = future.result()
                                if res: 
                                    processed_count += 1
                                    logger.debug(f"Успешно: {Path(res).name}")
                            except Exception as e:
                                logger.error(f"Ошибка обработки отдельного объявления: {e}")
                        
                        logger.info(f"Экспорт завершен: {processed_count} из {count} запрашиваемых.")

            logger.info("Начало транскрибации видео...")
            self._process_transcriptions()
            
        finally:
            logger.remove(log_sink)

    def _process_ad(self, ad_url: str, data: dict) -> Optional[str]:
        """Обрабатывает одно объявление: фильтрация, создание папки, загрузка медиа."""
        reaches = data.get("total_reaches", {"eu": {}, "uk": {}})
        eu_s = sum(reaches.get("eu", {}).values())
        uk_s = sum(reaches.get("uk", {}).values())
        total = eu_s + uk_s

        # Фильтрация по минимальному охвату
        if (total < config.data.exporter.min_reaches or 
            eu_s < config.data.exporter.min_reaches_eu or 
            uk_s < config.data.exporter.min_reaches_uk):
            return None

        folder_name = self._get_folder_name(reaches, data.get("ad_texts", []))
        path = self._create_creative_folder(folder_name)
        
        # Сохранение текстовой информации
        self._save_details(path, ad_url, data, eu_s, uk_s, total)

        # Обработка URL видео и картинок
        v_urls = data.get("video_urls", [])
        # Очистка дубликатов видео (по токену oe)
        v_urls_unique = list({u.split("oe=")[1].split('&')[0]: u for u in v_urls if "oe=" in u}.values()) if v_urls else []
        i_urls = data.get("img_urls", [])
        
        media_urls = v_urls_unique + i_urls
        if not media_urls:
            logger.debug(f"Медиа не найдены для {ad_url}")
            return str(path)

        for idx, url in enumerate(media_urls, 1):
            m_path = path / f"Creative #{idx}"
            m_path.mkdir(exist_ok=True)
            
            filename = config.data.exporter.video_filename if url in v_urls_unique else config.data.exporter.image_filename
            self._download(url, m_path / filename)
        
        return str(path)

    def _download(self, url: str, path: Path):
        """Загружает файл по URL с поддержкой повторных попыток."""
        for i in range(config.data.exporter.max_retries):
            try:
                with self.http_client.stream("GET", url) as r:
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        for chunk in r.iter_bytes(): f.write(chunk)
                return
            except Exception as e:
                logger.warning(f"Ошибка загрузки {path.name} (попытка {i+1}): {e}")
                if i < config.data.exporter.max_retries - 1:
                    time.sleep(config.data.exporter.retry_delay_seconds)

    def _save_details(self, path: Path, url: str, data: dict, eu_s: int, uk_s: int, total: int):
        """Формирует и сохраняет текстовый отчет Details.txt."""
        with open(path / config.data.exporter.details_filename, "w", encoding="utf-8") as f:
            f.write(f"Link: {url}\n\nTexts:\n")
            for i, t in enumerate(data.get("ad_texts", []), 1):
                f.write(f"--- Text #{i} ---\n{t}\n")
            
            s_date_raw = data.get("start_date")
            s_date = datetime.fromtimestamp(s_date_raw).strftime("%Y-%m-%d") if s_date_raw else "N/A"
            
            # Эстимейт затрат (упрощенная формула)
            spend = (total / 1000) * 10
            f.write(f"\nMetadata:\nStart: {s_date}\nSpend: ${spend:.2f}\n")
            f.write(f"\nReaches:\nTotal: {total}\nEU: {eu_s}\nUK: {uk_s}\n")
            
            def _write_region(label, r_data):
                f.write(f"\n[{label}]\n")
                if not r_data:
                    f.write("-\n")
                    return
                for code, count in sorted(r_data.items(), key=lambda x: x[1], reverse=True):
                    try:
                        country = pycountry.countries.get(alpha_2=code)
                        c_name = country.name if country else code
                    except:
                        c_name = code
                    f.write(f"{c_name}: {count}\n")
            
            _write_region("EU", data.get("total_reaches", {}).get("eu", {}))
            _write_region("UK", data.get("total_reaches", {}).get("uk", {}))
            
            rsoc = data.get("rsoc_keywords", [])
            f.write("\nRSOC:\n" + ("\n".join(f"- {k}" for k in rsoc) if rsoc else "None"))

    def _get_folder_name(self, reaches: dict, texts: list) -> str:
        """Генерирует имя папки на основе текста объявления и охватов."""
        summary = ""
        if texts:
            t = texts[0]
            try:
                # Перевод для названия папки, если текст не на русском
                if detect(t) != 'ru':
                    t = GoogleTranslator(source='auto', target='ru').translate(t)
            except: pass
            
            # Очистка спецсимволов для Windows
            t = re.sub(r'[<>:\"/\\|?*]', '', t).strip()
            summary = f"[{t[:50]}...] "
            
        eu_s = sum(reaches.get("eu", {}).values())
        uk_s = sum(reaches.get("uk", {}).values())
        return f"{summary}EU_{eu_s} UK_{uk_s} Total_{eu_s+uk_s}"

    def _create_results_dir(self):
        """Создает базовую директорию для результатов текущего запуска."""
        base_path = Path(__file__).parent.parent.parent / config.data.exporter.results_base_dir
        self.results_dir = base_path / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _create_creative_folder(self, name: str) -> Path:
        """Создает уникальную папку для одного объявления, избегая дубликатов имен."""
        p = self.results_dir / name
        counter = 1
        while p.exists():
            p = self.results_dir / f"{name} ({counter})"
            counter += 1
        p.mkdir(parents=True)
        return p

    def _process_transcriptions(self):
        """Запускает транскрибацию всех найденных видеофайлов."""
        try:
            model = WhisperModel("base", device="cpu", compute_type="int8")
        except Exception as e:
            logger.error(f"Не удалось инициализировать Whisper: {e}")
            return

        # Поиск видеофайлов
        videos = []
        for ext in config.data.video_extensions:
            videos.extend(list(self.results_dir.rglob(f"*{ext}")))
            
        # Фильтрация мелких файлов (пустых или битых)
        videos = [v for v in videos if v.stat().st_size > 1024]
        
        if not videos: 
            logger.info("Видео для транскрибации не найдено.")
            return
        
        logger.info(f"Обработка {len(videos)} видео для транскрибации...")
        with ThreadPoolExecutor(max_workers=config.data.exporter.exporter_workers) as executor:
            for v in videos:
                executor.submit(self._transcribe, v, model)

    def _transcribe(self, path: Path, model: WhisperModel):
        """Выполняет транскрибацию одного видеофайла и перевод на английский."""
        try:
            segments, _ = model.transcribe(str(path))
            text = "\n".join(s.text.strip() for s in segments)
            
            if not text.strip():
                return

            # Перевод текста транскрипции на английский
            try:
                trans = GoogleTranslator(source='auto', target='en').translate(text)
            except:
                trans = "Translation failed."
                
            out_file = path.parent / config.data.exporter.transcription_filename
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(f"Original:\n{text}\n\nTranslation (EN):\n{trans}")
        except Exception as e:
            logger.error(f"Ошибка транскрибации {path.name}: {e}")

async def main(input_path: str | Path):
    """Точка входа для запуска процесса экспорта."""
    await Exporter().run(input_path)
