import pytest

from src.core.config import config_instance as config
from src.core.models import GraphQLPage
from src.core.scraper import GraphQLClient


class _DummyResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _DummyCookies(dict):
    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)


class _DummyClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.cookies = _DummyCookies()
        self.calls = []

    async def post(self, _url, data=None, headers=None):
        if not self._responses:
            raise AssertionError("No more dummy responses configured")
        self.calls.append({"url": _url, "data": data, "headers": headers})
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_pagination_rate_limit_retries_and_continues(monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("src.core.scraper.asyncio.sleep", _no_sleep)
    monkeypatch.setattr(
        "src.core.scraper.extract_creatives_from_pagination",
        lambda data: data.get("mock_creatives", []),
    )
    monkeypatch.setattr(
        "src.core.scraper.extract_cursor",
        lambda data: data.get("mock_cursor"),
    )

    monkeypatch.setattr(config.data.scraper, "pagination_retries", 3, raising=False)
    monkeypatch.setattr(config.data.scraper, "pagination_rate_limit_retries", 2, raising=False)

    rate_limit_calls = 0

    async def _on_rate_limit():
        nonlocal rate_limit_calls
        rate_limit_calls += 1
        return True

    dummy = _DummyClient(
        [
            _DummyResponse(
                200,
                {"errors": [{"message": "Rate limit exceeded", "code": 1675004}]},
            ),
            _DummyResponse(
                200,
                {"mock_creatives": [[{"ad_archive_id": "42"}]], "mock_cursor": "next_cursor"},
            ),
        ]
    )
    gql = GraphQLClient(
        http_client=dummy,
        endpoint_url="https://example.test/graphql",
        doc_ids={"pagination": "doc"},
        on_pagination_rate_limit=_on_rate_limit,
    )
    gql.initial_variables = {"count": 30}
    gql.payload_template = {"fb_dtsg": "token", "__a": "1"}

    page = await gql._fetch_next_page(GraphQLPage(cursor="start_cursor", doc_id="doc"))

    assert rate_limit_calls == 1
    assert page.cursor == "next_cursor"
    assert len(page.raw_creatives) == 1
    assert dummy.calls[0]["data"]["fb_dtsg"] == "token"
    assert dummy.calls[0]["data"]["__a"] == "1"
