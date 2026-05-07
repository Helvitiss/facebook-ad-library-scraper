import json
import re
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from deep_translator import GoogleTranslator
from langdetect import DetectorFactory, detect
from loguru import logger

from src.core.config import config_instance as config

# Deterministic language detection.
DetectorFactory.seed = 0


class Exporter:
    """Loads media and builds export reports for ad groups."""

    _translate_lock = threading.Lock()

    def __init__(self):
        self.results_dir: Optional[Path] = None
        self.http_client: Optional[httpx.Client] = None
        self.insecure_http_client: Optional[httpx.Client] = None
        self.folder_lock = threading.Lock()
        try:
            detect("")
        except Exception:
            pass

    async def run(self, input_path: str | Path):
        """Run export from a JSON file or a directory with JSON dumps."""
        self._create_results_dir()
        log_sink = logger.add(
            self.results_dir / "process.log",
            format="{time} | {level} | {message}",
            level="INFO",
        )

        try:
            path_obj = Path(input_path)
            if path_obj.is_dir():
                json_files = list(path_obj.glob("*.json"))
            elif path_obj.is_file():
                json_files = [path_obj]
            else:
                json_files = []

            if not json_files:
                logger.error(f"JSON files not found at: {input_path}")
                return

            for jf in json_files:
                logger.info(f"Processing dump: {jf.name}")
                try:
                    with open(jf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception as e:
                    logger.error(f"Error reading JSON {jf}: {e}")
                    continue

                if not data:
                    logger.warning(f"File {jf} is empty.")
                    continue

                ads_to_process: list[dict] = []
                if isinstance(data, list):
                    ads_to_process = data
                else:
                    ad_data = next(iter(data.values()), {})
                    for ad_url, details in ad_data.items():
                        details["ad_url"] = ad_url
                        ads_to_process.append(details)

                count = len(ads_to_process)
                if count == 0:
                    logger.warning(f"Dump {jf} has no ad data.")
                    continue

                url_groups = defaultdict(list)
                for ad in ads_to_process:
                    target_url = ad.get("link_url") or ad.get("ad_url") or "No Link"
                    if isinstance(target_url, str):
                        target_url = target_url.strip()
                    url_groups[target_url].append(ad)

                logger.info(
                    f"Found {count} ads grouped into {len(url_groups)} unique links."
                )

                proxy = config.data.scraper.proxy_url or None
                limits = httpx.Limits(
                    max_keepalive_connections=config.data.exporter.exporter_workers,
                    max_connections=config.data.exporter.exporter_workers + 5,
                )

                with httpx.Client(
                    follow_redirects=True,
                    timeout=30,
                    proxy=proxy,
                    limits=limits,
                ) as client, httpx.Client(
                    follow_redirects=True,
                    timeout=30,
                    proxy=proxy,
                    limits=limits,
                    verify=False,
                ) as insecure_client:
                    self.http_client = client
                    self.insecure_http_client = insecure_client
                    with ThreadPoolExecutor(
                        max_workers=config.data.exporter.exporter_workers
                    ) as executor:
                        futures = [
                            executor.submit(self._process_ad_group, url, group)
                            for url, group in url_groups.items()
                        ]
                        processed_count = 0
                        for future in as_completed(futures):
                            try:
                                res = future.result()
                                if res:
                                    processed_count += 1
                                    logger.debug(
                                        f"Group processed: {Path(res).name}"
                                    )
                            except Exception as e:
                                logger.exception(f"Ad group processing error: {e}")

                        logger.info(
                            f"Export finished: processed {processed_count} link groups."
                        )

        finally:
            logger.remove(log_sink)

    def _process_ad_group(self, target_url: str, ads: list[dict]) -> Optional[str]:
        """Process one ad group with identical target URL."""
        total_global = 0
        all_reaches: dict[str, int | float] = {}

        for ad in ads:
            reaches = ad.get("total_reaches", {})
            if not isinstance(reaches, dict):
                continue

            if "eu" in reaches or "uk" in reaches:
                for reg in ("eu", "uk"):
                    reg_data = reaches.get(reg, {})
                    if isinstance(reg_data, dict):
                        for key, value in reg_data.items():
                            if isinstance(value, (int, float)):
                                all_reaches[key] = all_reaches.get(key, 0) + value
                                total_global += value
            else:
                for key, value in reaches.items():
                    if isinstance(value, (int, float)):
                        all_reaches[key] = all_reaches.get(key, 0) + value
                        total_global += value

        if total_global == 0 or total_global < config.data.exporter.min_reaches:
            logger.info(
                f"Group {target_url} skipped: reach ({total_global}) below threshold"
            )
            return None

        ad_texts = ads[0].get("ad_texts", [])
        main_text = ad_texts[0] if ad_texts else "No text"
        folder_name = self._get_folder_name(all_reaches, [main_text])
        path = self._create_creative_folder(folder_name)

        self._save_group_details(path, target_url, ads, all_reaches, total_global)

        for ad_idx, ad in enumerate(ads, 1):
            video_urls = ad.get("video_urls", [])
            video_urls_unique = (
                list(
                    {
                        url.split("oe=")[1].split("&")[0]: url
                        for url in video_urls
                        if "oe=" in url
                    }.values()
                )
                if video_urls
                else []
            )
            image_urls = ad.get("img_urls", [])

            media_urls = video_urls_unique + image_urls
            if not media_urls:
                continue

            creative_path = path / f"Creative #{ad_idx}" if len(ads) > 1 else path
            creative_path.mkdir(exist_ok=True)

            for media_idx, url in enumerate(media_urls, 1):
                final_path = creative_path
                if len(media_urls) > 1:
                    final_path = creative_path / f"File #{media_idx}"
                    final_path.mkdir(exist_ok=True)

                filename = (
                    config.data.exporter.video_filename
                    if url in video_urls_unique
                    else config.data.exporter.image_filename
                )
                self._download(url, final_path / filename)

        return str(path)

    def _save_group_details(
        self,
        path: Path,
        target_url: str,
        ads: list[dict],
        reaches: dict,
        total: int,
    ):
        """Save legacy-style Details.txt for one link group."""
        with open(path / config.data.exporter.details_filename, "w", encoding="utf-8") as f:
            f.write(f"Link: {target_url}\n\nTexts:\n")

            seen_texts = set()
            text_index = 1
            for ad in ads:
                for text in ad.get("ad_texts", []):
                    if text and text not in seen_texts:
                        f.write(f"--- Text #{text_index} ---\n{text}\n")
                        seen_texts.add(text)
                        text_index += 1

            start_dates = [ad.get("start_date") for ad in ads if ad.get("start_date")]
            min_start = min(start_dates) if start_dates else None
            start_date = (
                datetime.fromtimestamp(min_start).strftime("%Y-%m-%d")
                if min_start
                else "N/A"
            )

            spend = (total / 1000) * 10
            f.write(f"\nMetadata:\nStart: {start_date}\nSpend: ${spend:.2f}\n")

            eu_total = sum(v for k, v in reaches.items() if k == "EU_TOTAL")
            uk_total = sum(v for k, v in reaches.items() if k == "UK_TOTAL")

            f.write(f"\nReaches:\nTotal: {total}\n")
            f.write(
                "EU: "
                f"{eu_total or (total if 'EU_TOTAL' not in reaches and 'UK_TOTAL' not in reaches else 0)}\n"
            )
            f.write(f"UK: {uk_total}\n")

            f.write("\n[EU]\n")
            found_eu = False
            for code, count in sorted(reaches.items(), key=lambda x: x[1], reverse=True):
                if code in ("EU_TOTAL", "UK_TOTAL"):
                    continue
                f.write(f"{code}: {count}\n")
                found_eu = True
            if not found_eu:
                f.write("-\n")

            f.write("\n[UK]\n-\n")

            all_rsoc: list[str] = []
            for ad in ads:
                for keyword in ad.get("rsoc_keywords", []):
                    if keyword not in all_rsoc:
                        all_rsoc.append(keyword)
            f.write("\nRSOC:\n" + ("\n".join(f"- {k}" for k in all_rsoc) if all_rsoc else "-"))

    def _download(self, url: str, path: Path):
        """Download a file with retries."""
        for i in range(config.data.exporter.max_retries):
            try:
                self._download_with_client(self.http_client, url, path)
                return
            except Exception as e:
                if self._is_fbcdn_ssl_hostname_error(e, url):
                    try:
                        logger.warning(
                            f"Facebook CDN SSL hostname mismatch for {path.name}; "
                            "retrying this file without certificate verification"
                        )
                        self._download_with_client(self.insecure_http_client, url, path)
                        return
                    except Exception as fallback_error:
                        e = fallback_error

                logger.warning(f"Download error {path.name} (attempt {i + 1}): {e}")
                if i < config.data.exporter.max_retries - 1:
                    time.sleep(config.data.exporter.retry_delay_seconds)

    def _download_with_client(self, client: httpx.Client | None, url: str, path: Path):
        """Download a file through a specific HTTP client."""
        if client is None:
            raise RuntimeError("HTTP client is not initialized")

        tmp_path = path.with_name(f"{path.name}.part")
        try:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in response.iter_bytes():
                        f.write(chunk)
            tmp_path.replace(path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    @staticmethod
    def _is_fbcdn_ssl_hostname_error(exc: Exception, url: str) -> bool:
        """Detect Facebook CDN certificate host mismatches for a narrow retry path."""
        host = urlparse(url).hostname or ""
        if host != "fbcdn.net" and not host.endswith(".fbcdn.net"):
            return False

        message = str(exc).lower()
        return (
            "certificate_verify_failed" in message
            or "certificate verify failed" in message
        ) and (
            "hostname mismatch" in message
            or "not valid for" in message
        )

    def _get_folder_name(self, reaches: dict, texts: list[str]) -> str:
        """Generate folder name from ad text and aggregated reaches."""
        summary = ""
        if texts:
            text = texts[0]
            try:
                lang = detect(text)
                if lang != "ru":
                    proxy = config.data.scraper.proxy_url
                    proxies = {"http": proxy, "https": proxy} if proxy else None
                    with Exporter._translate_lock:
                        text = GoogleTranslator(
                            source="auto",
                            target="ru",
                            proxies=proxies,
                        ).translate(text)
                        time.sleep(0.5)
                    logger.debug(
                        f"Translated folder name: '{texts[0][:20]}...' ({lang}) -> '{text[:20]}...'"
                    )
            except Exception as e:
                logger.debug(f"Folder name translation failed: {e}")

            text = self._sanitize_folder_fragment(text)
            summary = f"[{text[:50]}...] " if text else "[No_text...] "

        total = sum(v for v in reaches.values() if isinstance(v, (int, float)))
        return f"{summary}Total_{total}"

    def _sanitize_folder_fragment(self, text: str) -> str:
        """Make folder-name fragment safe for Windows filesystems."""
        if not isinstance(text, str):
            return ""

        cleaned_chars = []
        for char in text:
            category = unicodedata.category(char)
            if category.startswith("C"):
                continue
            if category == "So":
                continue
            cleaned_chars.append(char)

        sanitized = "".join(cleaned_chars)
        sanitized = re.sub(r"\s+", " ", sanitized)
        sanitized = re.sub(r'[<>:"/\\|?*]', "", sanitized)
        sanitized = sanitized.strip(" .")

        if not re.search(r"[A-Za-z0-9\u0400-\u04FF]", sanitized):
            return ""
        return sanitized

    def _create_results_dir(self):
        """Create base results directory for current run."""
        base_path = Path(__file__).parent.parent.parent / config.data.exporter.results_base_dir
        self.results_dir = base_path / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _create_creative_folder(self, name: str) -> Path:
        """Create unique folder for one ad group."""
        with self.folder_lock:
            path = self.results_dir / name
            counter = 1
            while path.exists():
                path = self.results_dir / f"{name} ({counter})"
                counter += 1
            path.mkdir(parents=True)
            return path


async def main(input_path: str | Path):
    """Entry point for export."""
    await Exporter().run(input_path)
