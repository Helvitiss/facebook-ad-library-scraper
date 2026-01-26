import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from playwright.async_api import async_playwright, Browser

from src.core.config import config_instance as config
from src.core.models import GraphQLPage, Creative, AdGroup
from src.core.rsoc import RSOCExtractor

class RateLimitExceededError(Exception):
    pass

# Helper functions
def recursively_extract_value(data, key_name: str) -> list:
    results = []
    def recurse(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == key_name: results.append(value)
                if isinstance(value, (dict, list)): recurse(value)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)): recurse(item)
    recurse(data)
    return results

def extract_script_info(html: str):
    soup = BeautifulSoup(html, 'lxml')
    for script_tag in soup.find_all('script'):
        text = script_tag.string
        if text and "collated_results" in text:
            try: return json.loads(text)
            except: continue
    return None

def extract_creatives(data: dict): return recursively_extract_value(data, "collated_results")
def extract_cursor(data: dict):
    result = recursively_extract_value(data, "end_cursor")
    return result[0] if result else None
def extract_variables(data: dict):
    result = recursively_extract_value(data, "variables")
    return json.loads(result[0]) if result and result[0] else None

def extract_video_urls(creative_dict: dict):
    snapshot = creative_dict.get("snapshot", {})
    if not snapshot: return []
    urls = {v.get("video_sd_url") for v in snapshot.get("videos", []) if v.get("video_sd_url")}
    urls.update({c.get("video_sd_url") for c in snapshot.get("cards", []) if c.get("video_sd_url")})
    return list(urls)

def extract_image_urls(creative_dict: dict):
    snapshot = creative_dict.get("snapshot", {})
    if not snapshot: return []
    urls = {i.get("resized_image_url") for i in snapshot.get("images", []) if i.get("resized_image_url")}
    urls.update({c.get("resized_image_url") for c in snapshot.get("cards", []) if c.get("resized_image_url")})
    return list(urls)

def extract_text(creative_dict: dict):
    snapshot = creative_dict.get("snapshot", {})
    if not snapshot: return None
    body = snapshot.get("body") or {}
    if text := body.get("text"):
        if "{{product.brand}}" not in text and "{{product.name}}" not in text: return text
    for card in snapshot.get("cards", []):
        card_body = card.get("body")
        if isinstance(card_body, dict) and (text := card_body.get("text")): return text
        elif isinstance(card_body, str): return card_body
    return body.get("text")

def extract_creatives_from_pagination(data: dict):
    try:
        edges = data["data"]["ad_library_main"]["search_results_connection"]["edges"]
        return [edge["node"]["collated_results"] for edge in edges if edge.get("node", {}).get("collated_results")]
    except: return []

class GraphQLClient:
    def __init__(self, http_client: httpx.AsyncClient, endpoint_url: str, doc_ids: dict):
        self.client = http_client
        self.endpoint_url = endpoint_url
        self.doc_ids = doc_ids
        self.initial_variables = {}

    async def fetch_all_creatives(self, initial_page: GraphQLPage) -> list:
        self.initial_variables = initial_page.variables.copy()
        seen_ids, all_creatives = set(), []
        for chunk in initial_page.raw_creatives:
            cid = chunk[0].get("collation_id") or chunk[0].get("ad_archive_id")
            if cid and cid not in seen_ids:
                seen_ids.add(cid); all_creatives.append(chunk)
        next_page = initial_page
        while next_page.cursor:
            next_page = await self._fetch_next_page(next_page)
            all_creatives.extend(next_page.raw_creatives)
            logger.debug(f"Fetched from next page: {len(next_page.raw_creatives)} groups")
        return all_creatives

    async def _fetch_next_page(self, current_page: GraphQLPage) -> GraphQLPage:
        for i in range(5):
            try:
                vars = self.initial_variables.copy()
                vars["cursor"] = current_page.cursor
                payload = {"variables": json.dumps(vars), "doc_id": self.doc_ids["pagination"]}
                await config.IP_READY_EVENT.wait()
                resp = await self.client.post(self.endpoint_url, data=payload)
                resp.raise_for_status()
                data = resp.json()
                return GraphQLPage(cursor=extract_cursor(data), raw_creatives=extract_creatives_from_pagination(data))
            except Exception as e:
                logger.warning(f"Error fetching page (try {i+1}): {e}")
        return GraphQLPage()

    async def fetch_all_creatives_from_collation(self, collation_id: str) -> list:
        all_cards, next_cursor, has_next = [], None, True
        while has_next:
            vars = self.initial_variables.copy()
            vars["collationGroupID"] = collation_id
            vars["forwardCursor"] = next_cursor
            payload = {"variables": json.dumps(vars), "doc_id": self.doc_ids["collation"]}
            await config.IP_READY_EVENT.wait()
            resp = await self.client.post(self.endpoint_url, data=payload)
            resp.raise_for_status()
            data = resp.json()
            res = data.get("data", {}).get("ad_library_main", {}).get("collation_results", {})
            all_cards.extend(res.get("ad_cards", []))
            next_cursor = res.get("forward_cursor")
            has_next = bool(next_cursor)
        return all_cards

    async def fetch_creative_info(self, ad_id: str) -> dict:
        vars = self.initial_variables.copy()
        vars["adArchiveID"] = ad_id
        payload = {"variables": json.dumps(vars), "doc_id": self.doc_ids["creative_info"]}
        await config.IP_READY_EVENT.wait()
        resp = await self.client.post(self.endpoint_url, data=payload)
        resp.raise_for_status()
        return resp.json()

