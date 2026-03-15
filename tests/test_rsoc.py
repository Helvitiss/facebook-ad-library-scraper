import pytest
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
