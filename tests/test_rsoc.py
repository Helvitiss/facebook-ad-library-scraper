import pytest
import asyncio
from src.core.rsoc import RSOCExtractor

@pytest.fixture
def extractor():
    return RSOCExtractor()

def test_sanitize_geo(extractor):
    # Гео-санитизация отключена: текст должен возвращаться как есть.
    text = "Best job in London for you"
    sanitized = extractor._sanitize_geo(text)
    assert sanitized == text

    text2 = "Welcome to Germany"
    sanitized2 = extractor._sanitize_geo(text2)
    assert sanitized2 == text2

def test_is_valid_keyword(extractor):
    assert extractor._is_valid_keyword("validKeyword") is True
    assert extractor._is_valid_keyword("no") is False # too short
    assert extractor._is_valid_keyword("https://bad.com") is False
    assert extractor._is_valid_keyword("facebook") is False # blacklist
    assert extractor._is_valid_keyword("123456") is False # digit only

def test_extract_from_url(extractor):
    url = "https://example.com/?utm_term=super+product&rsoc_kw=cheap"
    keywords = extractor.extract_from_url(url)
    assert "super product" in keywords
    assert "cheap" in keywords

def test_extract_from_facebook_ads_library_q_domain(extractor):
    url = (
        "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL"
        "&is_targeted_country=false&media_type=all&q=%22gethappyday.com%22"
        "&search_type=keyword_exact_phrase&sort_data[mode]=total_impressions"
        "&sort_data[direction]=desc&source=page-transparency-widget"
    )

    keywords = extractor.extract_from_url(url)
    assert "gethappyday.com" in keywords
    assert "active" not in keywords
    assert "keyword_exact_phrase" not in keywords
    assert "total_impressions" not in keywords

def test_extract_from_facebook_ads_library_page_id(extractor):
    url = (
        "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL"
        "&is_targeted_country=false&media_type=all&search_type=page"
        "&sort_data[mode]=total_impressions&sort_data[direction]=desc"
        "&source=page-transparency-widget&view_all_page_id=541919755660451"
    )

    keywords = extractor.extract_from_url(url)
    assert "541919755660451" in keywords
    assert "page" not in keywords
    assert "desc" not in keywords
    assert "page-transparency-widget" not in keywords

def test_extract_from_jwt(extractor):
    import base64
    import json
    
    payload = {"utm_term": "jwt_secret_keyword"}
    payload_json = json.dumps(payload).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode().rstrip("=")
    
    fake_jwt = f"header.{payload_b64}.signature"
    # Text containing jwt
    text = f"some garbage {fake_jwt} more garbage"
    
    kws = extractor.extract_from_jwt(text)
    assert "jwt_secret_keyword" in kws

def test_extract_deep(extractor):
    # Тест агрессивного извлечения из неизвестных ключей
    payload = {"technical_key": "unexpected_keyword", "nested": [{"unknown": "deep_keyword"}]}
    kws = extractor._extract_from_dict(payload)
    assert "unexpected_keyword" in kws
    assert "deep_keyword" in kws

def test_extract_raw_base64(extractor):
    # Тест извлечения из сырой Base64 строки (не JWT)
    import base64
    import json
    
    payload = {"q": "base64_hidden_query"}
    payload_json = json.dumps(payload).encode()
    # Длинная строка для прохождения паттерна {24,}
    payload_b64 = base64.b64encode(payload_json).decode() + "padding_to_make_it_long_enough"
    
    text = f"Some data: {payload_b64}"
    kws = extractor.extract_from_jwt(text)
    assert "base64_hidden_query" in kws

def test_extract_from_url_skips_tracking_keys(extractor):
    url = (
        "https://example.com/?search=scholarships+to+study+in+seoul"
        "&cid=ch27052+ch52307&click_id=44071a5d-bd0b-4a56-a314-afef581dc7de"
        "&ad_id=%7BtrackingField1%7D"
    )

    kws = extractor.extract_from_url(url)
    assert "scholarships to study in seoul" in kws
    assert "ch27052 ch52307" not in kws
    assert "44071a5d-bd0b-4a56-a314-afef581dc7de" not in kws
    assert "{trackingField1}" not in kws

def test_extract_from_jwt_ignores_non_search_fields(extractor):
    import base64
    import json

    payload = {
        "city": "Helsinki",
        "dest": "eGMzZDVtLnZmc2t6cWIuY29t",
        "track_id": "1e7b2e80530eb207d69a45583c607e15g1c1f49d43e221b235474d5ab6d72747",
        "terms": "Viajes Para Mayores 65 Años,Ofertas Grandes Viajes 2026",
    }
    payload_json = json.dumps(payload).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode().rstrip("=")
    fake_jwt = f"header.{payload_b64}.signature"

    kws = extractor.extract_from_jwt(fake_jwt)
    assert "Viajes Para Mayores 65 Años" in kws
    assert "Ofertas Grandes Viajes 2026" in kws
    assert "Helsinki" not in kws
    assert "eGMzZDVtLnZmc2t6cWIuY29t" not in kws

def test_process_link_keeps_url_keywords_when_network_fails(monkeypatch, extractor):
    class BrokenOpener:
        def open(self, *args, **kwargs):
            raise OSError("network down")

    import urllib.request
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args, **kwargs: BrokenOpener())

    url = "https://www.holvix.com/dsr?q=staplerfahrer+nachtschicht&asid=a26_ch623"
    kws = asyncio.run(extractor.process_link(url))
    assert "staplerfahrer nachtschicht" in kws

def test_process_link_ignores_redirect_history_noise(extractor):
    class Resp:
        def __init__(self, url, history=None):
            self.url = url
            self.history = history or []
            self.text = ""

        def raise_for_status(self):
            return None

    class Client:
        async def get(self, url, headers=None):
            history = [Resp("https://tracker.example/r?search=bad+noise")]
            return Resp("https://landing.example/?search=good+keyword", history=history)

    kws = asyncio.run(extractor.process_link("https://tracker.example/start", http_client=Client()))
    assert "good keyword" in kws
    assert "bad noise" not in kws
