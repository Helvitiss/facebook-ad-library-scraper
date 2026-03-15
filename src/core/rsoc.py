import asyncio
import json
import httpx
import re
import base64
import unicodedata
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
        "query_terms", "forcekey",
        "p", "tid", "click_id", "cid", "subid", "subid1", "subid2", "ad_id", "campaign_id",
        "tkn", "token", "session", "jwt", "payload", "AB_VERSION_TERMS", "terms_list",
        "rac", "adtext"
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
        """Проверяет, является ли ключ поисковым (с учетом вариаций типа kw1 и forceKeyA)."""
        k_lower = key.lower()
        if k_lower in self.SEARCH_KEYS or "rsoc" in k_lower:
            return True

        # Для большинства ключей разрешаем только числовой суффикс (kw1, term2, q3).
        # Буквенный суффикс поддерживаем только для forceKey* (forceKeyA, forceKeyB...),
        # чтобы не матчить служебные параметры вроде pub (из-за базового ключа "p").
        for sk in self.SEARCH_KEYS:
            if not k_lower.startswith(sk):
                continue

            suffix = k_lower[len(sk):]
            if not suffix:
                continue

            if suffix.isdigit():
                return True

            if sk == "forcekey" and suffix.isalpha():
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
            # Полностью пропускаем HTML только для явных tracker-доменов.
            # Для остальных сайтов (включая noisy-домены) HTML нужен,
            # чтобы доставать полезные ключи из head/script (например query_terms).
            return any(host == d or host.endswith(f".{d}") for d in self.TRACKER_DOMAINS)
        except Exception:
            return False

    def _is_noise_keyword(self, keyword: str) -> bool:
        if not keyword or not isinstance(keyword, str):
            return True

        s = keyword.strip()
        if not s:
            return True
        s_lower = s.lower()

        # Частые служебные/интерфейсные фразы, не являющиеся search intent
        ui_noise = {
            "terms of service", "privacy policy", "skip to content", "cookie policy",
            "accept all", "reject all", "continue shopping", "write a review",
            "shopify-pixel", "trekkie-storefront-renderer", "payload", "main", "product"
        }
        if s_lower in ui_noise:
            return True

        # Технические JS-токены и API/DOM-конструкции
        js_markers = (
            "function(", "document.", "window.", "navigator.", "queryselector(",
            "createelement(", "scrollheight", "return ", "=>"
        )
        if any(marker in s_lower for marker in js_markers):
            return True
        if re.match(r"^\s*(?:var|const|let)\b", s_lower):
            return True

        if s_lower in {"search_term_string", "begin", "look", "learn_more", "learn more"}:
            return True
        if "_googcsa" in s_lower:
            return True

        # CSS-селекторы/цвета/размеры/версии
        if s_lower.startswith('.') or re.fullmatch(r"#[0-9a-f]{3,8}", s_lower):
            return True
        if re.fullmatch(r"-?\d+px", s_lower):
            return True
        if re.fullmatch(r"\d+(?:\.\d+){1,3}", s_lower):
            return True

        # Идентификаторы/артикулы/хэши: буквенно-цифровой шум без пробелов
        if ' ' not in s_lower:
            if re.fullmatch(r"[a-z]{0,3}\d{4,}[a-z0-9-]*", s_lower):
                return True
            if re.fullmatch(r"[a-z0-9_-]{14,}", s_lower):
                return True
            if re.fullmatch(r"[a-z0-9]{10,}", s_lower) and re.search(r"\d", s_lower):
                return True

        # IP-адреса
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", s_lower):
            return True

        # CTA-шаблоны из adtext/rac, обычно не являются целевыми ключами
        cta_markers = [
            "learn more", "read more", "descubre más", "dowiedz się więcej",
            "lesen sie mehr", "obtén información", "get insights on"
        ]
        if any(m in s_lower for m in cta_markers):
            return True

        # Слишком короткие одиночные слова часто шумовые в HTML/JS
        if len(s_lower.split()) == 1 and len(s_lower) < 5:
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
            s = self._normalize_candidate(p)
            if self._is_valid_keyword(s, vetted=vetted):
                cleaned.append(s)
        return cleaned

    def _normalize_candidate(self, text: str) -> str:
        if not isinstance(text, str):
            return ""

        s = text.strip()
        # Удаляем emoji/символьный шум, оставляя буквы/цифры/пунктуацию
        s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("So"))
        s = re.sub(r"\s+", " ", s).strip(" .,!?:;\t\n\r")
        return s


    def _extract_query_context_tokens(self, url: str) -> set[str]:
        """Извлекает опорные токены из q/search-параметров для фильтрации шумовых ключей."""
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
        except Exception:
            return set()

        context_values = []
        for key in ("q", "search", "search_term", "utm_term", "keyword"):
            context_values.extend(params.get(key, []))

        if not context_values:
            return set()

        tokens: set[str] = set()
        for raw in context_values:
            normalized = self._normalize_candidate(self._unquote_fully(raw)).lower()
            for token in re.findall(r"[a-zA-ZÀ-ÿ0-9]+", normalized):
                if len(token) < 3:
                    continue
                if token in self.GENERIC_WORDS or token in self.BLACKLIST:
                    continue
                tokens.add(token)
        return tokens

    def _extract_trusted_terms_from_url(self, url: str) -> set[str]:
        """Извлекает доверенные ключи из query-параметров terms/terms_list."""
        try:
            params = parse_qs(urlparse(url).query)
        except Exception:
            return set()

        trusted: set[str] = set()
        for key in ("terms", "terms_list", "term_list", "query_terms"):
            for raw in params.get(key, []):
                for kw in self._split_keywords(raw, vetted=True):
                    if kw:
                        trusted.add(kw.lower())
        return trusted

    def _extract_trusted_terms_from_html(self, html: Optional[str]) -> set[str]:
        """Извлекает доверенные ключи из JSON-массивов `terms` внутри HTML."""
        if not html:
            return set()

        trusted: set[str] = set()
        # Ищем фрагменты вида "terms": ["...", "..."]
        for match in re.finditer(r"[\"']terms[\"']\s*:\s*(\[[^\]]{1,8000}\])", html, re.I | re.S):
            arr_raw = match.group(1)
            try:
                values = json.loads(arr_raw)
            except Exception:
                continue
            if not isinstance(values, list):
                continue

            for value in values:
                for kw in self._split_keywords(value, vetted=True):
                    if kw:
                        trusted.add(kw.lower())

        return trusted

    def _should_apply_context_filter(self, url: str) -> bool:
        """Контекстная фильтрация включается для adtext/rac-ссылок, где чаще всего есть шум."""
        try:
            params = parse_qs(urlparse(url).query)
            return "rac" in params or "adtext" in params
        except Exception:
            return False

    def _is_context_relevant_keyword(self, keyword: str, context_tokens: set[str]) -> bool:
        if not context_tokens:
            return True

        kw_tokens = {
            t for t in re.findall(r"[a-zA-ZÀ-ÿ0-9]+", keyword.lower())
            if len(t) >= 3
        }
        if not kw_tokens:
            return True

        overlap = kw_tokens & context_tokens
        return len(overlap) >= 2

    def _extract_explicit_terms_from_url(self, url: str) -> list[str]:
        try:
            params = parse_qs(urlparse(url).query)
        except Exception:
            return []

        results: list[str] = []
        for key in ("terms", "terms_list", "term_list", "query_terms"):
            for raw in params.get(key, []):
                results.extend(self._split_keywords(raw, vetted=True))
        return results

    def _extract_forcekey_terms_from_url(self, url: str) -> list[str]:
        try:
            params = parse_qs(urlparse(url).query)
        except Exception:
            return []

        results: list[str] = []
        for key, values in params.items():
            key_lower = key.lower()
            if not key_lower.startswith("forcekey"):
                continue
            suffix = key_lower[len("forcekey"):]
            if not suffix or not suffix.isalpha():
                continue
            for raw in values:
                results.extend(self._split_keywords(raw, vetted=True))
        return results

    def _extract_explicit_terms_from_html(self, html: Optional[str]) -> list[str]:
        if not html:
            return []

        extracted: list[str] = []

        # 1) JSON-массивы: "terms": ["a", "b", ...]
        for match in re.finditer(r"[\"']terms[\"']\s*:\s*(\[[^\]]{1,8000}\])", html, re.I | re.S):
            arr_raw = match.group(1)
            try:
                values = json.loads(arr_raw)
            except Exception:
                continue
            if not isinstance(values, list):
                continue
            for value in values:
                extracted.extend(self._split_keywords(value, vetted=True))

        # 2) JS-объявления: const/let/var terms = "a,b,c"
        for match in re.finditer(r"\b(?:const|let|var)\s+terms\s*=\s*[\"']([^\"']{3,8000})[\"']", html, re.I):
            extracted.extend(self._split_keywords(match.group(1), vetted=True))

        # 3) JS-конфиги вида window.AB_VERSION_TERMS = "a,b,c" или AB_VERSION_TERMS: "..."
        # Поддерживаем любые имена переменных, оканчивающиеся на _TERMS.
        terms_var_patterns = [
            r"\b(?:window\.)?[A-Z0-9_]+_TERMS\s*[:=]\s*[\"']([^\"']{3,8000})[\"']",
            r"[\"'][A-Z0-9_]+_TERMS[\"']\s*:\s*[\"']([^\"']{3,8000})[\"']",
        ]
        for pattern in terms_var_patterns:
            for match in re.finditer(pattern, html):
                extracted.extend(self._split_keywords(match.group(1), vetted=True))

        return extracted

    def _finalize_keywords(self, keywords: list[str]) -> list[str]:
        unique_results = []
        seen = set()
        for kw in keywords:
            if not kw:
                continue
            if self._is_noise_keyword(kw):
                continue
            if kw in ("{country}", "{city}", "{}"):
                continue

            low = kw.lower()
            if low not in seen:
                seen.add(low)
                unique_results.append(kw)
        return unique_results

    async def process_link(self, url: str, http_client: Optional[httpx.AsyncClient] = None) -> list[str]:
        # Всегда сначала извлекаем из исходного URL: даже если сеть/редирект недоступны,
        # это сохраняет ключи из query (например q=...).
        all_keywords = self.extract_from_url(url)
        html_content = None
        final_url = url
        started_from_tracker = self._is_tracker_domain(url)
        
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

            if html_content and not started_from_tracker and not self._skip_html_for_domain(final_url):
                all_keywords.extend(self.extract_from_html(html_content, current_url=final_url))
                
        except Exception as e:
            logger.warning(f"RSOC: Ошибка доступа/обработки ссылки {url} ({type(e).__name__}): {e}")
        
        effective_url = final_url or url

        # Приоритет 1: явные terms-массивы (query/html). Если есть — считаем их окончательными.
        explicit_terms = []
        explicit_terms.extend(self._extract_explicit_terms_from_url(url))
        explicit_terms.extend(self._extract_explicit_terms_from_url(effective_url))
        explicit_terms.extend(self._extract_explicit_terms_from_html(html_content))
        explicit_terms = self._finalize_keywords(explicit_terms)
        if explicit_terms:
            return explicit_terms

        # Приоритет 2: forceKey* параметры. Если есть — не смешиваем с другими источниками.
        forcekey_terms = []
        forcekey_terms.extend(self._extract_forcekey_terms_from_url(url))
        forcekey_terms.extend(self._extract_forcekey_terms_from_url(effective_url))
        forcekey_terms = self._finalize_keywords(forcekey_terms)
        if forcekey_terms:
            return forcekey_terms

        context_tokens = self._extract_query_context_tokens(effective_url)
        apply_context_filter = self._should_apply_context_filter(effective_url) and bool(context_tokens)
        trusted_terms = self._extract_trusted_terms_from_url(effective_url)
        trusted_terms.update(self._extract_trusted_terms_from_url(url))
        trusted_terms.update(self._extract_trusted_terms_from_html(html_content))

        filtered = []
        for kw in all_keywords:
            if not kw:
                continue
            if apply_context_filter and kw.lower() not in trusted_terms and not self._is_context_relevant_keyword(kw, context_tokens):
                continue
            filtered.append(kw)

        return self._finalize_keywords(filtered)

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
        head_keywords = []
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

        # 5. Извлечение только из meta keywords
        meta_kw = soup.find('meta', attrs={'name': 'keywords'})
        if meta_kw and meta_kw.get('content'):
            head_keywords.extend(self._split_keywords(meta_kw['content'], vetted=True))

        # 5.1 Расширенное извлечение из head/meta по полям keywords/query/terms
        # Нужнo для сайтов, где ключи лежат в <meta name="keywords" ...>
        # или в property/itemprop вариациях.
        head = soup.head
        if head:
            for meta in head.find_all('meta'):
                key_name = (
                    meta.get('name')
                    or meta.get('property')
                    or meta.get('itemprop')
                    or ''
                ).strip().lower()
                content = (meta.get('content') or '').strip()
                if not content:
                    continue

                if any(x in key_name for x in ('keyword', 'query_terms', 'query', 'terms')):
                    head_keywords.extend(self._split_keywords(content, vetted=True))

        # Если в head уже есть валидные ключи, используем их как приоритетный источник
        # и не запускаем агрессивный парсинг всего HTML/скриптов (основной источник шума).
        if head_keywords:
            keywords.extend(head_keywords)
        else:
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
                    rf'[\"\']{key}[\"\']\s*[:=]\s*(\d+)',
                ]
                for pattern in patterns:
                    for match in re.findall(pattern, html, re.I):
                        if isinstance(match, str):
                            keywords.extend(self._split_keywords(match, vetted=True))
            
            # 4. Поиск и парсинг всех скриптов как JSON
            for script in soup.find_all('script'):
                content = script.string
                if not content or len(content) < 20: continue
                
                json_matches = re.findall(r'(\{.*?\})', content, re.DOTALL)
                for j_str in json_matches:
                    try:
                        clean_j = re.sub(r'//.*?\n', '', j_str)
                        data = json.loads(clean_j)
                        if isinstance(data, dict):
                            keywords.extend(self._extract_from_dict(data))
                    except:
                        for key in self.SEARCH_KEYS:
                            if key in j_str:
                                pattern = r'["\']?' + re.escape(key) + r'["\']?\s*[:=]\s*["\']?([^"\'\s,\]}]+)["\']?'
                                m = re.search(pattern, j_str, re.I)
                                if m: keywords.extend(self._split_keywords(m.group(1), vetted=True))
            
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
