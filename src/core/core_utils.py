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
    """Находит курсор пагинации (end_cursor) в данных."""
    result = recursively_extract_value(data, "end_cursor")
    return result[0] if result else None

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
        from urllib.parse import parse_qs
        params = parse_qs(data)
        vars_str = params.get("variables", [None])[0]
        if vars_str:
            try: return json.loads(vars_str)
            except: return None
        return None
        
    results = recursively_extract_value(data, "variables")
    for val in results:
        if not val: continue
        if isinstance(val, str):
            try: 
                parsed = json.loads(val)
                if isinstance(parsed, dict) and (parsed.get("ad_type") or parsed.get("q")):
                    return parsed
            except: continue
        elif isinstance(val, dict) and (val.get("ad_type") or val.get("q")):
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
    body = snapshot.get("body") or {}
    if text := body.get("text"):
        if "{{product.brand}}" not in text and "{{product.name}}" not in text: return text
    for card in snapshot.get("cards", []):
        card_body = card.get("body")
        if isinstance(card_body, dict) and (text := card_body.get("text")): return text
        elif isinstance(card_body, str): return card_body
    return body.get("text")

def extract_creatives_from_pagination(data: dict):
    """Извлекает креативы из ответа пагинации GraphQL."""
    try:
        edges = data["data"]["ad_library_main"]["search_results_connection"]["edges"]
        return [edge["node"]["collated_results"] for edge in edges if edge.get("node", {}).get("collated_results")]
    except: return []
