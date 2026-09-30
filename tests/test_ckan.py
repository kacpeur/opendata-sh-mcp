"""Tests for the CKAN client and fq construction (HTTP mocked with respx)."""

from __future__ import annotations

import httpx
import pytest

from opendata_sh_mcp import ckan
from opendata_sh_mcp.ckan import CkanClient, PortalError
from opendata_sh_mcp.server import build_fq

from .conftest import ANUBIS_HTML, API, BASE, KIEL_PACKAGE, NOT_FOUND_BODY, ok


@pytest.fixture
async def cc():
    c = CkanClient(base_url=BASE + "/", user_agent=ckan.DEFAULT_USER_AGENT, timeout=5, cache_ttl=300)
    yield c
    await c.aclose()


class TestBuildFq:
    def test_empty(self):
        assert build_fq() is None
        assert build_fq(groups=[], tags=[]) is None

    def test_all_clauses(self):
        fq = build_fq(
            organization="landeshauptstadt-kiel",
            groups=["soci", "tran"],
            tags=["Bevölkerung", "Einwohner"],
            res_format="csv",
            license_id="http://dcat-ap.de/def/licenses/dl-by-de/2.0",
            modified_since="2024-01-31",
        )
        assert fq == (
            'organization:"landeshauptstadt-kiel" AND groups:"soci" AND groups:"tran" AND '
            'tags:"Bevölkerung" AND tags:"Einwohner" AND res_format:"CSV" AND '
            'license_id:"http://dcat-ap.de/def/licenses/dl-by-de/2.0" AND metadata_modified:[2024-01-31T00:00:00Z TO *]'
        )

    def test_quoting_escapes_quotes_and_backslashes(self):
        assert build_fq(tags=['Kieler "Woche"']) == r'tags:"Kieler \"Woche\""'
        assert build_fq(tags=["a\\b"]) == r'tags:"a\\b"'
        # Solr operators inside the value stay inside the quoted phrase
        assert build_fq(organization="x OR *:*") == 'organization:"x OR *:*"'

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("2024-01-31", "2024-01-31T00:00:00Z"),
            ("2024-01-31T12:30:00Z", "2024-01-31T12:30:00Z"),
            ("2024-01-31T12:30:00", "2024-01-31T12:30:00Z"),
            ("2024-01-31T12:30:00.123Z", "2024-01-31T12:30:00.123Z"),
        ],
    )
    def test_modified_since_forms(self, given, expected):
        assert build_fq(modified_since=given) == f"metadata_modified:[{expected} TO *]"

    @pytest.mark.parametrize(
        "bad",
        ["31.01.2024", "2024/01/31", "gestern", "2024-1-31", "2024-01-31 12:00", "2024-01-31T12:00:00+01:00", "2024-01-31] OR [*"],
    )
    def test_modified_since_invalid(self, bad):
        with pytest.raises(PortalError, match="modified_since must be an ISO date"):
            build_fq(modified_since=bad)


