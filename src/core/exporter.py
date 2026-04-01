import asyncio
import json
import time
import httpx
import pycountry
import re
import unicodedata
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Dict, Any, TYPE_CHECKING

import threading
from loguru import logger
from deep_translator import GoogleTranslator
from langdetect import detect, DetectorFactory

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

# Устанавливаем seed для детерминированных результатов определения языка
DetectorFactory.seed = 0

from src.core.config import config_instance as config

class Exporter:
    """Класс для загрузки медиафайлов, генерации отчетов и транскрибации объявлений."""
    
    _translate_lock = threading.Lock()
    
    def __init__(self):
        self.results_dir: Optional[Path] = None
        self.http_client: Optional[httpx.Client] = None
        self.folder_lock = threading.Lock()
        # Предварительная инициализация langdetect для избежания состояния гонки в потоках
        try:
            detect("")
        except:
            pass

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

                # Поддержка как старого формата (dict), так и нового (list)
                ads_to_process = []
                if isinstance(data, list):
                    ads_to_process = data
                else:
                    # Старый формат: { "url": { "group_id": { ... } } }
                    ad_data = next(iter(data.values()), {})
                    for ad_url, details in ad_data.items():
                        details["ad_url"] = ad_url
                        ads_to_process.append(details)

                count = len(ads_to_process)
                
                if count == 0:
                    logger.warning(f"Дамп {jf} не содержит данных объявлений.")
                    continue

                # Группировка по полной link_url (query-параметры сохраняем)
                from collections import defaultdict
                url_groups = defaultdict(list)
                for ad in ads_to_process:
                    target_url = ad.get("link_url") or ad.get("ad_url") or "No Link"
                    if isinstance(target_url, str):
                        target_url = target_url.strip()
                    url_groups[target_url].append(ad)

                logger.info(f"Найдено {count} объявлений, сгруппированных в {len(url_groups)} уникальных ссылок.")
                
                proxy = config.data.scraper.proxy_url or None
                limits = httpx.Limits(max_keepalive_connections=config.data.exporter.exporter_workers, max_connections=config.data.exporter.exporter_workers + 5)
                with httpx.Client(follow_redirects=True, timeout=30, proxy=proxy, limits=limits) as client:
                    self.http_client = client
                    with ThreadPoolExecutor(max_workers=config.data.exporter.exporter_workers) as executor:
                        futures = [executor.submit(self._process_ad_group, url, group) for url, group in url_groups.items()]
                        processed_count = 0
                        for future in as_completed(futures):
                            try:
                                res = future.result()
                                if res: 
                                    processed_count += 1
                                    logger.debug(f"Успешно обработана группа: {Path(res).name}")
                            except Exception as e:
                                logger.exception(f"Ошибка обработки группы объявлений: {e}")
                        
                        logger.info(f"Экспорт завершен: обработано {processed_count} групп ссылок.")

            if not config.data.debug_mode:
                logger.info("Начало транскрибации видео...")
                # Запускаем в потоке, чтоб не блокировать event loop бота Telegram во время длительной транскрибации
                await asyncio.to_thread(self._process_transcriptions)
            else:
                logger.info("Отладочный режим: транскрибация видео пропущена.")
            
        finally:
            logger.remove(log_sink)

    def _process_ad_group(self, target_url: str, ads: List[dict]) -> Optional[str]:
        """Обрабатывает группу объявлений с одинаковой ссылкой: агрегация, папки, загрузка медиа."""
        # Агрегация статистики по всей группе
        total_global = 0
        all_reaches = {}
        
        for ad in ads:
            reaches = ad.get("total_reaches", {})
            if not isinstance(reaches, dict): continue

            # Если это СТАРАЯ структура {"eu": {...}, "uk": {...}}
            if "eu" in reaches or "uk" in reaches:
                for reg in ("eu", "uk"):
                    reg_data = reaches.get(reg, {})
                    if isinstance(reg_data, dict):
                        for k, v in reg_data.items():
                            if isinstance(v, (int, float)):
                                all_reaches[k] = all_reaches.get(k, 0) + v
                                total_global += v
            else:
                # НОВАЯ структура (плоская)
                for k, v in reaches.items():
                    if isinstance(v, (int, float)):
                        all_reaches[k] = all_reaches.get(k, 0) + v
                        total_global += v

        # Фильтрация по минимальному охвату группы
        if total_global == 0 or total_global < config.data.exporter.min_reaches:
            logger.info(f"Группа {target_url} пропущена: охват ({total_global}) ниже порога")
            return None

        # Имя папки по первому тексту (или URL) + суммарный охват
        ad_texts = ads[0].get("ad_texts", [])
        main_text = ad_texts[0] if ad_texts else "No text"
        folder_name = self._get_folder_name(all_reaches, [main_text])
        path = self._create_creative_folder(folder_name)
        
        # Сохранение общего Details.txt
        self._save_group_details(path, target_url, ads, all_reaches, total_global)

        # Обработка медиа для каждого объявления в группе
        for ad_idx, ad in enumerate(ads, 1):
            ad_url = ad.get("ad_url", "No URL")
            v_urls = ad.get("video_urls", [])
            # Очистка дубликатов видео (по токену oe)
            v_urls_unique = list({u.split("oe=")[1].split('&')[0]: u for u in v_urls if "oe=" in u}.values()) if v_urls else []
            i_urls = ad.get("img_urls", [])
            
            media_urls = v_urls_unique + i_urls
            if not media_urls: continue

            # Создаем подпапку для конкретного креатива, если их больше одного в группе
            creative_path = path / f"Creative #{ad_idx}" if len(ads) > 1 else path
            creative_path.mkdir(exist_ok=True)
            
            for m_idx, url in enumerate(media_urls, 1):
                # Если в креативе несколько картинок/видео
                final_path = creative_path
                if len(media_urls) > 1:
                    final_path = creative_path / f"File #{m_idx}"
                    final_path.mkdir(exist_ok=True)
                
                filename = config.data.exporter.video_filename if url in v_urls_unique else config.data.exporter.image_filename
                self._download(url, final_path / filename)
        
        return str(path)

    def _save_group_details(self, path: Path, target_url: str, ads: List[dict], reaches: dict, total: int):
        """Сохраняет агрегированный Details.txt для группы объявлений в СТАРОМ формате."""
        with open(path / config.data.exporter.details_filename, "w", encoding="utf-8") as f:
            f.write(f"Link: {target_url}\n\nTexts:\n")
            
            seen_texts = set()
            t_idx = 1
            for ad in ads:
                for t in ad.get("ad_texts", []):
                    if t and t not in seen_texts:
                        f.write(f"--- Text #{t_idx} ---\n{t}\n")
                        seen_texts.add(t)
                        t_idx += 1
            
            # Дата старта (самая ранняя в группе)
            start_dates = [ad.get("start_date") for ad in ads if ad.get("start_date")]
            min_start = min(start_dates) if start_dates else None
            s_date = datetime.fromtimestamp(min_start).strftime("%Y-%m-%d") if min_start else "N/A"
            
            # Эстимейт затрат
            spend = (total / 1000) * 10
            f.write(f"\nMetadata:\nStart: {s_date}\nSpend: ${spend:.2f}\n")
            
            # Охваты
            eu_total = sum(v for k, v in reaches.items() if k == "EU_TOTAL")
            # Если нет EU_TOTAL, возможно это старая структура или плоская по странам
            # Но для отчета нам нужны EU и UK суммы
            # В текущей логике скрапера мы стараемся давать EU_TOTAL
            uk_total = sum(v for k, v in reaches.items() if k == "UK_TOTAL")
            
            f.write(f"\nReaches:\nTotal: {total}\n")
            f.write(f"EU: {eu_total or (total if 'EU_TOTAL' not in reaches and 'UK_TOTAL' not in reaches else 0)}\n")
            f.write(f"UK: {uk_total}\n")
            
            def _write_region_old(label, r_data, filter_prefix=None):
                f.write(f"\n[{label}]\n")
                # В нашей плоской структуре reaches нет вложенности, 
                # но мы можем фильтровать или просто выводить всё в EU если нет деления
                found = False
                for code, count in sorted(reaches.items(), key=lambda x: x[1], reverse=True):
                    if code in ("EU_TOTAL", "UK_TOTAL"): continue
                    # Здесь сложно точно разделить плоский список стран на EU/UK без базы,
                    # поэтому выводим в EU всё, что не UK (упрощенно как было раньше)
                    if label == "EU":
                        f.write(f"{code}: {count}\n")
                        found = True
                if not found:
                    f.write("-\n")
            
            _write_region_old("EU", reaches)
            f.write("\n[UK]\n-\n") # UK обычно пуст или специфичен
            
            # RSOC
            all_rsoc = []
            for ad in ads:
                for k in ad.get("rsoc_keywords", []):
                    if k not in all_rsoc: all_rsoc.append(k)
            
            f.write("\nRSOC:\n" + ("\n".join(f"- {k}" for k in all_rsoc) if all_rsoc else "-"))

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

    def _get_folder_name(self, reaches: dict, texts: list) -> str:
        """Генерирует имя папки на основе текста объявления и охватов."""
        summary = ""
        if texts:
            t = texts[0]
            try:
                # Перевод для названия папки, если текст не на русском
                lang = detect(t)
                if lang != 'ru':
                    proxy = config.data.scraper.proxy_url
                    proxies = {'http': proxy, 'https': proxy} if proxy else None
                    with Exporter._translate_lock:
                        t = GoogleTranslator(source='auto', target='ru', proxies=proxies).translate(t)
                        # Микро-пауза чтобы не злить Google Translate
                        time.sleep(0.5)
                    logger.debug(f"Translated folder name task: '{texts[0][:20]}...' ({lang}) -> '{t[:20]}...'")
            except Exception as e:
                logger.debug(f"Translation failed for folder name: {e}")
            
            # Очистка и нормализация для Windows (ASCII, без переносов/emoji)
            t = self._sanitize_folder_fragment(t)
            if t:
                summary = f"[{t[:50]}...] "
            else:
                summary = "[No_text...] "
            
        # Суммируем все числовые значения из словаря охватов, избегая двойного счета
        # (если в словаре есть и EU_TOTAL и страны, лучше брать страны или только TOTAL)
        # Для простоты: берем сумму всех значений, но если есть EU_TOTAL, возможно стоит быть аккуратнее.
        # В нашей текущей логике мы суммируем всё.
        total = sum(v for v in reaches.values() if isinstance(v, (int, float)))
             
        return f"{summary}Total_{total}"

    def _sanitize_folder_fragment(self, text: str) -> str:
        """Делает фрагмент имени папки безопасным для Windows."""
        if not isinstance(text, str):
            return ""
        # Убираем управляющие и emoji/символьный шум
        cleaned_chars = []
        for ch in text:
            cat = unicodedata.category(ch)
            if cat.startswith("C"):  # control/format
                continue
            if cat == "So":  # symbols (emoji)
                continue
            cleaned_chars.append(ch)

        s = "".join(cleaned_chars)
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r'[<>:\"/\\|?*]', '', s)
        s = s.strip(" .")

        # Если нет букв/цифр — считаем пустым
        if not re.search(r"[A-Za-zА-Яа-я0-9]", s):
            return ""
        return s

    def _create_results_dir(self):
        """Создает базовую директорию для результатов текущего запуска."""
        base_path = Path(__file__).parent.parent.parent / config.data.exporter.results_base_dir
        self.results_dir = base_path / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _create_creative_folder(self, name: str) -> Path:
        """Создает уникальную папку для одного объявления, избегая дубликатов имен (потокобезопасно)."""
        with self.folder_lock:
            p = self.results_dir / name
            counter = 1
            while p.exists():
                p = self.results_dir / f"{name} ({counter})"
                counter += 1
            p.mkdir(parents=True)
            return p

    _whisper_model: Optional["WhisperModel"] = None

    def _has_audio(self, path: Path) -> bool:
        """Проверяет наличие аудиодорожки в файле с помощью библиотеки av."""
        try:
            import av
            container = av.open(str(path))
            has_audio = len(container.streams.audio) > 0
            container.close()
            return has_audio
        except Exception as e:
            logger.debug(f"Ошибка проверки аудио в {path.name}: {e}")
            # Если не удалось проверить, пробуем транскрибировать (на всякий случай)
            return True

    def _get_media_duration_seconds(self, path: Path) -> Optional[float]:
        """Возвращает длительность медиафайла в секундах, если её удалось определить."""
        try:
            import av

            container = av.open(str(path))
            try:
                if container.duration:
                    return float(container.duration) / 1_000_000

                video_stream = next(iter(container.streams.video), None)
                if video_stream and video_stream.duration and video_stream.time_base:
                    return float(video_stream.duration * video_stream.time_base)
            finally:
                container.close()
        except Exception as e:
            logger.debug(f"Не удалось определить длительность {path.name}: {e}")
        return None

    @staticmethod
    def _format_seconds(seconds: Optional[float]) -> str:
        """Форматирует секунды в MM:SS или HH:MM:SS."""
        if seconds is None:
            return "?:??"

        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _process_transcriptions(self):
        """Запускает транскрибацию всех найденных видеофайлов (последовательно)."""
        if Exporter._whisper_model is None:
            try:
                from faster_whisper import WhisperModel
                # Используем заранее загруженную модель из образа
                model_path = "/app/models/whisper" if Path("/app/models/whisper").exists() else None
                Exporter._whisper_model = WhisperModel(
                    "base", 
                    device="cpu", 
                    compute_type="int8", 
                    download_root=model_path
                )
            except Exception as e:
                logger.error(f"Не удалось инициализировать Whisper: {e}")
                return

        model = Exporter._whisper_model

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
        # Последовательная обработка для экономии CPU и во избежание ошибок с потоками в Docker
        total_videos = len(videos)
        for index, video in enumerate(videos, 1):
            self._transcribe(video, model, index, total_videos)

    def _transcribe(self, path: Path, model: "WhisperModel", index: int, total: int):
        """Выполняет транскрибацию одного видеофайла и перевод на английский."""
        try:
            duration_seconds = self._get_media_duration_seconds(path)
            logger.info(
                f"Транскрибация {index}/{total}: {path.name}"
                + (
                    f" ({self._format_seconds(duration_seconds)})"
                    if duration_seconds is not None else ""
                )
            )

            # 1. Проверка наличия аудио
            if not self._has_audio(path):
                logger.info(f"Транскрибация {index}/{total}: {path.name} пропущена, аудио не найдено.")
                return

            # 2. Транскрибация
            segments, _ = model.transcribe(str(path))
            
            # Собираем сегменты в текст, обрабатывая возможные ошибки итерации
            text_parts = []
            segments_count = 0
            last_logged_step = -1
            last_logged_segment = 0
            try:
                for segment in segments:
                    segment_text = segment.text.strip()
                    if segment_text:
                        text_parts.append(segment_text)

                    segments_count += 1

                    if duration_seconds and duration_seconds > 0:
                        current_seconds = min(float(segment.end), duration_seconds)
                        percent = min(100, int((current_seconds / duration_seconds) * 100))
                        progress_step = percent // 10
                        if progress_step > last_logged_step:
                            last_logged_step = progress_step
                            logger.info(
                                f"Транскрибация {index}/{total}: {path.name} {percent}% "
                                f"({self._format_seconds(current_seconds)}/{self._format_seconds(duration_seconds)}), "
                                f"сегментов: {segments_count}"
                            )
                    elif segments_count - last_logged_segment >= 5:
                        last_logged_segment = segments_count
                        logger.info(
                            f"Транскрибация {index}/{total}: {path.name}, обработано сегментов: {segments_count}"
                        )
            except Exception as e:
                logger.warning(f"Ошибка при чтении сегментов видео {path.name}: {e}")
            
            text = "\n".join(text_parts).strip()
            if not text:
                logger.info(f"Транскрибация {index}/{total}: {path.name} завершена без распознанного текста.")
                return

            # 3. Перевод текста транскрипции на английский
            try:
                proxy = config.data.scraper.proxy_url
                proxies = {'http': proxy, 'https': proxy} if proxy else None
                trans = GoogleTranslator(source='auto', target='en', proxies=proxies).translate(text)
            except Exception as e:
                logger.debug(f"Transcription translation failed: {e}")
                trans = "Translation failed."
                
            out_file = path.parent / config.data.exporter.transcription_filename
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(f"Original:\n{text}\n\nTranslation (EN):\n{trans}")
            logger.info(
                f"Транскрибация {index}/{total}: {path.name} завершена, "
                f"сегментов: {segments_count}, файл: {out_file.name}"
            )
        except Exception as e:
            import traceback
            logger.error(f"Ошибка транскрибации {path.name}: {e}\n{traceback.format_exc()}")

async def main(input_path: str | Path):
    """Точка входа для запуска процесса экспорта."""
    await Exporter().run(input_path)