class Scraper:
    def __init__(self):
        self.enricher_semaphore = asyncio.Semaphore(config.data.scraper.concurrent_requests)

    @property
    def proxy(self):
        return config.data.scraper.proxy_url or None

    async def _get_external_ip(self) -> Optional[str]:
        if not self.proxy: return "Local IP"
        for ep in ["http://checkip.amazonaws.com", "http://ipinfo.io/ip"]:
            try:
                async with httpx.AsyncClient(timeout=15, proxy=self.proxy) as client:
                    resp = await client.get(ep)
                    if resp.status_code == 200: return resp.text.strip()
            except: pass
        return None

    async def _change_proxy_ip(self) -> Optional[str]:
        if not config.data.scraper.proxy_change_url or not self.proxy: return None
        if not config.IP_READY_EVENT.is_set():
            await config.IP_READY_EVENT.wait(); return config.LAST_PROXY_IP
        async with config.IP_CHANGE_LOCK:
            config.IP_READY_EVENT.clear()
            try:
                old_ip = await self._get_external_ip()
                for i in range(5):
                    try: httpx.get(config.data.scraper.proxy_change_url, timeout=30)
                    except: pass
                    await asyncio.sleep(5)
                    new_ip = await self._get_external_ip()
                    if new_ip and new_ip != old_ip:
                        config.LAST_PROXY_IP = new_ip; return new_ip
                return None
            finally: config.IP_READY_EVENT.set()

    async def get_initial_data(self, browser: Browser, url: str) -> GraphQLPage:
        # browser is managed by caller for now
        page = await browser.new_page()
        try:
            for i in range(3):
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    async with page.expect_response("**/graphql/", timeout=30000) as resp_info:
                        for _ in range(3):
                            await page.evaluate("window.scrollBy(0, 800)"); await asyncio.sleep(0.8)
                    resp = await resp_info.value
                    data, req = await resp.json(), resp.request.post_data_json
                    return GraphQLPage(cursor=extract_cursor(data), raw_creatives=extract_creatives(data), variables=extract_variables(req))
                except Exception as e:
                    logger.warning(f"GraphQL attempt {i+1} failed: {e}")
                html = await page.content()
                info = extract_script_info(html)
                if info and (creatives := extract_creatives(info)):
                    return GraphQLPage(raw_creatives=creatives)
            raise Exception("No creatives found.")
        finally: await page.close()

    async def process_creatives(self, client: GraphQLClient, raw_creatives: list) -> List[AdGroup]:
        processed_ids = set()
        async def _proc_chunk(chunk):
            data = chunk[0]
            if data.get("collation_count", 0) > 1:
                group = AdGroup(link_url=data.get("snapshot", {}).get("link_url"), collation_id=data.get("collation_id"))
                for i in range(5):
                    try:
                        cards = await client.fetch_all_creatives_from_collation(group.collation_id)
                        for card in cards:
                            aid = card.get("ad_archive_id")
                            if aid and aid not in processed_ids:
                                processed_ids.add(aid)
                                group.creatives.append(Creative(ad_archive_id=aid, video_urls=extract_video_urls(card), 
                                                              image_urls=extract_image_urls(card), text=extract_text(card), 
                                                              start_date=card.get("start_date")))
                        return group
                    except Exception as e: logger.warning(f"Group retry {i+1}: {e}")
                return group
            else:
                aid = data.get("ad_archive_id")
                if aid and aid not in processed_ids:
                    processed_ids.add(aid)
                    return AdGroup(link_url=data.get("snapshot", {}).get("link_url"), 
                                   creatives=[Creative(ad_archive_id=aid, video_urls=extract_video_urls(data), 
                                                      image_urls=extract_image_urls(data), text=extract_text(data), 
                                                      start_date=data.get("start_date"))])
            return None
        res = await asyncio.gather(*[_proc_chunk(c) for c in raw_creatives])
        return [g for g in res if g and g.creatives]

    async def enrich_groups(self, groups: List[AdGroup], initial_vars: dict):
        creatives = [c for g in groups for c in g.creatives]
        pending = creatives
        while pending:
            tasks = [self._enrich_one(c, initial_vars) for c in pending]
            results, to_retry = await asyncio.gather(*tasks, return_exceptions=True), []
            for c, res in zip(pending, results):
                if isinstance(res, RateLimitExceededError): to_retry.append(c)
                elif isinstance(res, Exception): logger.error(f"Enrich error {c.ad_archive_id}: {res}")
            pending = to_retry
        
        extractor = RSOCExtractor(proxy=self.proxy)
        await asyncio.gather(*[self._proc_rsoc(g, extractor) for g in groups if g.link_url])

    async def _enrich_one(self, creative: Creative, initial_vars: dict):
        async with self.enricher_semaphore:
            for i in range(config.data.scraper.retries_per_creative):
                try:
                    async with httpx.AsyncClient(timeout=60, proxy=self.proxy) as client:
                        gql = GraphQLClient(client, config.data.facebook_api.endpoint_url, config.data.facebook_api.doc_ids)
                        gql.initial_variables = initial_vars
                        res = await gql.fetch_creative_info(creative.ad_archive_id)
                        if "Rate limit exceeded" in str(res):
                            await self._change_proxy_ip(); raise RateLimitExceededError()
                        creative.transparency_data = res.get("data", {}); return
                except (httpx.RequestError, RateLimitExceededError): pass
            logger.error(f"Failed to enrich {creative.ad_archive_id}")

    async def _proc_rsoc(self, group: AdGroup, extractor: RSOCExtractor):
        async with self.enricher_semaphore:
            if kw := await extractor.process_link(group.link_url):
                group.rsoc_keywords = list(set(kw))

