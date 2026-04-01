import pytest
import json
from src.core.core_utils import (
    recursively_extract_value,
    extract_script_info,
    extract_video_urls,
    extract_image_urls,
    extract_text,
    extract_variables,
)

def test_recursively_extract_value():
    data = {"a": {"b": {"c": "target"}, "d": [{"c": "target2"}]}}
    results = recursively_extract_value(data, "c")
    assert "target" in results
    assert "target2" in results
    assert len(results) == 2

def test_extract_script_info():
    html = """
    <html>
        <script>ignore me</script>
        <script>
            {"collated_results": "found me"}
        </script>
    </html>
    """
    # Note: the real function looks for "collated_results" string inside the script text
    # and then tries to json.loads the WHOLE text. 
    # So the script content must be valid JSON for it to return meaningful data, 
    # OR the function implementation catches generic exceptions.
    
    # Based on implementation: if text and "collated_results" in text: try: return json.loads(text)
    
    html_valid = """
    <script>
    {"some_key": "collated_results", "data": 123}
    </script>
    """
    res = extract_script_info(html_valid)
    assert res == {"some_key": "collated_results", "data": 123}

    html_invalid = "<script>collated_results but not json</script>"
    assert extract_script_info(html_invalid) is None

def test_extract_video_urls():
    # Case 1: Video inside snapshot
    data1 = {
        "snapshot": {
            "videos": [{"video_sd_url": "http://vid1.mp4"}],
            "cards": []
        }
    }
    assert "http://vid1.mp4" in extract_video_urls(data1)

    # Case 2: Video inside cards
    data2 = {
        "snapshot": {
            "cards": [{"video_sd_url": "http://vid2.mp4"}]
        }
    }
    assert "http://vid2.mp4" in extract_video_urls(data2)

def test_extract_image_urls():
    data = {
        "snapshot": {
            "images": [{"resized_image_url": "http://img1.jpg"}],
            "cards": [{"resized_image_url": "http://img2.jpg"}]
        }
    }
    urls = extract_image_urls(data)
    assert "http://img1.jpg" in urls
    assert "http://img2.jpg" in urls

def test_extract_text():
    # Priority: body.text -> cards.body.text
    
    # Case 1: Simple body text
    data1 = {"snapshot": {"body": {"text": "Main text"}}}
    assert extract_text(data1) == "Main text"
    
    # Case 2: Ignore templated text -> Fallback to it if no other option
    data2 = {"snapshot": {"body": {"text": "Hello {{product.name}}!"}}}
    # The function falls back to returning the body text at the end if no better text is found
    # So it should return the text itself, not None
    assert extract_text(data2) == "Hello {{product.name}}!"
    
    # Case 3: Fallback to card text
    data3 = {
        "snapshot": {
            "body": {"text": "{{product.brand}} template"},
            "cards": [{"body": {"text": "Card text"}}]
        }
    }
    assert extract_text(data3) == "Card text"


def test_extract_variables_from_ads_library_url_keeps_search_and_sort():
    url = (
        "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL"
        "&is_targeted_country=false&media_type=all&q=johnnyandcash.com"
        "&search_type=keyword_unordered&sort_data[mode]=total_impressions"
        "&sort_data[direction]=desc"
    )

    vars_ = extract_variables(url)

    assert vars_["adType"] == "ALL"
    assert vars_["activeStatus"] == "ACTIVE"
    assert vars_["countries"] == ["ALL"]
    assert vars_["isTargetedCountry"] is False
    assert vars_["mediaType"] == "all"
    assert vars_["searchType"] == "keyword_unordered"
    assert vars_["queryString"] == "johnnyandcash.com"
    assert vars_["sortData"]["mode"] == "SORT_BY_TOTAL_IMPRESSIONS"
    assert vars_["sortData"]["direction"] == "DESCENDING"
