import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

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
    
    def __init__(self, http_client: httpx.AsyncClient, endpoint_url: str, doc_ids: dict):
        self.client = http_client
        self.endpoint_url = endpoint_url
        self.doc_ids = doc_ids
        self.initial_variables = {}

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
        while next_page.cursor:
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
                vars_["cursor"] = current_page.cursor
                doc_id = current_page.doc_id or self.doc_ids["pagination"]
                payload = {"variables": json.dumps(vars_), "doc_id": doc_id}
                
                await config.IP_READY_EVENT.wait()
                resp = await self.client.post(self.endpoint_url, data=payload)
                resp.raise_for_status()
                
                data = resp.json()
                return GraphQLPage(cursor=extract_cursor(data), raw_creatives=extract_creatives_from_pagination(data))
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
                    await asyncio.sleep(1)
                    
                    # Прокрутка для активации (с защитой от разрушения контекста)
                    for _ in range(3):
                        try:
                            if page.is_closed(): break
                            await page.evaluate("window.scrollBy(0, 1000)")
                            await asyncio.sleep(0.6)
                        except: break
                    
                    await page.wait_for_timeout(2000)

                    for resp in gql_responses:
                        try:
                            # Проверяем статус ответа перед попыткой парсинга
                            if resp.status != 200:
                                continue
                                
                            data = await resp.json()
                            raw = extract_creatives(data)
                            if raw:
                                req_body = resp.request.post_data
                                vars_ = extract_variables(req_body)
                                
                                # Извлекаем doc_id из тела запроса (JSON или form-urlencoded)
                                doc_id = None
                                try:
                                    req_json = resp.request.post_data_json
                                    if req_json: doc_id = str(req_json.get("doc_id", ""))
                                except: pass
                                
                                if not doc_id and isinstance(req_body, str):
                                    from urllib.parse import parse_qs
                                    params = parse_qs(req_body)
                                    doc_id = params.get("doc_id", [None])[0]

                                logger.info(f"Данные успешно перехвачены из GQL для {url} (doc_id: {doc_id})")
                                return GraphQLPage(
                                    cursor=extract_cursor(data), 
                                    raw_creatives=raw, 
                                    variables=vars_,
                                    doc_id=doc_id
                                )
                        except Exception as e:
                            logger.debug(f"GQL Parse Error for {url}: {e}")
                    
                    # Пытаемся сразу найти данные в HTML, если GQL не сработал или не полон
                    logger.debug(f"GQL не дал результатов для {url}, пробуем парсить HTML...")
                    html_content = await page.content()
                    info = extract_script_info(html_content)
                    if info:
                        creatives = extract_creatives(info)
                        if creatives:
                            logger.info(f"Данные успешно извлечены из скриптов HTML для {url}")
                            return GraphQLPage(raw_creatives=creatives, variables=extract_variables(info))

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

        # Запускаем RSOC и Reaches одновременно
        await asyncio.gather(
            _run_enrichment(),
            *[self._proc_rsoc(g, extractor, gql_client.client) for g in groups if g.link_url]
        )

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
                    if creative.transparency_data:
                        logger.debug(f"Данные для {creative.ad_archive_id} получены успешно.")
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
        """Суммирует данные об охватах по странам (EU и UK)."""
        total_found = 0
        for group in groups:
            eu_map, uk_map = {}, {}
            for c in group.creatives:
                if not c.transparency_data: continue
                
                try:
                    ad_details = c.transparency_data.get("ad_library_main", {}).get("ad_details", {})
                    transparency = ad_details.get("transparency_by_location", {})
                    if not transparency: continue

                    for region_code in ("eu", "uk"):
                        reg_data = transparency.get(f"{region_code}_transparency")
                        if not reg_data: continue

                        breakdown = reg_data.get("age_country_gender_reach_breakdown", [])
                        for country_dict in breakdown:
                            country = country_dict.get("country")
                            if not country: continue

                            summary = 0
                            for bd_item in country_dict.get("age_gender_breakdowns", []):
                                summary += sum(bd_item.get(g, 0) or 0 for g in ("male", "female", "unknown"))
                            
                            if summary > 0:
                                target_map = eu_map if region_code == "eu" else uk_map
                                target_map[country] = target_map.get(country, 0) + summary
                                total_found += summary
                                
                except Exception as e:
                    logger.error(f"Ошибка агрегации статистики для {c.ad_archive_id}: {e}")

            group.total_reaches = {"eu": eu_map, "uk": uk_map}
        
        if total_found == 0:
            logger.warning("Агрегация: охваты не найдены. Facebook вернул пустые данные или иное ГЕО.")
        else:
            logger.info(f"Агрегация завершена. Суммарный охват: {total_found}")

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
        browser = await p.chromium.launch(headless=True)
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
                    gql = GraphQLClient(client, config.data.facebook_api.endpoint_url, config.data.facebook_api.doc_ids)
                    raw = await gql.fetch_all_creatives(init)
                    if not raw:
                        logger.warning(f"Объявления не найдены для {url}. Пропуск.")
                        continue
                        
                    groups = await scraper.process_creatives(gql, raw)
                    await scraper.enrich_groups(groups, gql)
                    
                    data_file = results_dir / f"data_{int(datetime.now().timestamp())}.json"
                    with open(data_file, "w", encoding="utf-8") as f:
                        json.dump({url: {g.link_url or f"group_{idx}": {
                            "video_urls": [v for c in g.creatives for v in c.video_urls],
                            "img_urls": [i for c in g.creatives for i in c.image_urls],
                            "ad_texts": list({c.text for c in g.creatives if c.text}),
                            "total_reaches": g.total_reaches or {"eu": {}, "uk": {}},
                            "rsoc_keywords": g.rsoc_keywords,
                            "start_date": g.creatives[0].start_date if g.creatives else None
                        } for idx, g in enumerate(groups)}}, f, ensure_ascii=False, indent=2)
                        
            return str(results_dir)
        finally:
            try: await browser.close()
            except: pass
