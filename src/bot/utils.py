import asyncio
import re
import time
import shutil
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from urllib.parse import urlparse
import httpx
from aiogram.types import Message, FSInputFile
from loguru import logger

from src.core.config import config_instance as config
from src.core.scraper import main as scraper_main
from src.core.exporter import main as exporter_main
from src.core.rsoc import RSOCExtractor

import aiogram.exceptions

class TelegramLogHandler:
    """Перехватчик логов для отправки их в сообщение Telegram (status message)."""
    def __init__(self, message: Message):
        self.message = message
        self.loop = asyncio.get_running_loop()
        self.last_text = ""
        self.last_update_time = 0
        self.cooldown = 3.0 # Увеличенная задержка между обновлениями в секундах
        self.update_task = None
        self.pending_text = None
        
    def write(self, log_record):
        try:
            self.pending_text = log_record
            now = time.time()
            if now - self.last_update_time >= self.cooldown:
                asyncio.run_coroutine_threadsafe(self.update_message(log_record), self.loop)
            else:
                # Если мы в кулдауне, планируем обновление на потом, если еще не запланировано
                if not self.update_task or self.update_task.done():
                    delay = self.cooldown - (now - self.last_update_time)
                    self.update_task = asyncio.run_coroutine_threadsafe(self._delayed_update(delay), self.loop)
        except Exception:
            pass

    async def _delayed_update(self, delay):
        await asyncio.sleep(delay)
        if self.pending_text:
            await self.update_message(self.pending_text)

    async def update_message(self, log_record):
        try:
            text = log_record
            clean_text = re.sub(r'<.*?>', '', text).strip()
            
            prefix = "[LOG]"
            if "INFO" in clean_text: prefix = "[INFO]"
            elif "SUCCESS" in clean_text: prefix = "[OK]"
            elif "WARNING" in clean_text: prefix = "[WARN]"
            elif "ERROR" in clean_text: prefix = "[ERR]"
            elif "DEBUG" in clean_text: prefix = "[DBG]"
            
            parts = clean_text.split('|')
            content = parts[-1].strip() if len(parts) > 1 else clean_text
            new_text = f"{prefix} {content}"
            
            if new_text == self.last_text: return
            
            self.last_text = new_text
            self.last_update_time = time.time()
            self.pending_text = None
            await self.message.edit_text(new_text)
        except aiogram.exceptions.TelegramRetryAfter as e:
            # Если словили флуд-контроль, увеличиваем задержку
            self.cooldown = e.retry_after + 1.0
            self.last_update_time = time.time()
        except Exception:
            pass

