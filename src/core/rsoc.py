import asyncio
import json
import httpx
import re
import base64
import unicodedata
from typing import Optional, List, Any
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

    # Ключи, в которых чаще всего лежат JWT/Base64 payload'ы
    TOKEN_QUERY_KEYS = {
        "tkn", "token", "jwt", "session", "payload", "auth", "bearer"
    }

    # Ключи, которые обычно содержат реальный список терминов внутри HTML/скриптов
    HTML_TERM_KEYS = {
        "terms", "query_terms", "keywords", "keyword", "search_terms", "search_term",
        "kw_list", "keyword_list", "terms_list", "term_list"
    }
    HTML_TERM_KEYS_PATTERN = "|".join(sorted(HTML_TERM_KEYS, key=len, reverse=True))

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
        "learn", "more", "about", "click", "here", "read", "view",
        "search", "costs", "pricing", "info", "guide"
    }

    SPLIT_PATTERN = r'[,|;\n]|\s*\|\|\s*::\s*'

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

    def _is_noisy_html_domain(self, url: str) -> bool:
        try:
            host = urlparse(url).netloc.lower()
            return any(host == d or host.endswith(f".{d}") for d in self.NOISY_HTML_DOMAINS)
        except Exception:
            return False

    def _is_noise_keyword(self, keyword: str) -> bool:
        if not keyword or not isinstance(keyword, str):
            return True

        s = keyword.strip()
        if not s:
            return True
        s_lower = s.lower()
        words = s.split()
        word_count = len(words)

        if word_count > 12:
            return True
        if len(s) > 90:
            return True

        if any(ch in s for ch in ("\n", "\r", "\t")):
            return True

        if any(dash in s for dash in ("—", "–")):
            return True

        if re.search(r"[.!?…]", s):
            if word_count > 3 or len(s) > 30:
                return True

        if re.search(r"[:;]", s):
            if word_count > 3:
                return True

        if re.search(r"\b\w\b$", s):
            return True

        if word_count == 1 and re.search(r"[a-zA-Z]", s) and re.search(r"\d", s):
            return True

        if s_lower.startswith(("and ", "or ", "with ", "without ", "including ", "plus ", "as well as ", "along with ")):
            return True

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
        if word_count == 1 and len(s_lower) < 5:
            return True

        return False

    def _looks_like_keyword_list(self, text: str) -> bool:
        if not text or not isinstance(text, str):
            return False
        s = self._unquote_fully(text).strip()
        if len(s) < 6:
            return False
        # Типичные разделители списков
        if s.count(",") >= 2:
            if re.search(r"[.!?…]", s):
                return False
            return True
        if any(x in s for x in ("||", "|", ";")):
            if re.search(r"[.!?…]", s):
                return False
            return True
        return False

    def _extract_terms_from_array_like(self, raw: str) -> list[str]:
        if not raw or not isinstance(raw, str):
            return []

        extracted: list[str] = []
        try:
            values = json.loads(raw)
            if isinstance(values, list):
                for value in values:
                    extracted.extend(self._split_keywords(value, vetted=True))
                return extracted
        except Exception:
            pass

        # Fallback: достаем строки из массива с одинарными/двойными кавычками
        for value in re.findall(r"[\"']([^\"']{2,200})[\"']", raw):
            extracted.extend(self._split_keywords(value, vetted=True))
        return extracted

    def _extract_terms_from_script(self, content: str) -> list[str]:
        if not content or not isinstance(content, str):
            return []
        if len(content) < 20:
            return []
        if len(content) > 800_000:
            return []

        extracted: list[str] = []

        # 1) JSON- и JS-объекты: "terms": ["a", "b"] / 'keywords': [...]
        for match in re.finditer(
            rf"[\"'](?P<key>{self.HTML_TERM_KEYS_PATTERN})[\"']\s*:\s*(\[[^\]]{{1,8000}}\])",
            content,
            re.I | re.S
        ):
            extracted.extend(self._extract_terms_from_array_like(match.group(2)))

        # 2) Строковые значения внутри объектов: "keywords": "a,b,c"
        for match in re.finditer(
            rf"[\"'](?P<key>terms|query_terms|keywords|keyword|search_terms|search_term)[\"']\s*:\s*[\"']([^\"']{{3,8000}})[\"']",
            content,
            re.I
        ):
            extracted.extend(self._split_keywords(match.group(2), vetted=True))

        # 3) JS-объявления: const/let/var terms = "a,b,c"
        for match in re.finditer(
            r"\b(?:const|let|var)\s+(terms|keywords|keyword|query_terms|search_terms|search_term)\s*=\s*[\"']([^\"']{3,8000})[\"']",
            content,
            re.I
        ):
            extracted.extend(self._split_keywords(match.group(2), vetted=True))

        # 4) *_TERMS переменные (window.AB_VERSION_TERMS и т.п.)
        for match in re.finditer(
            r"\b(?:window\.)?[A-Z0-9_]+_TERMS\s*[:=]\s*[\"']([^\"']{3,8000})[\"']",
            content
        ):
            extracted.extend(self._split_keywords(match.group(1), vetted=True))

        return extracted


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
        # Ищем фрагменты вида "terms/query_terms/keywords": ["...", "..."]
        for match in re.finditer(
            rf"[\"'](terms|query_terms|keywords|keyword)[\"']\s*:\s*(\[[^\]]{{1,8000}}\])",
            html,
            re.I | re.S
        ):
            arr_raw = match.group(2)
            for kw in self._extract_terms_from_array_like(arr_raw):
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

    def _extract_primary_query_terms_from_url(self, url: str) -> list[str]:
        try:
            params = parse_qs(urlparse(url).query)
        except Exception:
            return []

        results: list[str] = []
        for key in ("q", "search", "search_term", "search_terms"):
            for raw in params.get(key, []):
                if not raw:
                    continue
                results.extend(self._split_keywords(raw, vetted=True))
        return results

    def _extract_explicit_terms_from_html(self, html: Optional[str]) -> list[str]:
        if not html:
            return []

        extracted: list[str] = []

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, 'html.parser')
        except Exception as e:
            logger.debug(f"RSOC: Ошибка инициализации BeautifulSoup: {e}")
            return []

        # 1) Meta keywords / query_terms / terms
        for meta in soup.find_all('meta'):
            key_name = (
                meta.get('name')
                or meta.get('property')
                or meta.get('itemprop')
                or ''
            ).strip().lower()
            content = (meta.get('content') or '').strip()
            if not content:
                continue

            if "keyword" in key_name or "query_terms" in key_name or key_name == "terms":
                extracted.extend(self._split_keywords(content, vetted=True))
                continue

            # meta description иногда содержит список ключей
            if key_name in {"description", "og:description", "twitter:description"}:
                if self._looks_like_keyword_list(content):
                    extracted.extend(self._split_keywords(content, vetted=True))

        # 2) Скрипты: terms/keywords/query_terms + *_TERMS
        for script in soup.find_all('script'):
            content = script.string or script.get_text()
            extracted.extend(self._extract_terms_from_script(content))

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

    def _finalize_forcekey_keywords(self, keywords: list[str]) -> list[str]:
        """Finalize forceKey terms with minimal filtering.

        forceKey* parameters are explicit intent signals from the source URL and
        may legitimately end with single-letter tokens (e.g. "Exact Term A").
        """
        unique_results: list[str] = []
        seen: set[str] = set()
        for kw in keywords:
            normalized = self._normalize_candidate(kw)
            if not normalized:
                continue
            low = normalized.lower()
            if low in seen:
                continue
            seen.add(low)
            unique_results.append(normalized)
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
        allow_html_sources = (not started_from_tracker) and (not self._skip_html_for_domain(effective_url))

        # Приоритет 1: forceKey* параметры. Если есть — не смешиваем с другими источниками.
        forcekey_terms = []
        forcekey_terms.extend(self._extract_forcekey_terms_from_url(url))
        forcekey_terms.extend(self._extract_forcekey_terms_from_url(effective_url))
        forcekey_terms = self._finalize_forcekey_keywords(forcekey_terms)
        if forcekey_terms:
            return forcekey_terms

        # Приоритет 2: явные terms-массивы (query/html). Если есть — считаем их окончательными.
        explicit_terms = []
        explicit_terms.extend(self._extract_explicit_terms_from_url(url))
        explicit_terms.extend(self._extract_explicit_terms_from_url(effective_url))
        if allow_html_sources:
            explicit_terms.extend(self._extract_explicit_terms_from_html(html_content))

            # Для noisy-доменов добавляем базовый query intent (q/search), чтобы не терять
            # исходный пользовательский запрос при наличии query_terms в HTML.
            if self._is_noisy_html_domain(effective_url):
                explicit_terms.extend(self._extract_primary_query_terms_from_url(url))
                explicit_terms.extend(self._extract_primary_query_terms_from_url(effective_url))

        explicit_terms = self._finalize_keywords(explicit_terms)
        if explicit_terms:
            return explicit_terms

        context_tokens = self._extract_query_context_tokens(effective_url)
        apply_context_filter = self._should_apply_context_filter(effective_url) and bool(context_tokens)
        trusted_terms = self._extract_trusted_terms_from_url(effective_url)
        trusted_terms.update(self._extract_trusted_terms_from_url(url))
        if allow_html_sources:
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
                is_token_key = key_lower in self.TOKEN_QUERY_KEYS or any(x in key_lower for x in ("token", "jwt", "tkn", "payload"))

                # Для Facebook Ads Library игнорируем служебные параметры сортировки/фильтрации.
                # Оставляем только реальные сущности поиска: q и view_all_page_id.
                if is_fb_ads_library and key_lower not in {"q", "view_all_page_id"}:
                    continue

                if key_lower in self.TRACKING_QUERY_KEYS:
                    continue

                for val in values:
                    # Токены (JWT/Base64) не разбиваем на "слова"
                    if is_token_key:
                        keywords.extend(self.extract_from_jwt(val))
                        continue

                    # adtext/rac почти всегда шум — используем только как контекст (не как ключи)
                    if key_lower in {"rac", "adtext"}:
                        continue

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

        # 1) Meta keywords/terms/description (если это список)
        for meta in soup.find_all('meta'):
            key_name = (
                meta.get('name')
                or meta.get('property')
                or meta.get('itemprop')
                or ''
            ).strip().lower()
            content = (meta.get('content') or '').strip()
            if not content:
                continue

            if "keyword" in key_name or "query_terms" in key_name or key_name == "terms":
                keywords.extend(self._split_keywords(content, vetted=True))
                continue
            if key_name in {"description", "og:description", "twitter:description"}:
                if self._looks_like_keyword_list(content):
                    keywords.extend(self._split_keywords(content, vetted=True))

        # 2) Скрипты: terms/keywords/query_terms + *_TERMS
        for script in soup.find_all('script'):
            content = script.string or script.get_text()
            keywords.extend(self._extract_terms_from_script(content))

        # 3) data-атрибуты с явными ключевыми словами
        data_attr_pattern = r'data-(?:keywords?|terms?|queries|search-terms?|search-queries)\s*=\s*[\"\']([^\"\']+)[\"\']'
        keywords.extend(self._split_keywords(re.findall(data_attr_pattern, html, re.I)))

        return keywords
