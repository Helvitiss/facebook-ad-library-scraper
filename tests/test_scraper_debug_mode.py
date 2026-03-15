import pytest

from src.core.scraper import Scraper


@pytest.mark.asyncio
async def test_process_creatives_debug_rsoc_only_extracts_link_and_id():
    scraper = Scraper()
    raw = [
        [{"ad_archive_id": "1", "snapshot": {"link_url": "https://example.com/?q=test"}}],
        [{"ad_archive_id": "2", "snapshot": {"link_url": "https://example.com/?q=foo"}}],
        [{"ad_archive_id": "2", "snapshot": {"link_url": "https://example.com/?q=dup"}}],
        [{"ad_archive_id": "3", "snapshot": {}}],
    ]

    groups = await scraper.process_creatives_debug_rsoc_only(raw)

    assert len(groups) == 2
    assert groups[0].link_url == "https://example.com/?q=test"
    assert groups[0].creatives[0].ad_archive_id == "1"
    assert groups[1].link_url == "https://example.com/?q=foo"
    assert groups[1].creatives[0].ad_archive_id == "2"