async def get_final_domain(url: str) -> str:
    """Определяет конечный домен после всех редиректов."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            response = await client.head(url, follow_redirects=True)
            final_url = str(response.url)
            parsed = urlparse(final_url)
            domain = parsed.netloc
            if domain.startswith('www.'):
                domain = domain[4:]
            return domain if domain else url
    except:
        try:
            parsed = urlparse(url)
            domain = parsed.netloc
            if domain.startswith('www.'):
                domain = domain[4:]
            return domain if domain else url
        except:
            return url

async def process_task(original_message: Message, status_message: Message, url: str):
    """
    Основной воркер процесса:
    1. Запуск парсинга (scraper).
    2. Экспорт и транскрибация (exporter).
    3. Сбор статистики и упаковка результатов в ZIP.
    4. Отправка архива пользователю.
    """
    # tg_handler = TelegramLogHandler(status_message)
    # sink_id = logger.add(tg_handler.write, format="{level} | {message}", level="INFO", filter=lambda r: "aiogram" not in r["name"])
    
    try:
        await status_message.edit_text("Сбор данных...")
        res_dir = await scraper_main([url])
        
        if not res_dir:
            logger.warning(f"Парсер вернул пустой результат для {url}")
            return await status_message.edit_text("Парсер не смог найти данные по этой ссылке. Возможно, поиск не дал результатов или доступ заблокирован.")

        await status_message.edit_text("Загрузка и транскрибация...")
        await exporter_main(res_dir)

        await status_message.edit_text("Упаковка...")
        exp_base = Path(config.data.exporter.results_base_dir)
        subdirs = [d for d in exp_base.iterdir() if d.is_dir()]
        
        if not subdirs:
             return await status_message.edit_text("Ошибка экспорта (нет папок).")
        
        latest = max(subdirs, key=lambda d: d.stat().st_mtime)
        domain_stats = defaultdict(lambda: {"reach": 0, "spend": 0.0})
        
        for folder in latest.iterdir():
            if not folder.is_dir(): continue
            details_file = folder / config.data.exporter.details_filename
            if not details_file.exists(): continue
            
            try:
                with open(details_file, "r", encoding="utf-8") as f:
                    content = f.read()
                
                link_match = re.search(r'Link:\s*(https?://[^\s\n]+)', content)
                if not link_match: continue
                link = link_match.group(1).strip()
                
                domain = await get_final_domain(link)
                if not domain or domain == "N/A": continue
                
                # Поиск охватов (строго по старому формату Total: \d+)
                reach_match = re.search(r'Total:\s*(\d+)', content)
                reach = int(reach_match.group(1)) if reach_match else 0
                
                # Поиск затрат (строго по старому формату Spend: \$[0-9.]+)
                spend_match = re.search(r'Spend:\s*\$([0-9.]+)', content)
                spend = float(spend_match.group(1)) if spend_match else 0.0
                
                domain_stats[domain]["reach"] += reach
                domain_stats[domain]["spend"] += spend
                
            except Exception as e:
                logger.debug(f"Error parsing {details_file}: {e}")
                continue
        
        top_domain = max(domain_stats.items(), key=lambda x: x[1]["reach"])[0] if domain_stats else "results"
        safe_domain = re.sub(r'[^\w\-.]', '_', top_domain)
        
        zip_name = f"{safe_domain}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.make_archive(zip_name, 'zip', latest)
        zip_file = zip_name + ".zip"

        await status_message.edit_text("Отправка...")
        
        summary_lines = ["Результаты обработки:\n"]
        total_reach = 0
        total_spend = 0.0
        
        for domain, stats in sorted(domain_stats.items(), key=lambda x: x[1]["reach"], reverse=True):
            summary_lines.append(f"Link: <code>{domain}</code>")
            summary_lines.append(f"   Spend: ${stats['spend']:.2f}")
            summary_lines.append(f"   Reaches: {stats['reach']:,}\n")
            total_reach += stats["reach"]
            total_spend += stats["spend"]
        
        summary_lines.append(f"<b>Итого:</b>")
        summary_lines.append(f"Total Spend: ${total_spend:.2f}")
        summary_lines.append(f"Total Reaches: {total_reach:,}")
        
        caption = "\n".join(summary_lines)
        
        if len(caption) > 1024:
            await original_message.answer(caption, parse_mode="HTML")
            caption = f"Результаты обработки\nTotal Spend: ${total_spend:.2f}\nTotal Reaches: {total_reach:,}"
        
        await original_message.reply_document(
            document=FSInputFile(zip_file), 
            caption=caption,
            parse_mode="HTML"
        )
        
        try:
            os.remove(zip_file)
            shutil.rmtree(res_dir)
            shutil.rmtree(latest)
        except: pass
        await status_message.delete()
        
    except Exception as e:
        logger.exception(f"Task processing error for {url}")
        await original_message.answer(f"Ошибка обработки: {e}")
    finally:
        logger.remove(sink_id)

async def process_kw_task(message: Message, url: str):
    """
    Обрабатывает запрос на извлечение ключевых слов из ссылки.
    """
    status = await message.answer("🔍 Извлечение ключевых слов... Пожалуйста, подождите.")
    
    try:
        # Инициализация экстрактора с прокси из конфига
        proxy_url = config.data.scraper.proxy_url or None
        extractor = RSOCExtractor(proxy=proxy_url)
        
        # Получаем ключевые слова
        keywords = await extractor.process_link(url)
        logger.info(f"KW Task: Извлечено {len(keywords)} слов из {url}")
        
        if not keywords:
            return await status.edit_text("❌ Ключевые слова не найдены на этой странице или доступ заблокирован.\n\n<i>Проверьте, не защищен ли сайт Cloudflare или другими системами защиты.</i>", parse_mode="HTML")
        
        # Сортировка и форматирование результата
        keywords.sort()
        
        result_text = f"✅ <b>Найденные ключевые слова ({len(keywords)}):</b>\n\n"
        
        # Все ключевые слова в одном моноширинном блоке через новую строку
        # Тег <pre> в Telegram создает блок, который копируется целиком одним нажатием
        all_kw_str = "\n".join(keywords)
        result_text += f"<pre>{all_kw_str}</pre>"
        
        # Если текст слишком длинный для одного сообщения (лимит TG ~4096 симв)
        if len(result_text) > 4000:
            # Можно отправить файлом или разбить, отправим файлом для надежности
            file_path = f"keywords_{int(time.time())}.txt"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(keywords))
            
            await message.reply_document(
                document=FSInputFile(file_path),
                caption=f"✅ Найдено {len(keywords)} ключевых слов (отправлено файлом из-за объема)."
            )
            os.remove(file_path)
            await status.delete()
        else:
            await status.edit_text(result_text, parse_mode="HTML")
            
    except Exception as e:
        logger.exception(f"KW extraction error for {url}")
        await status.edit_text(f"❌ Произошла ошибка при извлечении: {e}")