class TestAction:
    async def test_success_and_params(self, cc, portal):
        route = portal.get(API + "package_show").respond(json=ok(KIEL_PACKAGE))
        res = await cc.action("package_show", id="kiel-bevoelkerung-stadtteile", ignored=None)
        assert res["name"] == "kiel-bevoelkerung-stadtteile"
        req = route.calls.last.request
        assert str(req.url) == f"{BASE}/api/3/action/package_show?id=kiel-bevoelkerung-stadtteile"
        assert req.headers["Accept"] == "application/json"

    def test_trailing_slash_stripped(self, cc):
        assert cc.base_url == BASE
        assert cc.dataset_url("abc") == f"{BASE}/dataset/abc"

    async def test_user_agent_not_browser_like(self, cc, portal):
        route = portal.get(API + "status_show").respond(json=ok({"ckan_version": "2.10.4"}))
        await cc.action("status_show")
        ua = route.calls.last.request.headers["User-Agent"]
        assert ua == ckan.DEFAULT_USER_AGENT
        for bad in ("Mozilla", "Claude", "ChatGPT"):
            assert bad not in ua
        assert ua.startswith("opendata-sh-mcp/")

    async def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("OPENDATA_SH_BASE_URL", "https://example.test/")
        monkeypatch.setenv("OPENDATA_SH_USER_AGENT", "custom-agent/1")
        monkeypatch.setenv("OPENDATA_SH_TIMEOUT", "7")
        monkeypatch.setenv("OPENDATA_SH_CACHE_TTL", "0")
        c = CkanClient()
        assert (c.base_url, c.user_agent, c.timeout, c.cache_ttl) == ("https://example.test", "custom-agent/1", 7.0, 0.0)

    async def test_ckan_404_error_body(self, cc, portal, sleeps):
        route = portal.get(API + "package_show").respond(404, json=NOT_FOUND_BODY)
        with pytest.raises(PortalError, match=r"^package_show: Not found$"):
            await cc.action("package_show", id="gibt-es-nicht")
        assert route.call_count == 1  # 404 is not retried
        assert sleeps == []

    async def test_error_type_only(self, cc, portal):
        portal.get(API + "package_search").respond(
            409, json={"success": False, "error": {"__type": "Search Query Error"}}
        )
        with pytest.raises(PortalError, match="package_search: Search Query Error"):
            await cc.action("package_search", q="(")

    async def test_error_missing(self, cc, portal):
        portal.get(API + "package_search").respond(json={"success": False})
        with pytest.raises(PortalError, match=r"package_search: \{\}"):
            await cc.action("package_search")

    async def test_anubis_challenge_page(self, cc, portal):
        portal.get(API + "package_search").respond(200, html=ANUBIS_HTML)
        with pytest.raises(PortalError) as ei:
            await cc.action("package_search", q="Kiel")
        msg = str(ei.value)
        assert "did not return JSON (HTTP 200)" in msg
        assert "bot-protection" in msg and "OPENDATA_SH_USER_AGENT" in msg
        assert "\n" not in msg

    async def test_plain_html_error_has_no_anubis_hint(self, cc, portal):
        portal.get(API + "package_search").respond(500, text="<h1>Internal Server Error</h1>")
        with pytest.raises(PortalError) as ei:
            await cc.action("package_search")
        assert "HTTP 500" in str(ei.value)
        assert "bot-protection" not in str(ei.value)

    async def test_retry_503_then_200(self, cc, portal, sleeps):
        route = portal.get(API + "package_list")
        route.side_effect = [httpx.Response(503, text="Service Unavailable"), httpx.Response(200, json=ok(["a", "b"]))]
        assert await cc.action("package_list") == ["a", "b"]
        assert route.call_count == 2
        assert sleeps == [0.5]

    async def test_retry_backoff_gives_up_after_four(self, cc, portal, sleeps):
        route = portal.get(API + "package_list").mock(
            side_effect=[httpx.Response(429), httpx.Response(502), httpx.Response(503), httpx.Response(504, text="Gateway Timeout")]
        )
        with pytest.raises(PortalError, match="HTTP 504"):
            await cc.action("package_list")
        assert route.call_count == 4
        assert sleeps == [0.5, 1.0, 2.0]

    async def test_transport_error_retried_then_raised(self, cc, portal, sleeps):
        route = portal.get(API + "package_list").mock(side_effect=httpx.ConnectError("Verbindung abgelehnt"))
        with pytest.raises(PortalError, match="network error talking to https://opendata.schleswig-holstein.de"):
            await cc.action("package_list")
        assert route.call_count == 4
        assert sleeps == [0.5, 1.0, 2.0]

    async def test_transport_error_then_success(self, cc, portal, sleeps):
        route = portal.get(API + "package_list").mock(
            side_effect=[httpx.ReadTimeout("zu langsam"), httpx.Response(200, json=ok([]))]
        )
        assert await cc.action("package_list") == []
        assert route.call_count == 2
        assert sleeps == [0.5]

    async def test_bare_string_error_body(self, cc, portal):
        # CKAN answers unknown actions with a JSON string, not an error object.
        portal.get(API + "datastore_search").respond(400, json="Bad request - Action name not known: datastore_search")
        with pytest.raises(PortalError, match="datastore_search: Bad request - Action name not known"):
            await cc.action("datastore_search")

    async def test_firewall_403_hint(self, cc, portal):
        portal.get(API + "package_search").respond(403, text="<h1>403 Forbidden</h1><center>nginx</center>")
        with pytest.raises(PortalError, match="HTTP 403.*firewall"):
            await cc.action("package_search")

    async def test_download_retries_connection_reset(self, cc, portal, sleeps):
        route = portal.get("/files/a.csv").mock(
            side_effect=[httpx.ReadError("Connection reset by peer"), httpx.Response(200, content=b"a;b\n1;2\n")]
        )
        data, _, truncated = await cc.download(BASE + "/files/a.csv", 1024)
        assert data == b"a;b\n1;2\n" and not truncated
        assert route.call_count == 2 and sleeps == [0.5]

    async def test_resolve_follows_redirect(self, cc, portal):
        portal.get("/collection/x/aktuell").respond(302, headers={"Location": BASE + "/dataset/x-2026"})
        portal.get("/dataset/x-2026").respond(200, html="<html></html>")
        assert await cc.resolve(BASE + "/collection/x/aktuell") == BASE + "/dataset/x-2026"

    async def test_resolve_404(self, cc, portal):
        portal.get("/collection/nope/aktuell").respond(404)
        with pytest.raises(PortalError, match="HTTP 404"):
            await cc.resolve(BASE + "/collection/nope/aktuell")

    async def test_cache_hit_and_param_sensitivity(self, cc, portal):
        route = portal.get(API + "package_show").respond(json=ok(KIEL_PACKAGE))
        await cc.action("package_show", id="a")
        await cc.action("package_show", id="a")
        assert route.call_count == 1
        await cc.action("package_show", id="b")
        assert route.call_count == 2
        await cc.action("package_show", cache=False, id="a")
        assert route.call_count == 3

    async def test_cache_expiry(self, cc, portal, monkeypatch):
        route = portal.get(API + "package_show").respond(json=ok(KIEL_PACKAGE))
        now = [1000.0]
        monkeypatch.setattr(ckan.time, "monotonic", lambda: now[0])
        await cc.action("package_show", id="a")
        now[0] += 299
        await cc.action("package_show", id="a")
        assert route.call_count == 1
        now[0] += 2
        await cc.action("package_show", id="a")
        assert route.call_count == 2

    async def test_errors_are_not_cached(self, cc, portal):
        route = portal.get(API + "package_show")
        route.side_effect = [httpx.Response(404, json=NOT_FOUND_BODY), httpx.Response(200, json=ok(KIEL_PACKAGE))]
        with pytest.raises(PortalError):
            await cc.action("package_show", id="a")
        assert (await cc.action("package_show", id="a"))["title"].startswith("Kiel")


