import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

import httpx
from loguru import logger
from playwright.async_api import async_playwright, Browser, Response

from src.core.config import config_instance as config
from src.core.models import GraphQLPage, Creative, AdGroup
from src.core.rsoc import RSOCExtractor
from src.core.core_utils import (
    extract_script_info, extract_creatives, extract_cursor, 
    extract_variables, extract_video_urls, extract_image_urls, 
    extract_text, extract_creatives_from_pagination
)

class RateLimitExceededError(Exception):
    """Исключение при превышении лимитов запросов."""
    pass

class GraphQLClient:
    """Клиент для взаимодействия с GraphQL API Facebook."""
    
    def __init__(self, http_client: httpx.AsyncClient, endpoint_url: str, doc_ids: dict, cookies: dict = None, lsd: str = None):
        self.client = http_client
        self.endpoint_url = endpoint_url
        self.doc_ids = doc_ids
        self.initial_variables = {}
        self.lsd = lsd
        if cookies:
            self.client.cookies.update(cookies)

    async def fetch_all_creatives(self, initial_page: GraphQLPage) -> list:
        """Собирает все объявления, проходя по всем страницам пагинации."""
        if initial_page is None:
            logger.error("fetch_all_creatives: initial_page is None!")
            return []
            
        if not initial_page.variables:
            logger.warning("fetch_all_creatives: initial_page.variables is empty or None")
            self.initial_variables = {}
        else:
            self.initial_variables = initial_page.variables.copy()
            
        seen_ids, all_creatives = set(), []
        
        # Обработка начальной страницы
        for chunk in initial_page.raw_creatives:
            if not chunk: continue
            cid = chunk[0].get("collation_id") or chunk[0].get("ad_archive_id")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                all_creatives.append(chunk)
                
        logger.debug(f"Начальная страница: найдено {len(all_creatives)} элементов")
        
        next_page = initial_page
        logger.info(f"Начало пагинации. Стартовый курсор: {next_page.cursor}")
        
        while next_page.cursor:
            logger.debug(f"Запрос следующей страницы с курсором: {next_page.cursor[:20]}...")
            next_page = await self._fetch_next_page(next_page)
            
            if next_page.raw_creatives:
                for chunk in next_page.raw_creatives:
                    if not chunk: continue
                    cid = chunk[0].get("collation_id") or chunk[0].get("ad_archive_id")
                    if cid and cid not in seen_ids:
                        seen_ids.add(cid)
                        all_creatives.append(chunk)
                logger.debug(f"Подгружено скроллом. Всего сейчас: {len(all_creatives)}")
            else:
                logger.warning("Курсор есть, но новые объявления не найдены.")
                break
                
        return all_creatives

    async def _fetch_next_page(self, current_page: GraphQLPage) -> GraphQLPage:
        """Запрашивает следующую страницу результатов."""
        for i in range(config.data.scraper.retries_per_creative):
            try:
                vars_ = self.initial_variables.copy()
                # Форсируем лимит, чтобы не застревать на порциях по 6 штук
                for limit_key in ["count", "first", "limit"]:
                    if limit_key in vars_:
                        vars_[limit_key] = 30
                
                vars_["cursor"] = current_page.cursor
                doc_id = current_page.doc_id or self.doc_ids["pagination"]
                payload = {"variables": json.dumps(vars_), "doc_id": doc_id}
                if self.lsd: payload["lsd"] = self.lsd
                
                logger.debug(f"GQL Payload: doc_id={doc_id}, variables keys={list(vars_.keys())}")
                
                await config.IP_READY_EVENT.wait()
                resp = await self.client.post(self.endpoint_url, data=payload)
                
                # Сохраняем дамп для отладки, если пагинация вернула пустоту или мало данных
                if resp.status_code == 200:
                    data = resp.json()
                    new_creatives = extract_creatives_from_pagination(data)
                    new_cursor = extract_cursor(data)
                    
                    logger.debug(f"GQL Response: status={resp.status_code}, found_creatives={len(new_creatives)}, new_cursor={new_cursor}")
                    
                    # Проверка на наличие ошибок в ответе (например, Rate Limit)
                    errors = data.get("errors", [])
                    if errors:
                        for err in errors:
                            msg = err.get("message", "")
                            if "Rate limit" in msg or err.get("code") == 1675004:
                                logger.error("❌ Facebook ограничил запросы (Rate Limit). Попробуйте сменить IP или подождать 10-15 мин.")
                            else:
                                logger.warning(f"Ошибка GQL: {msg}")
                        return GraphQLPage(cursor=None, raw_creatives=[])

                    # Если данных нет или мало (структурная пустота), дампим для анализа
                    if not new_creatives:
                        dump_path = Path("tmp_gql_dumps")
                        dump_path.mkdir(exist_ok=True)
                        with open(dump_path / f"pagination_empty_{int(datetime.now().timestamp())}.json", "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        logger.warning("Пагинация пуста. Возможно, достигнут конец списка или структура изменилась.")

                    return GraphQLPage(cursor=new_cursor, raw_creatives=new_creatives)
                
                resp.raise_for_status()
            except (httpx.ConnectError, httpx.ProxyError, httpx.HTTPError) as e:
                is_conn = isinstance(e, (httpx.ConnectError, httpx.ProxyError))
                wait = (2 ** i) + 1 if is_conn else 1
                logger.warning(f"Ошибка получения страницы (попытка {i+1}): {e}. Ждем {wait}с")
                await asyncio.sleep(wait)
        return GraphQLPage()

    async def fetch_all_creatives_from_collation(self, collation_id: str) -> list:
        """Извлекает все карточки из группы (collation)."""
        all_cards, next_cursor, has_next = [], None, True
        while has_next:
            vars_ = self.initial_variables.copy()
            vars_["collationGroupID"] = collation_id
            vars_["forwardCursor"] = next_cursor
            payload = {"variables": json.dumps(vars_), "doc_id": self.doc_ids["collation"]}
            if self.lsd: payload["lsd"] = self.lsd
            
            for i in range(config.data.scraper.retries_per_creative):
                try:
                    await config.IP_READY_EVENT.wait()
                    resp = await self.client.post(self.endpoint_url, data=payload)
                    resp.raise_for_status()
                    
                    data = resp.json()
                    res = data.get("data", {}).get("ad_library_main", {}).get("collation_results", {})
                    all_cards.extend(res.get("ad_cards", []))
                    next_cursor = res.get("forward_cursor")
                    has_next = bool(next_cursor)
                    break 
                except (httpx.ConnectError, httpx.ProxyError, httpx.HTTPError) as e:
                    is_conn = isinstance(e, (httpx.ConnectError, httpx.ProxyError))
                    wait = (2 ** i) + 1 if is_conn else 1
                    logger.warning(f"Ошибка collation {collation_id} (попытка {i+1}): {e}. Ждем {wait}с")
                    await asyncio.sleep(wait)
                    if i == config.data.scraper.retries_per_creative - 1:
                        has_next = False
            else:
                has_next = False

        return all_cards

    async def fetch_creative_info(self, ad_id: str) -> dict:
        """Запрашивает детальную информацию об объявлении (прозрачность, гео)."""
        vars_ = self.initial_variables.copy()
        vars_["adArchiveID"] = ad_id
        payload = {"variables": json.dumps(vars_), "doc_id": self.doc_ids["creative_info"]}
        if self.lsd: payload["lsd"] = self.lsd
        
        await config.IP_READY_EVENT.wait()
        resp = await self.client.post(self.endpoint_url, data=payload)
        resp.raise_for_status()
        return resp.json()

class Scraper:
    """Основной класс парсера для сбора данных из Facebook Ad Library."""
    
    def __init__(self):
        self.enricher_semaphore = asyncio.Semaphore(config.data.scraper.concurrent_requests)

    @property
    def proxy(self) -> Optional[str]:
        return config.data.scraper.proxy_url or None

    async def _get_external_ip(self) -> Optional[str]:
        """Определяет текущий внешний IP через прокси."""
        if not self.proxy: return "local"
        for ep in ["http://checkip.amazonaws.com", "http://ipinfo.io/ip"]:
            try:
                async with httpx.AsyncClient(timeout=10, proxy=self.proxy) as client:
                    resp = await client.get(ep)
                    if resp.status_code == 200: return resp.text.strip()
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                logger.debug(f"Сетевая ошибка при определении IP через {ep}: {e}")
            except Exception as e:
                logger.debug(f"Ошибка определения IP через {ep}: {e}")
        return "Local (Error/Offline)"

    async def _change_proxy_ip(self) -> Optional[str]:
        """Триггерит смену IP прокси, если настроен URL для смены."""
        if not config.data.scraper.proxy_change_url or not self.proxy:
            logger.debug("Смена IP пропущена: URL смены IP не настроен или прокси не используется.")
            return None
            
        if not config.IP_READY_EVENT.is_set():
            await config.IP_READY_EVENT.wait()
            return config.LAST_PROXY_IP
            
        async with config.IP_CHANGE_LOCK:
            config.IP_READY_EVENT.clear()
            try:
                old_ip = await self._get_external_ip()
                logger.info(f"Запуск смены IP. Текущий: {old_ip}")
                
                for i in range(5):
                    try: 
                        async with httpx.AsyncClient() as client:
                            await client.get(config.data.scraper.proxy_change_url, timeout=30)
                    except Exception as e:
                        logger.debug(f"Ошибка запроса на смену IP: {e}")
                        
                    await asyncio.sleep(10) # Даем время на переподключение
                    new_ip = await self._get_external_ip()
                    if new_ip and new_ip != old_ip:
                        logger.success(f"IP успешно изменен: {new_ip}")
                        config.LAST_PROXY_IP = new_ip
                        return new_ip
                
                logger.warning("Не удалось изменить IP после 5 попыток")
                return None
            finally:
                config.IP_READY_EVENT.set()

    async def get_initial_data(self, browser: Browser, url: str) -> GraphQLPage:
        """Инициализирует сессию в браузере и перехватывает POST-запросы к GraphQL."""
        page = await browser.new_page()
        gql_responses = []

        async def handle_response(response: Response):
            if "graphql" in response.url and response.request.method == "POST":
                if response.status == 200:
                    gql_responses.append(response)

        async def handle_route(route):
            if route.request.resource_type in ["image", "stylesheet", "font", "media", "imageset"]:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", handle_route)
        page.on("response", handle_response)
        
        try:
            for i in range(3):
                try:
                    gql_responses.clear()
                    await page.goto(url, wait_until="load", timeout=45000)
                    
                    # Небольшая пауза перед скроллом для стабильности контекста
                    await asyncio.sleep(2)
                    
                    # Улучшенная прокрутка для гарантированного перехвата GQL
                    scroll_iterations = getattr(config.data.scraper, "scroll_iterations", 3)
                    logger.debug(f"Запуск прокрутки: {scroll_iterations} итераций")
                    
                    for s in range(scroll_iterations):
                        try:
                            if page.is_closed(): break
                            # Скроллим на разную высоту для имитации поведения пользователя
                            distance = 1000 + (s * 200)
                            await page.evaluate(f"window.scrollBy(0, {distance})")
                            # Случайная пауза для загрузки контента
                            await asyncio.sleep(0.8 + (s % 2) * 0.4)
                            
                            if (s + 1) % 5 == 0:
                                logger.debug(f"Прокрутка: итерация {s+1}/{scroll_iterations}...")
                        except: break
                    
                    await page.wait_for_timeout(3000)

                    best_page = GraphQLPage()
                    max_creatives = 0

                    for resp in gql_responses:
                        try:
                            if resp.status != 200: continue
                                
                            data = await resp.json()
                            raw = extract_creatives(data)
                            if not raw: continue

                            # Считаем количество найденных креативов
                            creatives_count = len(raw)
                            logger.debug(f"Найдено {creatives_count} креативов в GQL ответе")

                            req_body = resp.request.post_data
                            vars_ = extract_variables(req_body)
                            
                            # Извлекаем doc_id
                            doc_id = None
                            try:
                                req_json = resp.request.post_data_json
                                if req_json: doc_id = str(req_json.get("doc_id", ""))
                            except: pass
                            
                            if not doc_id and isinstance(req_body, str):
                                from urllib.parse import parse_qs
                                params = parse_qs(req_body)
                                doc_id = params.get("doc_id", [None])[0]

                            # Если этот ответ содержит больше данных или мы еще ничего не нашли
                            if creatives_count > max_creatives or (creatives_count > 0 and not best_page.raw_creatives):
                                max_creatives = creatives_count
                                best_page = GraphQLPage(
                                    cursor=extract_cursor(data), 
                                    raw_creatives=raw, 
                                    variables=vars_,
                                    doc_id=doc_id
                                )
                                logger.debug(f"Выбран лучший GQL ответ (креативов: {max_creatives}, doc_id: {doc_id})")
                        except Exception as e:
                            logger.debug(f"GQL Parse Error for {url}: {e}")

                    # [NEW] Извлечение сессионных данных (Cookies и LSD)
                    import re
                    html_content = await page.content()
                    lsd_match = re.search(r'["\']LSD["\'],\[\],\{["\']token["\']:["\']([^"\']+)["\']\}', html_content)
                    lsd_token = lsd_match.group(1) if lsd_match else None
                    if not lsd_token:
                        lsd_match = re.search(r'["\']lsd["\']:["\']([^"\']+)["\']', html_content)
                        lsd_token = lsd_match.group(1) if lsd_match else None
                    
                    cookies = {c["name"]: c["value"] for c in await page.context.cookies()}
                    logger.debug(f"Сессия получена: LSD={lsd_token[:10] if lsd_token else 'None'}..., Cookies count={len(cookies)}")

                    if best_page.raw_creatives:
                        best_page.lsd = lsd_token
                        best_page.cookies = cookies
                        logger.info(f"Данные успешно перехвачены из GQL для {url} (всего {max_creatives} креативов)")
                        return best_page
                    
                    # Пытаемся сразу найти данные в HTML, если GQL не сработал или не полон
                    logger.debug(f"GQL не дал результатов для {url}, пробуем парсить HTML...")
                    info = extract_script_info(html_content)
                    if info:
                        creatives = extract_creatives(info)
                        if creatives:
                            logger.info(f"Данные успешно извлечены из скриптов HTML для {url} (всего {len(creatives)} креативов)")
                            vars_ = extract_variables(info)
                            if not vars_ or not (vars_.get("view_all_page_id") or vars_.get("viewAllPageID")):
                                logger.debug(f"Скрипты не дали переменных для {url}, пробуем URL fallback...")
                                vars_ = extract_variables(url)
                            
                            return GraphQLPage(
                                cursor=extract_cursor(info),
                                raw_creatives=creatives, 
                                variables=vars_, 
                                lsd=lsd_token, 
                                cookies=cookies
                            )

                except Exception as e:
                    logger.warning(f"Попытка инициализации {i+1} для {url} не удалась: {e}")
            
            logger.error(f"Все попытки инициализации для {url} провалены.")
            # Если после всех попыток ничего не нашли, возвращаем пустой объект
            return GraphQLPage()
        finally:
             await page.close()

    async def process_creatives(self, client: GraphQLClient, raw_creatives: list) -> List[AdGroup]:
        """Обрабатывает сырые данные объявлений, объединяя их в группы."""
        processed_ids = set()
        
        async def _proc_chunk(chunk):
            if not chunk: return None
            data = chunk[0]
            
            # Если это группа похожих объявлений
            if (data.get("collation_count") or 0) > 1:
                group = AdGroup(link_url=data.get("snapshot", {}).get("link_url"), collation_id=data.get("collation_id"))
                for i in range(3):
                    try:
                        cards = await client.fetch_all_creatives_from_collation(group.collation_id)
                        for card in cards:
                            aid = card.get("ad_archive_id")
                            if aid and aid not in processed_ids:
                                processed_ids.add(aid)
                                group.creatives.append(Creative(
                                    ad_archive_id=aid, 
                                    video_urls=extract_video_urls(card), 
                                    image_urls=extract_image_urls(card), 
                                    text=extract_text(card), 
                                    start_date=card.get("start_date")
                                ))
                        return group
                    except Exception as e: 
                        logger.warning(f"Ошибка при обработке группы {group.collation_id} (попытка {i+1}): {e}")
                return group
            else:
                # Одиночное объявление
                aid = data.get("ad_archive_id")
                if aid and aid not in processed_ids:
                    processed_ids.add(aid)
                    return AdGroup(
                        link_url=data.get("snapshot", {}).get("link_url"), 
                        creatives=[Creative(
                            ad_archive_id=aid, 
                            video_urls=extract_video_urls(data), 
                            image_urls=extract_image_urls(data), 
                            text=extract_text(data), 
                            start_date=data.get("start_date")
                        )]
                    )
            return None

        res = await asyncio.gather(*[_proc_chunk(c) for c in raw_creatives])
        final_groups = [g for g in res if g and g.creatives]
        
        logger.success(f"Успешно обработано {len(final_groups)} групп объявлений")
        return final_groups

    async def enrich_groups(self, groups: List[AdGroup], gql_client: GraphQLClient):
        """Обогащает группы данными о просмотрах (reaches) и ключевыми словами RSOC."""
        logger.info("Сбор детальной статистики (просмотры, ГЕО)...")
        creatives = [c for g in groups for c in g.creatives if not c.transparency_data]
        
        pending = creatives
        

        # Объединяем сбор охватов и RSOC в параллельное выполнение
        extractor = RSOCExtractor(proxy=self.proxy)
        logger.info("Сбор статистики и анализ ссылок (RSOC)...")
        
        async def _run_enrichment():
            nonlocal pending
            global_retries = 0
            max_global_retries = 10
            
            while pending and global_retries < max_global_retries:
                tasks = [self._enrich_one(c, gql_client) for c in pending]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                to_retry = []
                for c, res in zip(pending, results):
                    if isinstance(res, RateLimitExceededError): 
                        to_retry.append(c)
                    elif isinstance(res, Exception): 
                        logger.error(f"Ошибка деталей {c.ad_archive_id}: {res}")
                
                if to_retry:
                    global_retries += 1
                    logger.info(f"Рейт-лимит (попытка {global_retries}/{max_global_retries}). Ожидание и смена IP...")
                    
                    # Пробуем сменить IP
                    new_ip = await self._change_proxy_ip()
                    
                    # Если IP не сменился или смена не настроена, ждем дольше
                    wait_time = 15 if new_ip else 30
                    if global_retries > 5: wait_time *= 2 # Экспоненциальное замедление
                    
                    await asyncio.sleep(wait_time)
                
                pending = to_retry
            
            if pending:
                logger.error(f"Не удалось обогатить {len(pending)} объявлений после {max_global_retries} глобальных попыток.")

        # Обогащаем группы данными о просмотрах (reaches)
        await _run_enrichment()

        # После того как данные загружены, извлекаем link_url для групп, у которых его еще нет
        for g in groups:
            if not g.link_url and g.creatives:
                for c in g.creatives:
                    if c.transparency_data:
                        details = c.transparency_data.get("ad_library_main", {}).get("ad_details", {})
                        if link := details.get("link_url"):
                            g.link_url = link
                            break

        # Теперь можно запускать RSOC, так как у нас появились ссылки
        extractor = RSOCExtractor(proxy=self.proxy)
        await asyncio.gather(*[self._proc_rsoc(g, extractor, gql_client.client) for g in groups if g.link_url])

        # Агрегация охватов выполняется ПОСЛЕ того, как все данные получены
        self._aggregate_reaches(groups)

    async def _enrich_one(self, creative: Creative, gql_client: GraphQLClient):
        async with self.enricher_semaphore:
            for i in range(config.data.scraper.retries_per_creative):
                try:
                    res = await gql_client.fetch_creative_info(creative.ad_archive_id)
                    
                    if "Rate limit exceeded" in str(res):
                        raise RateLimitExceededError()
                        
                    creative.transparency_data = res.get("data", {})
                    if not creative.transparency_data:
                        logger.warning(f"Facebook вернул пустые данные (data is empty) для {creative.ad_archive_id}")
                    else:
                         # Логируем наличие ключа aaa_info для отладки "пропавших просмотров"
                         ad_dt = creative.transparency_data.get("ad_library_main", {}).get("ad_details", {})
                         has_aaa = "aaa_info" in ad_dt
                         has_tpl = "transparency_by_location" in ad_dt
                         logger.debug(f"Детали для {creative.ad_archive_id} получены. AAA: {has_aaa}, TPL: {has_tpl}")
                         if not has_aaa and not has_tpl:
                             logger.debug(f"Keys in details: {list(ad_dt.keys())}")
                    return
                except (RateLimitExceededError, httpx.ConnectError, httpx.RequestError) as e:
                    is_conn_error = isinstance(e, (httpx.ConnectError, httpx.ProxyError))
                    if is_conn_error:
                        logger.warning(f"Ошибка соединения (попытка {i+1}) для {creative.ad_archive_id}: {e}")
                    
                    if isinstance(e, RateLimitExceededError):
                        raise
                    
                    # Экспоненциальная задержка для сетевых ошибок
                    wait_time = (2 ** i) + 1 if is_conn_error else 0.5
                    await asyncio.sleep(wait_time)
                except Exception as e:
                    logger.debug(f"Попытка {i+1} получения деталей {creative.ad_archive_id} провалена: {e}")
                    await asyncio.sleep(0.5)
            
            raise Exception(f"Не удалось получить детали для {creative.ad_archive_id} после {config.data.scraper.retries_per_creative} попыток")

    def _aggregate_reaches(self, groups: List[AdGroup]):
        """Суммирует данные об охватах по всем доступным регионам (глобально)."""
        total_found = 0
        for group in groups:
            reaches_map = {}
            for c in group.creatives:
                if not c.transparency_data: continue
                
                try:
                    ad_details = c.transparency_data.get("ad_library_main", {}).get("ad_details", {})
                    
                    # 1. Пробуем новую структуру aaa_info (часто встречается в последних версиях FB)
                    aaa_info = ad_details.get("aaa_info", {})
                    if aaa_info:
                        eu_reach = aaa_info.get("eu_total_reach")
                        if isinstance(eu_reach, (int, float)) and eu_reach > 0:
                            reaches_map["EU_TOTAL"] = reaches_map.get("EU_TOTAL", 0) + int(eu_reach)
                            total_found += int(eu_reach)
                        
                        breakdown = aaa_info.get("age_country_gender_reach_breakdown", [])
                        if breakdown:
                            for country_dict in breakdown:
                                country = country_dict.get("country")
                                if not country: continue
                                summary = 0
                                for bd_item in country_dict.get("age_gender_breakdowns", []):
                                    summary += sum(bd_item.get(g, 0) or 0 for g in ("male", "female", "unknown"))
                                if summary > 0:
                                    reaches_map[country] = reaches_map.get(country, 0) + summary
                                    total_found += summary

                    # 2. Пробуем классическую структуру transparency_by_location
                    transparency = ad_details.get("transparency_by_location", {})
                    if not transparency and not aaa_info:
                        logger.debug(f"Transparency/AAA missing for {c.ad_archive_id}. Keys: {list(ad_details.keys())}")
                        continue

                    if transparency:
                        # Ищем все ключи, оканчивающиеся на _transparency
                        for key, reg_data in transparency.items():
                            if not key.endswith("_transparency") or not reg_data: continue
                            
                            logger.debug(f"Parsing transparency for {c.ad_archive_id}, region: {key}")

                            breakdown = reg_data.get("age_country_gender_reach_breakdown", [])
                            if breakdown:
                                for country_dict in breakdown:
                                    country = country_dict.get("country")
                                    if not country: continue

                                    summary = 0
                                    for bd_item in country_dict.get("age_gender_breakdowns", []):
                                        summary += sum(bd_item.get(g, 0) or 0 for g in ("male", "female", "unknown"))
                                    
                                    if summary > 0:
                                        reaches_map[country] = reaches_map.get(country, 0) + summary
                                        total_found += summary
                            else:
                                # Резервный поиск любых числовых данных об охвате для этого региона
                                # Например, в estimated_audience_size или impressions
                                for fallback_key in ("estimated_audience_size", "impressions", "reach"):
                                    if val := reg_data.get(fallback_key):
                                        # val может быть числом или словарем с min/max
                                        amount = 0
                                        if isinstance(val, (int, float)): amount = int(val)
                                        elif isinstance(val, dict): amount = val.get("upper_bound") or val.get("lower_bound") or 0
                                        
                                        if amount > 0:
                                            region_label = key.replace("_transparency", "").upper()
                                            reaches_map[region_label] = reaches_map.get(region_label, 0) + amount
                                            total_found += amount
                                            break
                                
                except Exception as e:
                    logger.error(f"Ошибка агрегации статистики для {c.ad_archive_id}: {e}")
            
            # Сохраняем результат в группу
            group.total_reaches = reaches_map
            group_sum = sum(v for v in reaches_map.values() if isinstance(v, (int, float)))
            if group_sum > 0:
                logger.debug(f"Группа {group.collation_id or 'single'} агрегирована: {group_sum} охватов")
        
        if total_found == 0:
            logger.warning("Агрегация: охваты не найдены ни в одном объявлении.")
        else:
            logger.info(f"Агрегация завершена. Суммарный охват всех групп: {total_found}")

    async def _proc_rsoc(self, group: AdGroup, extractor: RSOCExtractor, http_client: httpx.AsyncClient):
        """Парсит ключевые слова RSOC по ссылке."""
        try:
            if kw := await extractor.process_link(group.link_url, http_client):
                group.rsoc_keywords = list(set(kw))
        except Exception as e:
            logger.debug(f"RSOC Error for {group.link_url}: {e}")

async def main(urls: List[str] = None):
    """Точка входа для запуска парсинга списка URL."""
    scraper = Scraper()
    async with async_playwright() as p:
        browser_type_name = getattr(config.data.scraper, "browser_type", "chromium").lower()
        logger.info(f"Запуск браузера: {browser_type_name}")
        
        proxy_settings = None
        if scraper.proxy:
            parsed = urlparse(scraper.proxy)
            proxy_settings = {
                "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}" if parsed.port else f"{parsed.scheme}://{parsed.hostname}"
            }
            if parsed.username:
                proxy_settings["username"] = parsed.username
            if parsed.password:
                proxy_settings["password"] = parsed.password
                
        if browser_type_name == "firefox":
            browser = await p.firefox.launch(headless=True, proxy=proxy_settings)
        elif browser_type_name == "webkit":
            browser = await p.webkit.launch(headless=True, proxy=proxy_settings)
        else:
            browser = await p.chromium.launch(headless=True, proxy=proxy_settings)
        results_dir = Path("Parser_Results") / datetime.now().strftime("%Y%m%d_%H%M%S")
        results_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            for url in (urls or []):
                logger.debug(f"Начало обработки: {url}")
                init = await scraper.get_initial_data(browser, url)
                if not init or not init.raw_creatives:
                    logger.warning(f"Не удалось инициализировать данные для {url}. Пропуск.")
                    continue
                
                # Настройка лимитов для предотвращения проблем с сокетами на Windows
                conn_limit = config.data.scraper.concurrent_requests
                # Оставляем запас для других запросов
                limits = httpx.Limits(max_keepalive_connections=conn_limit, max_connections=conn_limit + 10)
                async with httpx.AsyncClient(proxy=scraper.proxy, timeout=30, limits=limits, follow_redirects=True) as client:
                    gql = GraphQLClient(
                        client, 
                        config.data.facebook_api.endpoint_url, 
                        config.data.facebook_api.doc_ids,
                        cookies=init.cookies,
                        lsd=init.lsd
                    )
                    raw = await gql.fetch_all_creatives(init)
                    if not raw:
                        logger.warning(f"Объявления не найдены для {url}. Пропуск.")
                        continue
                        
                    groups = await scraper.process_creatives(gql, raw)
                    await scraper.enrich_groups(groups, gql)
                    
                    data_file = results_dir / f"data_{int(datetime.now().timestamp())}.json"
                    output_data = []
                    for idx, g in enumerate(groups):
                        output_data.append({
                            "search_url": url,
                            "ad_url": g.collation_id or (f"ad_{g.creatives[0].ad_archive_id}" if g.creatives else f"group_{idx}"),
                            "link_url": g.link_url,
                            "video_urls": [v for c in g.creatives for v in c.video_urls],
                            "img_urls": [i for c in g.creatives for i in c.image_urls],
                            "ad_texts": list({c.text for c in g.creatives if c.text}),
                            "total_reaches": g.total_reaches or {},
                            "rsoc_keywords": g.rsoc_keywords,
                            "start_date": g.creatives[0].start_date if g.creatives else None
                        })
                    
                    with open(data_file, "w", encoding="utf-8") as f:
                        json.dump(output_data, f, ensure_ascii=False, indent=2)
                        
            return str(results_dir)
        finally:
            try: await browser.close()
            except: pass
