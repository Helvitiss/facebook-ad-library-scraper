import json

from src.bot.utils import _collect_rsoc_by_link


def test_collect_rsoc_by_link_groups_and_dedups(tmp_path):
    data = [
        {"link_url": "https://a.com", "rsoc_keywords": ["alpha", "beta", "Alpha"]},
        {"link_url": "https://b.com", "rsoc_keywords": ["gamma"]},
        {"link_url": "https://a.com", "rsoc_keywords": ["beta", "delta"]},
        {"link_url": None, "rsoc_keywords": ["orphan"]},
    ]
    p = tmp_path / "data.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    grouped = _collect_rsoc_by_link(tmp_path)

    assert grouped["https://a.com"] == ["alpha", "beta", "delta"]
    assert grouped["https://b.com"] == ["gamma"]
    assert grouped["N/A"] == ["orphan"]
