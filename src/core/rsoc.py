import asyncio
import json
import httpx
import re
import base64
from pathlib import Path
from typing import Optional, List, Dict, Any, Any
from urllib.parse import urlparse, parse_qs

import pycountry
import geonamescache
from loguru import logger

class RSOCExtractor:
    # Ключи с высокой вероятностью содержащие поисковые фразы
    SEARCH_KEYS = {
        "utm_term", "utm_terms", "term", "terms", "keyword", "keywords", "kw", "kws", 
        "query", "queries", "q", "qs", "search", "search_term", "search_terms", 
        "searchQuery", "searchQueries", "rsoc", "keywordList", "keyword_list", 
        "kw_list", "kwList", "termList", "term_list", "queryList", "query_list", 
        "suggestions", "suggests", "suggestedTerms", "suggested_terms", 
        "suggestedQueries", "suggested_queries", "related", "relatedTerms", 
        "related_terms", "relatedQueries", "related_queries", "recommendations", 
        "recommendedTerms", "recommended_terms", "recommendedQueries", 
        "recommended_queries", "seedKeywords", "seed_keywords", "seedTerms", "seed_terms",
        "p", "tid", "click_id", "cid", "subid", "subid1", "subid2", "ad_id", "campaign_id",
        "tkn", "token", "session", "jwt", "payload"
    }

    # Структурные / контекстные ключи только для рекурсии
    CONTEXT_KEYS = {
        "feed", "feedData", "feed_data", "feedItems", "feed_items", "items", "list", 
        "data", "payload", "content", "results", "result", "entries", "entry", "cards", 
        "cardItems", "blocks", "widgets", "modules", "sections", "config", "pageConfig", 
        "page_config", "appConfig", "app_config", "settings", "options", "params", 
        "state", "initialState", "initial_state", "hydration", "__INITIAL_STATE__", 
        "__NEXT_DATA__", "__NUXT__", "pageData", "searchConfig", "search_config"
    }

    # Технический шум, который всегда игнорируется
    BLACKLIST = {
        "facebook", "windows", "desktop", "macintosh", "chrome", "safari", "mozilla", 
        "webkit", "application", "json", "html", "true", "false", "null", "undefined", 
        "object", "array", "string", "number", "boolean", "http", "https", "www", 
        ".com", ".net", ".org", "ads", "advertising", "marketing", "tracker", 
        "redirect", "android", "iphone", "ipad", "linux", "google", "bing", "yahoo",
        "ad_blocked", "device_type", "platform_name", "track_id", "channel", 
        "city", "country", "lang", "language", "network", "target_url", "dest",
        "hs256", "jwt", "unknown", "none", "unrestrictedsharedarraybuffer", 
        "sharedarraybuffer", "arraybuffer", "dataview", "uint8array", "uint16array", 
        "uint32array", "int8array", "int16array", "int32array", "float32array", 
        "float64array", "biguint64array", "bigint64array", "cryptokey", "cryptokeypair"
    }

    SPLIT_PATTERN = r'[,|;\n]|\s*\|\|\s*|\s*::\s*'

    def __init__(self, proxy: Optional[httpx.Proxy] = None):
        self.proxy = proxy
        self.client_kwargs = {
            "timeout": 35.0,
            "follow_redirects": True,
            "proxy": self.proxy,
            "headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            }
        }
        
        # Инициализация гео-данных
        self.countries = {c.name.lower(): c.name for c in pycountry.countries}
        
        # Исключаем очень распространенные слова, которые часто путают с городами
        geo_ignore = {
            "para", "bank", "kredi", "news", "best", "total", "link", "info", "data",
            "online", "shop", "store", "home", "back", "next", "page", "user"
        }
        
        gc = geonamescache.GeonamesCache()
        cities = gc.get_cities()
        self.cities = {
            c['name'].lower() 
            for c in cities.values() 
            if c['population'] > 15000 and len(c['name']) > 2 and c['name'].lower() not in geo_ignore
        }

    def _is_search_key(self, key: str) -> bool:
        """Проверяет, является ли ключ поисковым (с учетом нумерации типа kw1, term2)."""
        k_lower = key.lower()
        if k_lower in self.SEARCH_KEYS or "rsoc" in k_lower:
            return True
        
        # Проверка на нумерованные вариации (kw1, q2, term3 и т.д.)
        for sk in self.SEARCH_KEYS:
            if k_lower.startswith(sk):
                suffix = k_lower[len(sk):]
                if suffix.isdigit():
                    return True
        return False

    def _sanitize_geo(self, text: str) -> str:
        if not text: return text
        words = text.split()
        new_words = []
        for word in words:
            w_clean = word.strip(".,!?;:()[]{}")
            w_lower = w_clean.lower()
            if w_lower in self.countries:
                word = word.replace(w_clean, "{country}")
            elif w_lower in self.cities:
                 word = word.replace(w_clean, "{city}")
            new_words.append(word)
        result = " ".join(new_words)
        text_lower = result.lower()
        for c_lower in self.countries:
            if c_lower in text_lower and " " in c_lower:
                 pattern = re.compile(re.escape(c_lower), re.IGNORECASE)
                 result = pattern.sub("{country}", result)
        return result

    def _is_valid_keyword(self, s: str) -> bool:
        if not s or len(s) < 3: return False
        s_lower = s.lower()
        if s_lower in self.BLACKLIST: return False
        if any(x in s_lower for x in ["://", "www.", ".com", ".net", ".org", ".php", ".js"]): return False
        if len(s) > 120: return False
        if s.isdigit() or re.match(r'^[a-f0-9]{20,}$', s_lower): return False
        if re.search(r'[pbs]_\d{3,}', s_lower): return False
        if re.search(r'0x[0-9a-f]+|[\[\]\(\)\{\}]', s_lower): return False
        if re.search(r'[=+\*<>;]', s_lower): return False
        if s.startswith('_') or s.startswith('$'): return False
        if "window" in s_lower or "document" in s_lower: return False
        
        # Фильтрация длинных непонятных строк (хеши, ID), если они не содержат пробелов
        if len(s) > 20 and " " not in s:
            # Если это одно слово длиннее 20 символов и содержит цифры -> вероятно ID
            if re.search(r'[0-9]', s): return False
            # Если это очень длинное слово (более 35 симв) без пробелов -> вероятно техническое
            if len(s) > 35: return False
            
        return True

    def _unquote_fully(self, text: str) -> str:
        """Рекурсивно декодирует URL-строку до тех пор, пока она не перестанет меняться."""
        if not isinstance(text, str) or not text: return text
        try:
            import urllib.parse
            last = ""
            current = text
            # Максимум 3 итерации, чтобы не попасть в цикл
            for _ in range(3):
                last = current
                current = urllib.parse.unquote(current)
                if current == last: break
            return current
        except:
            return text

    def _split_keywords(self, text: Any) -> list[str]:
        if not text: return []
        if not isinstance(text, str):
            if isinstance(text, (list, tuple)):
                results = []
                for item in text:
                    results.extend(self._split_keywords(item))
                return results
            return []
        
        # Полное декодирование перед обработкой
        text = self._unquote_fully(text)
        
        parts = re.split(self.SPLIT_PATTERN, text)
        cleaned = []
        for p in parts:
            s = p.strip()
            if self._is_valid_keyword(s):
                cleaned.append(self._sanitize_geo(s))
        return cleaned

    async def process_link(self, url: str, http_client: Optional[httpx.AsyncClient] = None) -> list[str]:
        all_keywords = []
        try:
            if http_client:
                # Используем переданный клиент
                response = await http_client.get(url)
                for history_resp in response.history:
                    all_keywords.extend(self.extract_from_url(str(history_resp.url)))
                all_keywords.extend(self.extract_from_url(str(response.url)))
                all_keywords.extend(self.extract_from_html(response.text))
            else:
                # fallback - создаем свой клиент, если внешний не передан
                async with httpx.AsyncClient(**self.client_kwargs) as client:
                    logger.debug(f"RSOC: Запрос к {url} (proxy: {self.proxy})")
                    response = await client.get(url)
                    logger.debug(f"RSOC: Ответ от {url} -> {response.status_code} (len: {len(response.text)})")
                    
                    for history_resp in response.history:
                        all_keywords.extend(self.extract_from_url(str(history_resp.url)))
                    all_keywords.extend(self.extract_from_url(str(response.url)))
                    all_keywords.extend(self.extract_from_html(response.text))
        except Exception as e:
            logger.error(f"RSOC: Критическая ошибка при обработке ссылки {url}: {e}")
        
        unique_results = []
        seen = set()
        for kw in all_keywords:
            if not kw: continue
            # Игнорируем голые плейсхолдеры
            if kw in ("{country}", "{city}", "{}"): continue
            
            low = kw.lower()
            if low not in seen:
                seen.add(low)
                unique_results.append(kw)
        return unique_results

    def extract_from_url(self, url: str) -> list[str]:
        keywords = []
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            for key, values in params.items():
                if self._is_search_key(key):
                    for val in values:
                        keywords.extend(self._split_keywords(val))
                for val in values:
                    keywords.extend(self.extract_from_jwt(val))
        except: pass
        return keywords

    def extract_from_jwt(self, text: str) -> list[str]:
        keywords = []
        # 1. Поиск стандартных JWT
        jwt_pattern = r'([a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)'
        matches = re.findall(jwt_pattern, text)
        
        # 2. Поиск длинных Base64 строк, которые могут быть JSON
        base64_pattern = r'([a-zA-Z0-9+/=]{24,})'
        matches.extend(re.findall(base64_pattern, text))

        for match in set(matches):
            try:
                payload_json = None
                if '.' in match:
                    # Обработка JWT
                    parts = match.split('.')
                    if len(parts) != 3: continue
                    payload_b64 = parts[1]
                else:
                    # Обработка сырого Base64
                    payload_b64 = match

                missing_padding = len(payload_b64) % 4
                if missing_padding: payload_b64 += '=' * (4 - missing_padding)
                
                try:
                    decoded = base64.urlsafe_b64decode(payload_b64).decode('utf-8', errors='ignore')
                except:
                    # Fallback to standard base64 if urlsafe fails
                    decoded = base64.b64decode(payload_b64).decode('utf-8', errors='ignore')

                if '{' in decoded and '}' in decoded:
                    payload_json = json.loads(re.search(r'(\{.*\})', decoded).group(1))
                
                if payload_json and isinstance(payload_json, dict):
                    logger.debug(f"Успешно декодирован токен/Base64. Ключи: {list(payload_json.keys())}")
                    keywords.extend(self._extract_from_dict(payload_json))
            except: continue
        return keywords

    def _extract_from_dict(self, data: Any) -> list[str]:
        extracted = []
        if isinstance(data, dict):
            for k, v in data.items():
                k_lower = k.lower()
                # 1. Если ключ известный — берем его содержимое (обычно это строки с разделителями)
                if self._is_search_key(k_lower):
                    extracted.extend(self._split_keywords(v))
                    # Если ключ совпал, не переходим к агрессивному захвату для этого же значения
                    continue
                
                # 2. Рекурсия для вложенных структур
                if isinstance(v, (dict, list)):
                    extracted.extend(self._extract_from_dict(v))
                
                # 3. Агрессивный захват: если значение - строка и похожа на запрос
                elif isinstance(v, str) and self._is_valid_keyword(v.strip()):
                    # Избегаем захвата чисто технических ID и HEX
                    if not re.match(r'^[a-f0-9_\-\.]{15,}$', v.strip().lower()):
                        extracted.append(self._sanitize_geo(v.strip()))
                        
        elif isinstance(data, list):
            for item in data:
                extracted.extend(self._extract_from_dict(item))
                
        return extracted

    def extract_from_html(self, html: str) -> list[str]:
        keywords = []
        # Извлечение из JWT токенов, зашитых в HTML (скрипты, конфиги)
        keywords.extend(self.extract_from_jwt(html))
        
        # 1. Поиск в data-атрибутах
        data_attr_pattern = r'data-(?:keywords?|terms?|queries|search-terms?|search-queries)\s*=\s*[\"\']([^\"\']+)[\"\']'
        keywords.extend(self._split_keywords(re.findall(data_attr_pattern, html, re.I)))
        
        # 2. Поиск в JSON-подобных структурах по ключам
        for key in self.SEARCH_KEYS:
            patterns = [
                rf'[\"\']{key}[\"\']\s*[:=]\s*[\"\']([^\"\']+)[\"\']',
                rf'[\"\']{key}[\"\']\s*[:=]\s*\[(.*?)\]',
                rf'\b{key}\s*[:=]\s*[\"\']([^\"\']+)[\"\']'
            ]
            for pattern in patterns:
                for match in re.findall(pattern, html, re.I):
                    keywords.extend(self._split_keywords(match))
        
        # 3. Новое: Извлечение из meta keywords
        meta_kw = re.search(r'<meta\s+name=["\']keywords["\']\s+content=["\']([^"\']+)["\']', html, re.I)
        if meta_kw:
            keywords.extend(self._split_keywords(meta_kw.group(1)))
            
        # 4. Новое: Извлечение из заголовка (title)
        title = re.search(r'<title>(.*?)</title>', html, re.I | re.S)
        if title:
            # Титл обычно содержит 1-2 фразы, попробуем их тоже взять если они валидны
            keywords.extend(self._split_keywords(title.group(1).strip()))
            
        return keywords
