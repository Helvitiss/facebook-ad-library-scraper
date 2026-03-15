import asyncio
import json
import httpx
import re
import base64
from pathlib import Path
from typing import Optional, List, Dict, Any, Any
from urllib.parse import urlparse, parse_qs

from loguru import logger

class RSOCExtractor:
    # Ключи с высокой вероятностью содержащие поисковые фразы
    SEARCH_KEYS = {
        "utm_term", "utm_terms", "term", "terms", "keyword", "keywords", "kw", "kws", 
        "q", "qs", "search", "search_term", "search_terms", 
        "searchQuery", "searchQueries", "rsoc", "keywordList", "keyword_list", 
        "kw_list", "kwList", "termList", "term_list", "queryList", "query_list", 
        "suggestions", "suggests", "suggestedTerms", "suggested_terms", 
        "suggestedQueries", "suggested_queries", "related", "relatedTerms", 
        "related_terms", "relatedQueries", "related_queries", "recommendations", 
        "recommendedTerms", "recommended_terms", "recommendedQueries", 
        "recommended_queries", "seedKeywords", "seed_keywords", "seedTerms", "seed_terms",
        "p", "tid", "click_id", "cid", "subid", "subid1", "subid2", "ad_id", "campaign_id",
        "tkn", "token", "session", "jwt", "payload", "AB_VERSION_TERMS", "terms_list"
    }

    TRACKING_QUERY_KEYS = {
        "click_id", "cid", "subid", "subid1", "subid2", "ad_id", "campaign_id", "adset_id",
        "account_id", "fb_pixel_id", "fbclid", "fbcv", "channel", "stid", "sclid", "asid",
        "source", "utm_source", "utm_medium", "pub", "de", "locale", "lang", "m", "layout"
    }

    TRACKER_DOMAINS = {
        "track.topfindtoday.com",
        "topfindtoday.com",
    }

    # Домены, где HTML чаще содержит автогенерируемый JS-шум вместо полезных RSOC.
    NOISY_HTML_DOMAINS = {
        "gethappyday.com",
        "searchrelayr.com",
    }

    # Структурные / контекстные ключи только для рекурсии
    CONTEXT_KEYS = {
        "feed", "feedData", "feed_data", "feedItems", "feed_items", "items", "list", 
        "data", "payload", "content", "results", "result", "entries", "entry", "cards", 
        "cardItems", "blocks", "widgets", "modules", "sections", "config", "pageConfig", 
        "page_config", "appConfig", "app_config", "settings", "options", "params", 
        "state", "initialState", "initial_state", "hydration", "__INITIAL_STATE__", 
        "__NEXT_DATA__", "__NUXT__", "pageData", "searchConfig", "search_config",
        "query", "queries"
    }

    # Технический шум, который всегда игнорируется
    BLACKLIST = {
        "facebook", "windows", "desktop", "macintosh", "chrome", "safari", "mozilla", 
        "webkit", "application", "json", "html", "true", "false", "null", "undefined", 
        "object", "array", "string", "number", "boolean", "http", "https", "www", 
        ".com", ".net", ".org", "ads", "advertising", "marketing", "tracker", 
        "redirect", "android", "iphone", "ipad", "linux", "google", "bing", "yahoo",
        "ad_blocked", "device_type", "platform_name", "track_id", "channel", 
        "lang", "language", "network", "target_url", "dest",
        "hs256", "jwt", "unknown", "none", "unrestrictedsharedarraybuffer", 
        "sharedarraybuffer", "arraybuffer", "dataview", "uint8array", "uint16array", 
        "uint32array", "int8array", "int16array", "int32array", "float32array", 
        "float64array", "biguint64array", "bigint64array", "cryptokey", "cryptokeypair",
        "organicItemsList", "ko_oil", "privacy", "copyright", "terms", "about", "contact",
        "support", "policy", "legal", "cookies", "advertising", "career", "feedback",
        "article", "author", "headline", "url", "datepublished", 
        "datemodified", "inlanguage", "@type", "@context", "@id", "schema.org", "imageobject",
        "webpage", "postaladdress", "organization", "place"
    }

    # Ключи JSON, которые обычно содержат заголовки или тех. описание, а не ключевые слова
    KEY_BLACKLIST = {
        "headline", "description", "title", "caption", "text", "body", "content", 
        "author", "publisher", "image", "logo", "url", "link", "href", "date", 
        "published", "modified", "type", "context", "id", "schema", "version",
        "og", "twitter", "meta", "viewport", "charset", "color", "theme",
        "article", "post", "comment", "user", "name", "id", "@type", "@context",
        "inLanguage", "mainEntityOfPage", "potentialAction", "articleBody",
        "description", "headline", "name", "url", "mainEntityOfPage"
    }

    # Слова, которые слишком общие для одиночного захвата (разрешены только в составе фраз)
    GENERIC_WORDS = {
        "dental", "implants", "medical", "health", "care", "service", "price", "cost",
        "learn", "more", "about", "click", "here", "read", "view"
    }

    SPLIT_PATTERN = r'[,|;\n]|\s*\|\|\s*|\s*::\s*'

    def __init__(self, proxy: Optional[httpx.Proxy] = None):
        self.proxy = proxy
        self.client_kwargs = {
            "timeout": 35.0,
            "follow_redirects": True,
            "proxy": self.proxy,
            "headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
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

    def _is_tracker_domain(self, url: str) -> bool:
        try:
            host = urlparse(url).netloc.lower()
            return any(host == d or host.endswith(f".{d}") for d in self.TRACKER_DOMAINS)
        except Exception:
            return False

    def _skip_html_for_domain(self, url: str) -> bool:
        try:
            host = urlparse(url).netloc.lower()
            return (
                any(host == d or host.endswith(f".{d}") for d in self.TRACKER_DOMAINS)
                or any(host == d or host.endswith(f".{d}") for d in self.NOISY_HTML_DOMAINS)
            )
        except Exception:
            return False

    def _is_noise_keyword(self, keyword: str) -> bool:
        if not keyword or not isinstance(keyword, str):
            return True

        s = keyword.strip()
        if not s:
            return True
        s_lower = s.lower()

        # Технические JS-токены
        if s_lower in {"search_term_string", "begin", "look"}:
            return True
        if s_lower.startswith("function("):
            return True
        if "_googcsa" in s_lower or s_lower.startswith("window."):
            return True

        # CSS/размеры/пиксели
        if re.fullmatch(r"-?\d+px", s_lower):
            return True

        # IP-адреса
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", s_lower):
            return True

        return False



    def _is_valid_keyword(self, s: str, min_len: int = 4, vetted: bool = False) -> bool:
        if not s: return False
        s_lower = s.lower()
        
        # Фильтрация по черному списку слов
        words = re.findall(r'\b\w+\b', s_lower)
        
        # Одиночные общие слова - бан
        if len(words) == 1 and s_lower in self.GENERIC_WORDS:
            return False

        for black_word in self.BLACKLIST:
            if black_word.startswith("@") or black_word == "schema.org":
                if black_word in s_lower: return False
            elif black_word in words: 
                # Если фраза состоит ТОЛЬКО из слова в черном списке - бан
                if len(words) == 1: return False
                # Если слово в списке - очень общее/техническое, и оно есть в фразе - продолжаем проверку
        
        if any(x in s_lower for x in ["://", "www.", ".com", ".net", ".org", ".php", ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".svg", ".json"]): return False
        if s.startswith('/') or s.startswith('./') or s.startswith('../'): return False
        
        if len(s) > 100: return False
        if s.isdigit() or re.match(r'^[a-f0-9]{20,}$', s_lower): return False
        if re.search(r'[pbs]_\d{3,}', s_lower): return False
        
        # Разрешаем {} для плейсхолдеров типа {City}
        if re.search(r'0x[0-9a-f]+', s_lower): return False
        if re.search(r'[=+\*<>;]', s_lower): return False
        if s.startswith('_') or s.startswith('$'): return False
        
        # Строгая фильтрация JS-мусора
        if "[" in s_lower or "]" in s_lower: return False
        if re.search(r'[a-zA-Z]\(.*\)', s_lower): return False
        if s.count('{') > 1 or s.count('}') > 1: return False
        if ":" in s and " " not in s: return False
        
        if not vetted:
            if len(s) < 4:
                # logger.debug(f"RSOC: Filtered (not vetted, too short): {s}")
                return False
        else:
            if len(s) < min_len:
                # logger.debug(f"RSOC: Filtered (vetted, too short): {s}")
                return False
            
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

    def _sanitize_geo(self, text: str) -> str:
        """Гео-санитизация отключена: возвращаем исходный текст без замен."""
        return text

    def _split_keywords(self, text: Any, vetted: bool = False) -> list[str]:
        if not text: return []
        if not isinstance(text, str):
            if isinstance(text, (list, tuple)):
                results = []
                for item in text:
                    results.extend(self._split_keywords(item, vetted=vetted))
                return results
            return []
        
        # Полное декодирование перед обработкой
        text = self._unquote_fully(text)
        
        parts = re.split(self.SPLIT_PATTERN, text)
        cleaned = []
        for p in parts:
            s = p.strip()
            if self._is_valid_keyword(s, vetted=vetted):
                cleaned.append(s)
        return cleaned

    async def process_link(self, url: str, http_client: Optional[httpx.AsyncClient] = None) -> list[str]:
        # Всегда сначала извлекаем из исходного URL: даже если сеть/редирект недоступны,
        # это сохраняет ключи из query (например q=...).
        all_keywords = self.extract_from_url(url)
        html_content = None
        final_url = url
        
        try:
            used_urllib = False
            if http_client:
                try:
                    response = await http_client.get(url, headers=self.client_kwargs["headers"])
                    response.raise_for_status()
                    final_url = str(response.url)
                    all_keywords.extend(self.extract_from_url(final_url))
                    html_content = response.text
                except Exception as e:
                    logger.debug(f"RSOC: Внешний клиент httpx не смог обработать {url} ({e}), пробуем urllib...")
                    used_urllib = True
            else:
                used_urllib = True

            if used_urllib:
                # ВАЖНО: Используем urllib, так как он обходит 406 ошибку на многих сайтах
                import urllib.request
                import urllib.parse
                
                logger.debug(f"RSOC: Запрос к {url} через urllib (proxy: {self.proxy})")
                
                proxy_handler = urllib.request.ProxyHandler({'http': self.proxy, 'https': self.proxy}) if self.proxy else urllib.request.ProxyHandler({})
                opener = urllib.request.build_opener(proxy_handler)
                
                # Для urllib используем только безопасные заголовки, без gzip (иначе нужно декодировать вручную)
                safe_headers = {
                    "User-Agent": self.client_kwargs["headers"]["User-Agent"],
                    "Accept": self.client_kwargs["headers"]["Accept"],
                    "Accept-Language": self.client_kwargs["headers"]["Accept-Language"],
                }
                req = urllib.request.Request(url, headers=safe_headers)
                
                def _fetch():
                    with opener.open(req, timeout=25) as response:
                        return response.geturl(), response.read().decode('utf-8', errors='ignore')

                final_url, html_content = await asyncio.to_thread(_fetch)
                
                # Извлекаем данные из URL (если еще не извлекли или они изменились)
                all_keywords.extend(self.extract_from_url(url))
                all_keywords.extend(self.extract_from_url(final_url))

            if html_content and not self._skip_html_for_domain(final_url):
                all_keywords.extend(self.extract_from_html(html_content, current_url=final_url))
                
        except Exception as e:
            logger.warning(f"RSOC: Ошибка доступа/обработки ссылки {url} ({type(e).__name__}): {e}")
        
        unique_results = []
        seen = set()
        for kw in all_keywords:
            if not kw: continue
            if self._is_noise_keyword(kw): continue
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
            host = parsed.netloc.lower()
            is_fb_ads_library = "facebook.com" in host and "/ads/library" in parsed.path.lower()
            
            # 1. Извлечение из параметров запроса
            params = parse_qs(parsed.query)
            for key, values in params.items():
                key_lower = key.lower()
                is_vetted = self._is_search_key(key)

                # Для Facebook Ads Library игнорируем служебные параметры сортировки/фильтрации.
                # Оставляем только реальные сущности поиска: q и view_all_page_id.
                if is_fb_ads_library and key_lower not in {"q", "view_all_page_id"}:
                    continue

                if key_lower in self.TRACKING_QUERY_KEYS:
                    continue

                for val in values:
                    # Facebook Ads Library часто передает поисковый запрос в q в кавычках
                    # (например q="gethappyday.com"). Такие доменные запросы не проходят
                    # общую фильтрацию _is_valid_keyword, но это и есть целевой ключ.
                    if is_fb_ads_library and key_lower == "q":
                        normalized = self._unquote_fully(val).strip().strip('"\'')
                        if normalized:
                            keywords.append(normalized)

                    # Для режима поиска по странице Facebook Ads Library ключом выступает
                    # view_all_page_id. Его значение числовое, поэтому оно не проходит
                    # общую фильтрацию и добавляется отдельно.
                    if is_fb_ads_library and key_lower == "view_all_page_id":
                        page_id = self._unquote_fully(val).strip()
                        if page_id:
                            keywords.append(page_id)

                    # Для обычных URL извлекаем только из целевых ключей.
                    if is_vetted:
                        keywords.extend(self._split_keywords(val, vetted=True))
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
                    keywords.extend(self._extract_from_dict(payload_json, aggressive_strings=False))
            except: continue
        return keywords

    def _extract_from_dict(self, data: Any, aggressive_strings: bool = True) -> list[str]:
        extracted = []
        if isinstance(data, dict):
            for k, v in data.items():
                k_lower = k.lower()
                
                # Пропускаем нецелевые поля типа headline, description
                if k_lower in self.KEY_BLACKLIST:
                    continue
                    
                is_vetted = self._is_search_key(k_lower)
                
                # 1. Если ключ известный — берем его содержимое (обычно это строки с разделителями)
                if is_vetted:
                    extracted.extend(self._split_keywords(v, vetted=True))
                    # Если ключ совпал, не переходим к агрессивному захвату для этого же значения
                    continue
                
                # 2. Рекурсия для вложенных структур
                if isinstance(v, (dict, list)):
                    extracted.extend(self._extract_from_dict(v, aggressive_strings=aggressive_strings))
                
                # 3. Агрессивный захват (vetted=False по умолчанию)
                elif aggressive_strings and isinstance(v, str):
                    s_clean = v.strip()
                    if self._is_valid_keyword(s_clean, vetted=False):
                        if not re.match(r'^[a-f0-9_\-\.]{15,}$', s_clean.lower()):
                            extracted.append(s_clean)
                        
        elif isinstance(data, list):
            for item in data:
                extracted.extend(self._extract_from_dict(item, aggressive_strings=aggressive_strings))
                
        return extracted

    def extract_from_html(self, html: str, current_url: Optional[str] = None) -> list[str]:
        keywords = []
        if not html: return []
        
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, 'html.parser')
        except Exception as e:
            logger.debug(f"RSOC: Ошибка инициализации BeautifulSoup: {e}")
            return []

        # 0. Извлечение заголовков для фильтрации
        page_title = soup.title.string.strip().lower() if soup.title and soup.title.string else ""
        h1_tag = soup.find('h1')
        h1_text = h1_tag.get_text(strip=True).lower() if h1_tag else ""
        
        # Определение базового домена для фильтрации "источников" (внешних ссылок)
        base_domain = ""
        if current_url:
            try:
                base_domain = urlparse(current_url).netloc
            except: pass

        # 1. Извлечение из JWT токенов (скрипты, конфиги)
        keywords.extend(self.extract_from_jwt(html))
        
        # 2. Поиск в data-атрибутах
        data_attr_pattern = r'data-(?:keywords?|terms?|queries|search-terms?|search-queries)\s*=\s*[\"\']([^\"\']+)[\"\']'
        keywords.extend(self._split_keywords(re.findall(data_attr_pattern, html, re.I)))
        
        # 3. Поиск в JSON-подобных структурах по ключам во всем HTML (агрессивно)
        for key in self.SEARCH_KEYS:
            patterns = [
                rf'[\"\']{key}[\"\']\s*[:=]\s*[\"\']([^\"\']+)[\"\']', 
                rf'[\"\']{key}[\"\']\s*[:=]\s*\[(.*?)\]',           
                rf'\b{key}\s*[:=]\s*[\"\']([^\"\']+)[\"\']',        
                rf'\b{key}\s*[:=]\s*\[(.*?)\]',
                rf'[\"\']{key}[\"\']\s*[:=]\s*(\d+)', # Для числовых ID, которые могут быть полезны
            ]
            for pattern in patterns:
                for match in re.findall(pattern, html, re.I):
                    if isinstance(match, str):
                        keywords.extend(self._split_keywords(match, vetted=True))
        
        # 4. Поиск и парсинг всех скриптов как JSON
        for script in soup.find_all('script'):
            content = script.string
            if not content or len(content) < 20: continue
            
            # Ищем что-то похожее на JSON внутри скрипта
            json_matches = re.findall(r'(\{.*?\})', content, re.DOTALL)
            for j_str in json_matches:
                try:
                    # Пытаемся почистить строку для json.loads (убираем JS комментарии и т.д. - упрощенно)
                    clean_j = re.sub(r'//.*?\n', '', j_str)
                    data = json.loads(clean_j)
                    if isinstance(data, dict):
                        keywords.extend(self._extract_from_dict(data))
                except:
                    # Если не JSON, пробуем искать ключи внутри строки этого блока
                    for key in self.SEARCH_KEYS:
                        if key in j_str:
                            # Используем обычную строку для regex, чтобы не путаться с f-string скобками
                            pattern = r'["\']?' + re.escape(key) + r'["\']?\s*[:=]\s*["\']?([^"\'\s,\]}]+)["\']?'
                            m = re.search(pattern, j_str, re.I)
                            if m: keywords.extend(self._split_keywords(m.group(1), vetted=True))
        
        # 5. Извлечение только из meta keywords
        meta_kw = soup.find('meta', attrs={'name': 'keywords'})
        if meta_kw and meta_kw.get('content'):
            keywords.extend(self._split_keywords(meta_kw['content'], vetted=True))
            
        # 6. Поиск в произвольных атрибутах 'keywords' любого тега
        for tag in soup.find_all(True, attrs={"keywords": True}):
            keywords.extend(self._split_keywords(tag['keywords'], vetted=True))
            
        # 7. Интеллектуальное извлечение ссылок (самое важное)
        for a in soup.find_all('a'):
            href = a.get('href', '')
            text = a.get_text(strip=True)
            if not text or len(text) < 5: continue
            
            # Фильтр 1: Внешние ссылки (источники) - ИГНОРИРУЕМ
            if base_domain:
                try:
                    parsed_href = urlparse(href)
                    if parsed_href.netloc and parsed_href.netloc != base_domain:
                        continue
                except: pass
            elif href.startswith('http'): # Если нет базового домена, но ссылка абсолютная -> вероятно внешняя
                continue

            # Фильтр 2: Контекст (Sources, Resources и т.д.)
            is_source_block = False
            for parent in a.parents:
                # Если в родителе есть заголовок со словами Source/Resource -> это не ключи
                header = parent.find(['h1','h2','h3','h4','h5','h6'])
                if header:
                    h_text = header.get_text().lower()
                    if any(x in h_text for x in ['source', 'resource', 'reference', 'additional', 'about', 'contact']):
                        is_source_block = True
                        break
                # Если сам контейнер имеет подозрительный класс/id
                container_id = (parent.get('id') or '').lower()
                container_class = " ".join(parent.get('class') or []).lower()
                if any(x in container_id or x in container_class for x in ['footer', 'nav', 'menu', 'sidebar', 'copyright']):
                    is_source_block = True
                    break
            
            if not is_source_block:
                # Из ссылок берем только если это длинные фразы (от 3-х слов или > 15 симв)
                if len(text) > 15 or text.count(" ") >= 2:
                    keywords.extend(self._split_keywords(text))

        # Финальная фильтрация: убираем совпадения с заголовком/H1 и частичные дубли
        filtered = []
        for kw in keywords:
            kw_low = kw.lower()
            # Убираем, если это в точности заголовок
            if page_title and kw_low == page_title: continue
            if h1_text and kw_low == h1_text: continue
            
            # Убираем "Learn more about..." если оно пролезло через заголовки
            if "learn more" in kw_low or "click here" in kw_low: continue
            
            filtered.append(kw)
            
        return filtered
