import json
from typing import Any
from bs4 import BeautifulSoup

def recursively_extract_value(data, key_name: str) -> list:
    """Рекурсивно ищет все значения по ключу key_name во вложенных словарях и списках."""
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

def extract_script_info(html: str, marker: str = "collated_results"):
    """Извлекает JSON данные из тегов <script>, проверяя наличие заданного маркера."""
    soup = BeautifulSoup(html, 'lxml')
    for script_tag in soup.find_all('script'):
        text = script_tag.string
        if text and marker in text:
            try: return json.loads(text)
            except: continue
    return None

def extract_creatives(data: Any): 
    """Извлекает список креативов из JSON структуры или HTML строки."""
    if isinstance(data, str):
        # Если это HTML, пробуем найти скрипт с данными
        json_data = extract_script_info(data)
        if json_data: return extract_creatives(json_data)
        return []
        
    return recursively_extract_value(data, "collated_results")

def extract_cursor(data: dict):
    """Находит курсор пагинации (end_cursor) в данных, выбирая непустой."""
    results = recursively_extract_value(data, "end_cursor")
    from loguru import logger
    if results: logger.debug(f"Found potential cursors: {results}")
    for res in results:
        if res and isinstance(res, str): return res
    return None

def extract_variables(data: Any):
    """Извлекает переменные запроса (variables) для последующих вызовов API."""
    if isinstance(data, str):
        # 1. Проверяем, не JSON ли это строка
        try:
            potential_json = json.loads(data)
            if isinstance(potential_json, (dict, list)):
                return extract_variables(potential_json)
        except: pass
        
        # 2. Если это HTML, ищем в скриптах (сначала пробуем найти блок с переменными)
        if "<html" in data.lower() or "<script" in data.lower():
            # Попытка найти блок, где явно указаны variables
            json_data = extract_script_info(data, "variables")
            if not json_data:
                # Если не нашли, пробуем стандартный блок с результатами
                json_data = extract_script_info(data, "collated_results")
                
            if json_data: return extract_variables(json_data)

        # 3. Пробуем как query string / form-data (URL параметры)
        from urllib.parse import parse_qs, urlparse
        parsed_url = urlparse(data)
        params = parse_qs(parsed_url.query if parsed_url.query else data)
        
        vars_str = params.get("variables", [None])[0]
        if vars_str:
            try: return json.loads(vars_str)
            except: pass
            
        # Fallback: пробуем собрать переменные из отдельных параметров URL
        fallback_vars = {}
        if pid := params.get("view_all_page_id", [None])[0]: fallback_vars["viewAllPageID"] = pid
        if ad_type := params.get("ad_type", [None])[0]: fallback_vars["adType"] = ad_type.upper()
        if country := params.get("country", [None])[0]: fallback_vars["countries"] = [country]
        if status := params.get("active_status", [None])[0]: fallback_vars["activeStatus"] = status
        
        if fallback_vars:
            import uuid
            # Базовые значения для пагинации
            fallback_vars.update({
                "count": 30,
                "first": 30, 
                "cursor": None, 
                "searchType": "page", 
                "mediaType": "all",
                "publisherPlatforms": [],
                "pageIDs": [],
                "bylines": [],
                "contentLanguages": [],
                "excludedIDs": None,
                "isTargetedCountry": False,
                "location": None,
                "multiCountryFilterMode": None,
                "potentialReachInput": None,
                "queryString": "",
                "regions": None,
                "sessionID": str(uuid.uuid4()),
                "startDate": None,
                "v": "3ba1ef",
                "sortData": {"mode": "SORT_BY_TOTAL_IMPRESSIONS", "direction": "DESCENDING"}
            })
            return fallback_vars
            
        return None
        
    results = recursively_extract_value(data, "variables")
    for val in results:
        if not val: continue
        if isinstance(val, str):
            try: 
                parsed = json.loads(val)
                if isinstance(parsed, dict) and (parsed.get("ad_type") or parsed.get("q") or parsed.get("view_all_page_id")):
                    return parsed
            except: continue
        elif isinstance(val, dict) and (val.get("ad_type") or val.get("q") or val.get("view_all_page_id")):
            return val
            
    return results[0] if results and results[0] else None

def extract_video_urls(creative_dict: dict):
    """Извлекает ссылки на видео (SD качество) из снепшота или карточек."""
    snapshot = creative_dict.get("snapshot", {})
    if not snapshot: return []
    urls = {v.get("video_sd_url") for v in snapshot.get("videos", []) if v.get("video_sd_url")}
    urls.update({c.get("video_sd_url") for c in snapshot.get("cards", []) if c.get("video_sd_url")})
    return list(urls)

def extract_image_urls(creative_dict: dict):
    """Извлекает ссылки на изображения из снепшота или карточек."""
    snapshot = creative_dict.get("snapshot", {})
    if not snapshot: return []
    urls = {i.get("resized_image_url") for i in snapshot.get("images", []) if i.get("resized_image_url")}
    urls.update({c.get("resized_image_url") for c in snapshot.get("cards", []) if c.get("resized_image_url")})
    return list(urls)

def extract_text(creative_dict: dict):
    """Извлекает основной рекламный текст, игнорируя шаблоны."""
    snapshot = creative_dict.get("snapshot", {})
    if not snapshot: return None
    def _valid_text(t: str) -> bool:
        return isinstance(t, str) and t.strip() and "{{product.brand}}" not in t and "{{product.name}}" not in t

    body = snapshot.get("body") or {}
    if _valid_text(body.get("text")):
        return body.get("text").strip()

    for card in snapshot.get("cards", []):
        card_body = card.get("body")
        if isinstance(card_body, dict) and _valid_text(card_body.get("text")):
            return card_body.get("text").strip()
        if isinstance(card_body, str) and _valid_text(card_body):
            return card_body.strip()
        for key in ("title", "link_description", "caption", "subcaption"):
            if _valid_text(card.get(key)):
                return card.get(key).strip()

    for key in ("title", "link_description", "caption", "subcaption", "headline"):
        if _valid_text(snapshot.get(key)):
            return snapshot.get(key).strip()

    return body.get("text")

def extract_creatives_from_pagination(data: dict):
    """Извлекает креативы из ответа пагинации GraphQL используя рекурсивный поиск."""
    return recursively_extract_value(data, "collated_results")
