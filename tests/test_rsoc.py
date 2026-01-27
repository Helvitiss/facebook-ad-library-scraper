import pytest
from src.core.rsoc import RSOCExtractor

@pytest.fixture
def extractor():
    return RSOCExtractor()

def test_sanitize_geo(extractor):
    # Setup some fake cache if needed, or rely on actual pycountry/geonamescache if valid
    # But strictly unit testing might want to mock. 
    # For now let's assume 'London' and 'Germany' are known
    
    text = "Best job in London for you"
    # Note: The implementation replaces city with {city}. 
    # Provided 'London' is in self.cities (likely is)
    sanitized = extractor._sanitize_geo(text)
    assert "{city}" in sanitized
    assert "London" not in sanitized

    text2 = "Welcome to Germany"
    sanitized2 = extractor._sanitize_geo(text2)
    assert "{country}" in sanitized2
    assert "Germany" not in sanitized2

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