class TestDownload:
    URL = BASE + "/dataset/x/resource/y/download/daten.csv"

    async def test_full_download(self, cc, portal):
        route = portal.get("/dataset/x/resource/y/download/daten.csv").respond(
            content=b"a;b\n1;2\n", headers={"Content-Type": "text/csv"}
        )
        data, ctype, truncated = await cc.download(self.URL, 1024)
        assert (data, ctype, truncated) == (b"a;b\n1;2\n", "text/csv", False)
        assert route.calls.last.request.headers["Accept"] == "*/*"

    async def test_truncates_at_max_bytes(self, cc, portal):
        portal.get("/dataset/x/resource/y/download/daten.csv").respond(content=b"x" * 5000)
        data, _, truncated = await cc.download(self.URL, 1000)
        assert len(data) == 1000 and truncated is True

    async def test_http_error(self, cc, portal):
        portal.get("/dataset/x/resource/y/download/daten.csv").respond(404, text="nicht gefunden")
        with pytest.raises(PortalError, match="download failed with HTTP 404"):
            await cc.download(self.URL, 1000)

    async def test_follows_redirects(self, cc, portal):
        portal.get("/dataset/x/resource/y/download/daten.csv").respond(302, headers={"Location": BASE + "/files/daten.csv"})
        portal.get("/files/daten.csv").respond(content=b"ok")
        data, _, _ = await cc.download(self.URL, 100)
        assert data == b"ok"

    async def test_network_error(self, cc, portal):
        portal.get("/dataset/x/resource/y/download/daten.csv").mock(side_effect=httpx.ConnectError("weg"))
        with pytest.raises(PortalError, match="network error downloading"):
            await cc.download(self.URL, 100)


def test_modified_since_without_seconds_rejected():
    import pytest
    from opendata_sh_mcp.ckan import PortalError
    from opendata_sh_mcp.server import build_fq

    with pytest.raises(PortalError):
        build_fq(modified_since="2024-01-31T12:30")