async def main(urls: List[str] = None):
    scraper = Scraper()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        results_dir = Path("Parser_Results") / datetime.now().strftime("%Y%m%d_%H%M%S")
        results_dir.mkdir(parents=True, exist_ok=True)
        
        for url in urls or []:
            init = await scraper.get_initial_data(browser, url)
            
            # Here we must also respect the proxy when doing initial GQL fetch
            proxy_url = config.data.scraper.proxy_url or None
            
            async with httpx.AsyncClient(proxy=proxy_url) as client:
                gql = GraphQLClient(client, config.data.facebook_api.endpoint_url, config.data.facebook_api.doc_ids)
                raw = await gql.fetch_all_creatives(init)
                groups = await scraper.process_creatives(gql, raw)
                await scraper.enrich_groups(groups, gql.initial_variables)
                
                # Simple export for the scraper part
                data_file = results_dir / f"data_{datetime.now().timestamp()}.json"
                with open(data_file, "w", encoding="utf-8") as f:
                    json.dump({url: {g.link_url or f"group_{idx}": {
                        "video_urls": [v for c in g.creatives for v in c.video_urls],
                        "img_urls": [i for c in g.creatives for i in c.image_urls],
                        "ad_texts": list({c.text for c in g.creatives if c.text}),
                        "total_reaches": g.total_reaches or {"eu": {}, "uk": {}},
                        "rsoc_keywords": g.rsoc_keywords,
                        "start_date": g.creatives[0].start_date if g.creatives else None
                    } for idx, g in enumerate(groups)}}, f, ensure_ascii=False, indent=2)
        await browser.close()
        return str(results_dir)
