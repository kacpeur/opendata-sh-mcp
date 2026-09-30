"""End-to-end tests of every tool, the resource template and the prompt via the in-process MCP client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from opendata_sh_mcp import ckan, server

from .conftest import (
    ANUBIS_HTML,
    API,
    BASE,
    CSV_BYTES,
    CSV_RESOURCE,
    DATASTORE_RESOURCE,
    DATASTORE_SEARCH_RESULT,
    GEOJSON_BYTES,
    GEOJSON_RESOURCE,
    GROUP_LIST,
    KIEL_PACKAGE,
    NOT_FOUND_BODY,
    ORGANIZATION_FACET,
    PACKAGE_SEARCH_RESULT,
    WMS_RESOURCE,
    ok,
    params_of,
)

EXPECTED_TOOLS = {
    "search_datasets",
    "get_dataset",
    "get_resource",
    "preview_resource",
    "query_datastore",
    "preview_series_latest",
    "list_organizations",
    "list_groups",
    "get_facets",
    "analyze_resource",
}


def dataset_for_source(portal):
    """Tools that read a resource also fetch its dataset for the source block."""
    return portal.get(API + "package_show").respond(json=ok(KIEL_PACKAGE))


async def call(name: str, args: dict[str, Any] | None = None):
    async with Client(server.mcp) as c:
        return await c.call_tool(name, args or {})


def data(result) -> Any:
    assert not result.is_error, result.content[0].text
    sc = result.structured_content
    return sc["result"] if set(sc) == {"result"} else sc


def err(result) -> str:
    assert result.is_error
    return result.content[0].text


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


async def test_list_tools():
    async with Client(server.mcp) as c:
        tools = (await c.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert len(tools) == len(EXPECTED_TOOLS) == 10
    for t in tools:
        assert t.annotations.read_only_hint is True
        assert t.annotations.open_world_hint is True
        assert t.description
    by_name = {t.name: t for t in tools}
    props = by_name["search_datasets"].input_schema["properties"]
    assert props["sort"]["enum"] == ["relevance", "modified", "created", "title"]
    assert props["limit"]["maximum"] == 50
    assert by_name["get_facets"].input_schema["required"] == ["field"]
    assert by_name["preview_series_latest"].input_schema["required"] == ["series"]


async def test_server_info_and_instructions():
    async with Client(server.mcp) as c:
        assert c.server_info.name == "opendata-sh"
        assert "search_datasets" in (c.instructions or "")


async def test_prompt_find_data():
    async with Client(server.mcp) as c:
        prompts = (await c.list_prompts()).prompts
        assert [p.name for p in prompts] == ["find_data"]
        args = {a.name: a.required for a in prompts[0].arguments}
        assert args == {"topic": True, "region": False}
        r = await c.get_prompt("find_data", {"topic": "Badegewässer", "region": "Kreis Ostholstein"})
    text = r.messages[0].content.text
    assert r.messages[0].role == "user"
    assert "'Badegewässer' for Kreis Ostholstein" in text
    assert "dataset_url" in text and "license" in text and "analyze_resource" in text


async def test_prompt_default_region():
    async with Client(server.mcp) as c:
        r = await c.get_prompt("find_data", {"topic": "Verkehr"})
    assert "for Schleswig-Holstein on" in r.messages[0].content.text


async def test_resource_template_dataset(portal):
    route = portal.get(API + "package_show").respond(json=ok(KIEL_PACKAGE))
    async with Client(server.mcp) as c:
        templates = (await c.list_resource_templates()).resource_templates
        assert [t.uri_template for t in templates] == ["opendata-sh://dataset/{name}"]
        r = await c.read_resource("opendata-sh://dataset/kiel-bevoelkerung-stadtteile")
    content = r.contents[0]
    assert content.mime_type == "application/json"
    body = json.loads(content.text)
    assert body["title"] == "Kiel: Bevölkerung nach Stadtteilen"
    assert body["portal_url"] == f"{BASE}/dataset/kiel-bevoelkerung-stadtteile"
    assert params_of(route)["id"] == "kiel-bevoelkerung-stadtteile"


async def test_resource_template_not_found_raises(portal):
    portal.get(API + "package_show").respond(404, json=NOT_FOUND_BODY)
    async with Client(server.mcp) as c:
        with pytest.raises(MCPError):
            await c.read_resource("opendata-sh://dataset/gibt-es-nicht")


async def test_resource_template_not_found_keeps_portal_message(portal):
    portal.get(API + "package_show").respond(404, json=NOT_FOUND_BODY)
    async with Client(server.mcp) as c:
        with pytest.raises(MCPError, match="Not found"):
            await c.read_resource("opendata-sh://dataset/gibt-es-nicht")


# ---------------------------------------------------------------------------
# search_datasets
# ---------------------------------------------------------------------------


async def test_search_defaults(portal):
    route = portal.get(API + "package_search").respond(json=ok(PACKAGE_SEARCH_RESULT))
    out = data(await call("search_datasets"))
    p = params_of(route)
    assert p["q"] == "*:*"
    assert "fq" not in p
    assert p["sort"] == "score desc, metadata_modified desc"
    assert p["rows"] == "10"
    assert p["start"] == "0"
    assert route.calls.last.request.headers["User-Agent"] == ckan.DEFAULT_USER_AGENT
    assert "Mozilla" not in route.calls.last.request.headers["User-Agent"]

    assert out["total"] == 42
    assert out["returned"] == 2
    assert out["next_offset"] == 2
    kiel, luebeck = out["datasets"]
    assert kiel == {
        "name": "kiel-bevoelkerung-stadtteile",
        "title": "Kiel: Bevölkerung nach Stadtteilen",
        "organization": "Landeshauptstadt Kiel",
        "notes": "Die Tabelle enthält die Einwohnerzahlen der Kieler Stadtteile zum Stichtag 31.12. des "
        "jeweiligen Jahres, gegliedert nach Geschlecht und Altersgruppen.",
        "groups": ["soci", "regi"],
        "formats": ["CSV", "GEOJSON"],
        "license": "Datenlizenz Deutschland – Namensnennung – Version 2.0",
        "metadata_modified": "2024-03-06T08:00:00.000000",
        "portal_url": f"{BASE}/dataset/kiel-bevoelkerung-stadtteile",
    }
    # license_title missing -> falls back to the license URI; notes clipped to 300 chars
    assert luebeck["license"] == "http://dcat-ap.de/def/licenses/cc-by/4.0"
    assert luebeck["formats"] == ["CSV", "JSON"]
    assert len(luebeck["notes"]) == 300 and luebeck["notes"].endswith("…")


async def test_search_all_filters(portal):
    route = portal.get(API + "package_search").respond(json=ok({"count": 0, "results": []}))
    out = data(
        await call(
            "search_datasets",
            {
                "query": "Einwohner Kiel",
                "organization": "landeshauptstadt-kiel",
                "groups": ["soci", "gove"],
                "tags": ['Kieler "Woche"', "Stadtteile"],
                "res_format": "geojson",
                "license_id": "http://dcat-ap.de/def/licenses/cc-by/4.0",
                "modified_since": "2024-01-01",
                "sort": "modified",
                "limit": 25,
                "offset": 50,
            },
        )
    )
    p = params_of(route)
    assert p["q"] == "Einwohner Kiel"
    assert p["fq"] == (
        'organization:"landeshauptstadt-kiel" AND groups:"soci" AND groups:"gove" AND '
        'tags:"Kieler \\"Woche\\"" AND tags:"Stadtteile" AND res_format:"GEOJSON" AND '
        'license_id:"http://dcat-ap.de/def/licenses/cc-by/4.0" AND '
        "metadata_modified:[2024-01-01T00:00:00Z TO *]"
    )
    assert p["sort"] == "metadata_modified desc"
    assert (p["rows"], p["start"]) == ("25", "50")
    assert out == {"total": 0, "offset": 50, "returned": 0, "next_offset": None, "datasets": []}


@pytest.mark.parametrize(
    ("sort", "solr"),
    [
        ("relevance", "score desc, metadata_modified desc"),
        ("modified", "metadata_modified desc"),
        ("created", "metadata_created desc"),
        ("title", "title_string asc"),
    ],
)
async def test_search_sort_mapping(portal, sort, solr):
    route = portal.get(API + "package_search").respond(json=ok({"count": 0, "results": []}))
    data(await call("search_datasets", {"sort": sort}))
    assert params_of(route)["sort"] == solr


async def test_search_modified_since_with_time(portal):
    route = portal.get(API + "package_search").respond(json=ok({"count": 0, "results": []}))
    data(await call("search_datasets", {"modified_since": "2024-06-30T12:00:00"}))
    assert params_of(route)["fq"] == "metadata_modified:[2024-06-30T12:00:00Z TO *]"


async def test_search_last_page_has_no_next_offset(portal):
    portal.get(API + "package_search").respond(json=ok({**PACKAGE_SEARCH_RESULT, "count": 42}))
    out = data(await call("search_datasets", {"offset": 40, "limit": 2}))
    assert out["next_offset"] is None


async def test_search_invalid_modified_since_makes_no_request(portal):
    route = portal.get(API + "package_search").respond(json=ok({"count": 0, "results": []}))
    msg = err(await call("search_datasets", {"modified_since": "01.01.2024"}))
    assert "modified_since must be an ISO date" in msg
    assert route.call_count == 0


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 51}, {"offset": -1}, {"sort": "popular"}])
async def test_search_argument_validation(portal, args):
    route = portal.get(API + "package_search").respond(json=ok({"count": 0, "results": []}))
    assert err(await call("search_datasets", args))
    assert route.call_count == 0


async def test_search_anubis_surfaces_as_tool_error(portal):
    portal.get(API + "package_search").respond(200, html=ANUBIS_HTML)
    msg = err(await call("search_datasets", {"query": "Kiel"}))
    assert "bot-protection" in msg


async def test_search_retries_503(portal, sleeps):
    route = portal.get(API + "package_search")
    route.side_effect = [httpx.Response(503), httpx.Response(200, json=ok(PACKAGE_SEARCH_RESULT))]
    out = data(await call("search_datasets", {"query": "Haushalt"}))
    assert out["total"] == 42
    assert route.call_count == 2
    assert sleeps == [0.5]


async def test_search_sends_sort_before_quoted_filters(portal):
    # The portal firewall 403s a '"' followed later by words like "created".
    route = portal.get(API + "package_search").respond(json=ok(PACKAGE_SEARCH_RESULT))
    data(await call("search_datasets", {"query": '"Kieler Woche"', "tags": ["Kiel"], "sort": "created"}))
    qs = route.calls.last.request.url.query.decode()
    assert qs.index("created") < qs.index("%22")


async def test_formats_from_eu_vocabulary_uris(portal):
    uri = "http://publications.europa.eu/resource/authority/file-type/"
    pkg = {**KIEL_PACKAGE, "resources": [{**CSV_RESOURCE, "format": uri + "CSV"}, {**GEOJSON_RESOURCE, "format": uri + "GEOJSON"}]}
    portal.get(API + "package_show").respond(json=ok(pkg))
    out = data(await call("get_dataset", {"dataset": "kiel"}))
    assert out["formats"] == ["CSV", "GEOJSON"]
    assert [r["format"] for r in out["resources"]] == ["CSV", "GEOJSON"]


async def test_series_members_are_capped(portal):
    rels = [{"type": "child_of", "__extras": {"subject_package_id": f"id-{i}"}} for i in range(25)]
    portal.get(API + "package_show").respond(json=ok({**KIEL_PACKAGE, "relationships_as_object": rels}))
    out = data(await call("get_dataset", {"dataset": "serie"}))
    assert out["series_member_count"] == 25
    assert out["series_members"] == [f"id-{i}" for i in range(10)]


async def test_search_results_are_cached_across_calls(portal):
    route = portal.get(API + "package_search").respond(json=ok(PACKAGE_SEARCH_RESULT))
    async with Client(server.mcp) as c:
        await c.call_tool("search_datasets", {"query": "Kiel"})
        await c.call_tool("search_datasets", {"query": "Kiel"})
        await c.call_tool("search_datasets", {"query": "Lübeck"})
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# get_dataset / get_resource
# ---------------------------------------------------------------------------


async def test_get_dataset(portal):
    route = portal.get(API + "package_show").respond(json=ok(KIEL_PACKAGE))
    out = data(await call("get_dataset", {"dataset": "kiel-bevoelkerung-stadtteile"}))
    assert params_of(route)["id"] == "kiel-bevoelkerung-stadtteile"
    assert out["id"] == KIEL_PACKAGE["id"]
    assert out["organization"] == {"name": "landeshauptstadt-kiel", "title": "Landeshauptstadt Kiel"}
    assert out["notes"].startswith("Die Tabelle enthält die Einwohnerzahlen")
    assert "<" not in out["notes"]
    assert out["tags"] == ["Bevölkerung", "Einwohner", "Stadtteile"]
    assert out["groups"] == ["soci", "regi"]
    assert out["license_id"] == "http://dcat-ap.de/def/licenses/dl-by-de/2.0"
    assert out["license_url"] == "https://www.govdata.de/dl-de/by-2-0"
    assert out["temporal"] == {"start": "2010-01-01T00:00:00", "end": "2023-12-31T00:00:00"}
    assert out["spatial"] == "Kiel, Landeshauptstadt"
    assert out["issued"] == "2019-05-14"
    assert out["publisher"] == "Landeshauptstadt Kiel, Statistikstelle"
    assert out["contact_email"] == "opendata@kiel.de"
    assert out["attribution"] == "Landeshauptstadt Kiel (2024)"
    assert out["frequency"].endswith("/ANNUAL")
    assert out["series_members"] == ["kiel-bevoelkerung-stadtteile-2022", "kiel-bevoelkerung-stadtteile-2023"]
    assert out["author"] == "Amt für Digitalisierung, Kiel"
    assert out["maintainer"] == "Statistikstelle Kiel"
    assert out["metadata_created"] == "2019-05-14T08:30:00.000000"
    assert "openness_score" not in out  # None values are dropped

    res = {r["id"]: r for r in out["resources"]}
    assert len(res) == 4
    assert res[CSV_RESOURCE["id"]] == {
        "id": CSV_RESOURCE["id"],
        "name": "Einwohner nach Stadtteilen 2023 (CSV)",
        "format": "CSV",
        "url": CSV_RESOURCE["url"],
        "size": 2048,
        "last_modified": "2024-03-05T10:00:00.000000",
        "datastore_active": False,
    }
    assert res[DATASTORE_RESOURCE["id"]]["datastore_active"] is True
    assert res[DATASTORE_RESOURCE["id"]]["last_modified"] == "2024-03-06T08:00:00.000000"
    assert res[GEOJSON_RESOURCE["id"]]["format"] == "GEOJSON"
    wms = res[WMS_RESOURCE["id"]]
    assert wms["name"] == "WMS-Dienst Stadtteile"  # falls back to description
    assert wms["format"] is None


async def test_get_dataset_fallbacks(portal):
    pkg = {
        "id": "x1",
        "name": "minimal",
        "title": "Minimal",
        "maintainer_email": "poststelle@lfu.landsh.de",
        "temporal_start": "2020-01-01",
        "spatial_uri": "http://dcat-ap.de/def/politicalGeocoding/stateKey/01",
        "extras": [{"key": "spatial_uri", "value": "http://dcat-ap.de/def/politicalGeocoding/stateKey/01"}, {"value": "no key"}],
        "relationships_as_object": [],
    }
    portal.get(API + "package_show").respond(json=ok(pkg))
    out = data(await call("get_dataset", {"dataset": "minimal"}))
    assert out["contact_email"] == "poststelle@lfu.landsh.de"
    assert out["temporal"] == {"start": "2020-01-01", "end": None}
    assert out["spatial"] == "http://dcat-ap.de/def/politicalGeocoding/stateKey/01"
    for absent in ("series_members", "tags", "resources", "publisher", "attribution", "issued", "license"):
        assert absent not in out


async def test_get_dataset_not_found(portal, sleeps):
    route = portal.get(API + "package_show").respond(404, json=NOT_FOUND_BODY)
    msg = err(await call("get_dataset", {"dataset": "gibt-es-nicht"}))
    assert "package_show: Not found" in msg
    assert route.call_count == 1 and sleeps == []


async def test_get_resource(portal):
    dataset_for_source(portal)
    route = portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    out = data(await call("get_resource", {"resource_id": CSV_RESOURCE["id"]}))
    assert params_of(route)["id"] == CSV_RESOURCE["id"]
    source = out.pop("source")
    assert source["dataset_url"] == f"{BASE}/dataset/kiel-bevoelkerung-stadtteile"
    assert source["resource_url"] == CSV_RESOURCE["url"]
    assert source["last_modified"] == "2024-03-05T10:00:00.000000"
    assert source["license"] and source["publisher"] and source["retrieved_at"]
    assert out == {
        "id": CSV_RESOURCE["id"],
        "name": "Einwohner nach Stadtteilen 2023 (CSV)",
        "format": "CSV",
        "url": CSV_RESOURCE["url"],
        "size": 2048,
        "last_modified": "2024-03-05T10:00:00.000000",
        "datastore_active": False,
        "description": "Einwohnerinnen und Einwohner mit Hauptwohnsitz je Stadtteil.",
        "mimetype": "text/csv",
        "dataset_id": CSV_RESOURCE["package_id"],
        "created": "2024-02-01T09:15:00.000000",
    }


async def test_get_resource_datastore_and_nulls(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(DATASTORE_RESOURCE))
    out = data(await call("get_resource", {"resource_id": DATASTORE_RESOURCE["id"]}))
    assert out["datastore_active"] is True
    assert "size" not in out


async def test_get_resource_not_found(portal):
    portal.get(API + "resource_show").respond(
        404, json={"success": False, "error": {"message": "Not found: Resource was not found.", "__type": "Not Found Error"}}
    )
    assert "Resource was not found" in err(await call("get_resource", {"resource_id": "nope"}))


# ---------------------------------------------------------------------------
# preview_resource
# ---------------------------------------------------------------------------


async def test_preview_csv_download(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    dl = portal.get(CSV_RESOURCE["url"].removeprefix(BASE)).respond(
        content=CSV_BYTES, headers={"Content-Type": "text/csv; charset=windows-1252"}
    )
    out = data(await call("preview_resource", {"resource_id": CSV_RESOURCE["id"], "max_rows": 2}))
    assert dl.call_count == 1
    assert out["read_from"] == "download"
    assert out["resource"]["id"] == CSV_RESOURCE["id"]
    assert out["kind"] == "csv" and out["delimiter"] == ";"
    assert out["columns"][-1] == "Fläche km²"
    assert out["rows"] == [
        {"Stadtteil": "Altstadt", "Jahr": "2023", "Einwohner": "1.234", "Anteil in %": "0,5", "Fläche km²": "0,35"},
        {"Stadtteil": "Düsternbrook", "Jahr": "2023", "Einwohner": "2.345", "Anteil in %": "0,9", "Fläche km²": "2,41"},
    ]
    assert out["rows_in_sample"] == 5
    assert out["sample_truncated"] is False


async def test_preview_csv_truncated_download(portal):
    dataset_for_source(portal)
    big = CSV_BYTES + b"".join(f"Stadtteil {i};2023;{i};0,1;0,2\r\n".encode("cp1252") for i in range(2000))
    assert len(big) > 16 * 1024
    portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    portal.get(CSV_RESOURCE["url"].removeprefix(BASE)).respond(content=big, headers={"Content-Type": "text/csv"})
    out = data(await call("preview_resource", {"resource_id": CSV_RESOURCE["id"], "max_rows": 3, "max_kb": 16}))
    assert out["sample_truncated"] is True
    head = big[: 16 * 1024].decode("cp1252")
    complete_lines = head[: head.rfind("\n")].count("\n")  # header + body lines
    assert out["rows_in_sample"] == complete_lines
    assert len(out["rows"]) == 3


async def test_preview_geojson_download(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(GEOJSON_RESOURCE))
    portal.get(GEOJSON_RESOURCE["url"].removeprefix(BASE)).respond(
        content=GEOJSON_BYTES, headers={"Content-Type": "application/geo+json"}
    )
    out = data(await call("preview_resource", {"resource_id": GEOJSON_RESOURCE["id"]}))
    assert out["kind"] == "geojson"
    assert out["feature_count"] == 3
    assert out["features"][1]["properties"]["name"] == "Düsternbrook"


async def test_preview_datastore(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(DATASTORE_RESOURCE))
    ds = portal.get(API + "datastore_search").respond(json=ok(DATASTORE_SEARCH_RESULT))
    out = data(await call("preview_resource", {"resource_id": DATASTORE_RESOURCE["id"], "max_rows": 3}))
    p = params_of(ds)
    assert p["resource_id"] == DATASTORE_RESOURCE["id"]
    assert p["limit"] == "3"
    assert out["read_from"] == "datastore"
    assert out["total_rows"] == 30
    assert out["columns"] == ["Stadtteil", "Jahr", "Einwohner", "Anteil_%"]
    assert out["rows"][1]["Stadtteil"] == "Düsternbrook"


async def test_preview_datastore_is_not_cached(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(DATASTORE_RESOURCE))
    ds = portal.get(API + "datastore_search").respond(json=ok(DATASTORE_SEARCH_RESULT))
    async with Client(server.mcp) as c:
        await c.call_tool("preview_resource", {"resource_id": DATASTORE_RESOURCE["id"]})
        await c.call_tool("preview_resource", {"resource_id": DATASTORE_RESOURCE["id"]})
    assert ds.call_count == 2


async def test_preview_datastore_failure_falls_back_to_download(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(DATASTORE_RESOURCE))
    ds = portal.get(API + "datastore_search").respond(
        404, json={"success": False, "error": {"message": "Resource \"e9b8\" was not found.", "__type": "Not Found Error"}}
    )
    dl = portal.get(DATASTORE_RESOURCE["url"].removeprefix(BASE)).respond(content=CSV_BYTES, headers={"Content-Type": "text/csv"})
    out = data(await call("preview_resource", {"resource_id": DATASTORE_RESOURCE["id"]}))
    assert ds.call_count == 1 and dl.call_count == 1
    assert out["read_from"] == "download"
    assert out["kind"] == "csv"


async def test_preview_without_url(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(WMS_RESOURCE))
    assert "resource has no download URL" in err(await call("preview_resource", {"resource_id": WMS_RESOURCE["id"]}))


async def test_preview_download_http_error(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    portal.get(CSV_RESOURCE["url"].removeprefix(BASE)).respond(410)
    assert "download failed with HTTP 410" in err(await call("preview_resource", {"resource_id": CSV_RESOURCE["id"]}))


async def test_preview_binary(portal):
    dataset_for_source(portal)
    res = {**CSV_RESOURCE, "format": "ZIP", "url": BASE + "/files/archiv.zip"}
    portal.get(API + "resource_show").respond(json=ok(res))
    portal.get("/files/archiv.zip").respond(content=b"PK\x03\x04" + bytes(200), headers={"Content-Type": "application/zip"})
    out = data(await call("preview_resource", {"resource_id": res["id"]}))
    assert out["kind"] == "binary" and out["bytes_read"] == 204


@pytest.mark.parametrize("args", [{"max_rows": 0}, {"max_rows": 201}, {"max_kb": 15}, {"max_kb": 5121}])
async def test_preview_argument_validation(portal, args):
    route = portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    assert err(await call("preview_resource", {"resource_id": "x", **args}))
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# query_datastore
# ---------------------------------------------------------------------------


async def test_query_datastore_all_params(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(DATASTORE_RESOURCE))
    route = portal.get(API + "datastore_search").respond(json=ok(DATASTORE_SEARCH_RESULT))
    out = data(
        await call(
            "query_datastore",
            {
                "resource_id": DATASTORE_RESOURCE["id"],
                "q": "Gaarden",
                "filters": {"Jahr": "2023", "Stadtteil": ["Düsternbrook", "Wik"]},
                "fields": ["Stadtteil", "Einwohner"],
                "sort": "Einwohner desc",
                "limit": 3,
                "offset": 6,
            },
        )
    )
    p = params_of(route)
    assert p["resource_id"] == DATASTORE_RESOURCE["id"]
    assert p["q"] == "Gaarden"
    assert json.loads(p["filters"]) == {"Jahr": "2023", "Stadtteil": ["Düsternbrook", "Wik"]}
    assert p["fields"] == "Stadtteil,Einwohner"
    assert p["sort"] == "Einwohner desc"
    assert (p["limit"], p["offset"]) == ("3", "6")
    assert out["total_rows"] == 30
    assert out["columns"] == [
        {"id": "Stadtteil", "type": "text"},
        {"id": "Jahr", "type": "int"},
        {"id": "Einwohner", "type": "int"},
        {"id": "Anteil_%", "type": "numeric"},
    ]
    assert len(out["rows"]) == 3


async def test_query_datastore_minimal_omits_optional_params(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(DATASTORE_RESOURCE))
    route = portal.get(API + "datastore_search").respond(json=ok(DATASTORE_SEARCH_RESULT))
    data(await call("query_datastore", {"resource_id": "abc", "filters": {}}))
    p = params_of(route)
    assert set(p.keys()) == {"resource_id", "limit", "offset"}
    assert (p["limit"], p["offset"]) == ("100", "0")


async def test_query_datastore_portal_without_datastore(portal):
    portal.get(API + "datastore_search").respond(400, json="Bad request - Action name not known: datastore_search")
    assert "this portal has no DataStore" in err(await call("query_datastore", {"resource_id": "abc"}))


async def test_query_datastore_not_in_datastore(portal):
    portal.get(API + "datastore_search").respond(
        404, json={"success": False, "error": {"message": "Resource \"abc\" was not found.", "__type": "Not Found Error"}}
    )
    assert 'Resource "abc" was not found.' in err(await call("query_datastore", {"resource_id": "abc"}))


async def test_query_datastore_validation_error_409(portal):
    portal.get(API + "datastore_search").respond(
        409, json={"success": False, "error": {"__type": "Validation Error", "filters": ["Unbekannte Spalte"]}}
    )
    assert "Validation Error" in err(await call("query_datastore", {"resource_id": "abc", "filters": {"X": 1}}))


# ---------------------------------------------------------------------------
# preview_series_latest
# ---------------------------------------------------------------------------


def _series_routes(portal, latest: dict, file_route: str | None = None, content: bytes = CSV_BYTES):
    portal.get("/collection/kiel-bevoelkerung-stadtteile/aktuell").respond(
        302, headers={"Location": f"{BASE}/dataset/{latest['name']}"}
    )
    portal.get(f"/dataset/{latest['name']}").respond(200, html="<html></html>")
    show = portal.get(API + "package_show").respond(json=ok(latest))
    dl = portal.get(file_route).respond(content=content, headers={"Content-Type": "application/octet-stream"}) if file_route else None
    return show, dl


async def test_preview_series_latest(portal):
    csv_url = "/dataset/k/resource/r1/download/einwohner.csv"
    latest = {
        **KIEL_PACKAGE,
        "name": "kiel-bevoelkerung-stadtteile-2023",
        "resources": [
            {"id": "pdf", "format": "http://publications.europa.eu/resource/authority/file-type/PDF", "url": BASE + "/x.pdf"},
            {"id": "csv", "format": "http://publications.europa.eu/resource/authority/file-type/CSV", "url": BASE + csv_url},
        ],
    }
    show, dl = _series_routes(portal, latest, csv_url)
    out = data(await call("preview_series_latest", {"series": "kiel-bevoelkerung-stadtteile", "max_rows": 1}))
    assert params_of(show)["id"] == "kiel-bevoelkerung-stadtteile-2023"
    assert dl.call_count == 1
    assert "Mozilla" not in dl.calls.last.request.headers["User-Agent"]
    assert out["series"] == "kiel-bevoelkerung-stadtteile"
    assert out["latest"]["name"] == "kiel-bevoelkerung-stadtteile-2023"
    assert out["resource"]["id"] == "csv" and out["resource"]["format"] == "CSV"  # CSV preferred over PDF
    assert out["kind"] == "csv"  # forced CSV even though the content type is generic
    assert out["delimiter"] == ";"
    assert out["rows"] == [
        {"Stadtteil": "Altstadt", "Jahr": "2023", "Einwohner": "1.234", "Anteil in %": "0,5", "Fläche km²": "0,35"}
    ]


async def test_preview_series_latest_truncation(portal):
    csv_url = "/dataset/k/resource/r1/download/big.csv"
    latest = {**KIEL_PACKAGE, "name": "kfue-7t-2024-09", "resources": [{"id": "r1", "format": "CSV", "url": BASE + csv_url}]}
    _series_routes(portal, latest, csv_url, CSV_BYTES * 400)
    out = data(await call("preview_series_latest", {"series": "kiel-bevoelkerung-stadtteile", "max_kb": 16}))
    assert out["sample_truncated"] is True
    assert len(out["rows"]) == 20


async def test_preview_series_latest_without_resources(portal):
    _series_routes(portal, {**KIEL_PACKAGE, "name": "leer-2024", "resources": []})
    out = data(await call("preview_series_latest", {"series": "kiel-bevoelkerung-stadtteile"}))
    assert out["latest"]["name"] == "leer-2024"
    assert "no downloadable resource" in out["note"]


async def test_preview_series_latest_unknown_series(portal):
    portal.get("/collection/gibt-es-nicht/aktuell").respond(404, html="<h1>Not Found</h1>")
    assert "unknown data series 'gibt-es-nicht'" in err(await call("preview_series_latest", {"series": "gibt-es-nicht"}))


async def test_preview_series_latest_no_dataset_redirect(portal):
    portal.get("/collection/leer/aktuell").respond(200, html="<html></html>")
    assert "has no current release" in err(await call("preview_series_latest", {"series": "leer"}))


# ---------------------------------------------------------------------------
# list_organizations / list_groups / get_facets
# ---------------------------------------------------------------------------


async def test_list_organizations(portal):
    facets = {**PACKAGE_SEARCH_RESULT, "search_facets": {"organization": {"items": ORGANIZATION_FACET}}}
    route = portal.get(API + "package_search").respond(json=ok(facets))
    out = data(await call("list_organizations"))
    p = params_of(route)
    assert json.loads(p["facet.field"]) == ["organization"]
    assert p["facet.limit"] == "-1" and p["rows"] == "0"
    assert [o["name"] for o in out] == ["landeshauptstadt-kiel", "melund", "hansestadt-luebeck"]
    assert out[0] == {"name": "landeshauptstadt-kiel", "title": "Landeshauptstadt Kiel", "datasets": 481}


async def test_list_groups(portal):
    route = portal.get(API + "group_list").respond(json=ok(GROUP_LIST))
    out = data(await call("list_groups"))
    assert params_of(route)["all_fields"] == "true"
    assert out == [
        {"name": "envi", "title": "Umwelt", "datasets": 812},
        {"name": "tran", "title": "Verkehr", "datasets": 190},  # empty title -> display_name
        {"name": "gove", "title": "Regierung und öffentlicher Sektor", "datasets": 144},
        {"name": "soci", "title": "Bevölkerung und Gesellschaft", "datasets": 305},
    ]


async def test_list_groups_portal_down(portal, sleeps):
    route = portal.get(API + "group_list").mock(side_effect=httpx.ConnectError("Name or service not known"))
    assert "network error" in err(await call("list_groups"))
    assert route.call_count == 4 and sleeps == [0.5, 1.0, 2.0]


async def test_get_facets(portal):
    route = portal.get(API + "package_search").respond(json=ok(PACKAGE_SEARCH_RESULT))
    out = data(await call("get_facets", {"field": "res_format", "query": "Kiel", "limit": 5}))
    p = params_of(route)
    assert p["q"] == "Kiel"
    assert p["rows"] == "0"
    assert json.loads(p["facet.field"]) == ["res_format"]
    assert p["facet.limit"] == "5"
    assert p["facet.mincount"] == "1"
    assert "fq" not in p and "sort" not in p
    assert out == [
        {"name": "CSV", "display_name": "CSV", "count": 311},
        {"name": "GEOJSON", "display_name": "GeoJSON", "count": 58},
        {"name": "JSON", "display_name": "JSON", "count": 17},
    ]


async def test_get_facets_defaults_and_missing_field(portal):
    route = portal.get(API + "package_search").respond(json=ok(PACKAGE_SEARCH_RESULT))
    out = data(await call("get_facets", {"field": "license_id"}))
    p = params_of(route)
    assert p["q"] == "*:*" and p["facet.limit"] == "50"
    assert out == []


async def test_get_facets_rejects_unknown_field(portal):
    route = portal.get(API + "package_search").respond(json=ok(PACKAGE_SEARCH_RESULT))
    assert err(await call("get_facets", {"field": "maintainer"}))
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


async def test_lifespan_closes_http_client(portal):
    portal.get(API + "group_list").respond(json=ok(GROUP_LIST))
    async with Client(server.mcp) as c:
        await c.call_tool("list_groups", {})
        assert server.client._client is not None
    assert server.client._client is None


# ---------------------------------------------------------------------------
# analyze_resource and source blocks
# ---------------------------------------------------------------------------


async def test_analyze_resource_exact_group_sum(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    portal.get(CSV_RESOURCE["url"].removeprefix(BASE)).respond(content=CSV_BYTES * 1)
    out = data(
        await call(
            "analyze_resource",
            {
                "resource_id": CSV_RESOURCE["id"],
                "filters": [{"column": "Einwohner", "op": "ge", "value": 2000}],
                "group_by": ["Jahr"],
                "metrics": [{"agg": "sum", "column": "Einwohner"}, {"agg": "count"}],
            },
        )
    )
    assert out["complete"] is True
    assert out["rows_total"] == 5 and out["rows_matched"] == 4
    assert out["groups"] == [{"key": {"Jahr": "2023"}, "sum(Einwohner)": 49998, "count(*)": 4}]
    assert out["source"]["dataset_url"] == f"{BASE}/dataset/kiel-bevoelkerung-stadtteile"
    assert out["source"]["resource_url"] == CSV_RESOURCE["url"]


async def test_analyze_resource_refuses_partial_file(portal, monkeypatch):
    monkeypatch.setattr(server, "MAX_ANALYZE_MB", 16 / 1024)  # 16 KiB
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    portal.get(CSV_RESOURCE["url"].removeprefix(BASE)).respond(content=CSV_BYTES * 400)
    assert "exact figures are not possible" in err(await call("analyze_resource", {"resource_id": CSV_RESOURCE["id"]}))


async def test_analyze_resource_bad_column(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    portal.get(CSV_RESOURCE["url"].removeprefix(BASE)).respond(content=CSV_BYTES)
    msg = err(await call("analyze_resource", {"resource_id": CSV_RESOURCE["id"], "group_by": ["Kreis"]}))
    assert "unknown column(s) ['Kreis']" in msg


async def test_analyze_resource_rejects_unknown_agg(portal):
    msg = err(await call("analyze_resource", {"resource_id": "x", "metrics": [{"agg": "avg", "column": "a"}]}))
    assert "agg" in msg


async def test_preview_carries_source_and_sample_note(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    portal.get(CSV_RESOURCE["url"].removeprefix(BASE)).respond(content=CSV_BYTES)
    out = data(await call("preview_resource", {"resource_id": CSV_RESOURCE["id"], "max_rows": 2}))
    assert out["sample_note"] == "first 2 of 5 rows"
    assert out["source"]["license"] and out["source"]["dataset_url"].endswith("/kiel-bevoelkerung-stadtteile")


async def test_preview_survives_missing_parent_dataset(portal):
    portal.get(API + "package_show").respond(404, json=NOT_FOUND_BODY)
    portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    portal.get(CSV_RESOURCE["url"].removeprefix(BASE)).respond(content=CSV_BYTES)
    out = data(await call("preview_resource", {"resource_id": CSV_RESOURCE["id"]}))
    assert out["source"]["resource_url"] == CSV_RESOURCE["url"]
    assert "dataset_url" not in out["source"]


async def test_get_dataset_has_source(portal):
    portal.get(API + "package_show").respond(json=ok(KIEL_PACKAGE))
    out = data(await call("get_dataset", {"dataset": "kiel-bevoelkerung-stadtteile"}))
    assert out["source"]["dataset_url"] == out["portal_url"]
    assert out["source"]["license"]


async def test_analyze_resource_reuses_parsed_file(portal):
    dataset_for_source(portal)
    portal.get(API + "resource_show").respond(json=ok(CSV_RESOURCE))
    dl = portal.get(CSV_RESOURCE["url"].removeprefix(BASE)).respond(content=CSV_BYTES)
    await call("analyze_resource", {"resource_id": CSV_RESOURCE["id"]})
    out = data(await call("analyze_resource", {"resource_id": CSV_RESOURCE["id"], "group_by": ["Stadtteil"]}))
    assert dl.call_count == 1
    assert out["group_count"] == 5


async def test_search_marks_series(portal):
    series = {**KIEL_PACKAGE, "name": "kfue-7t", "type": "collection", "resources": []}
    portal.get(API + "package_search").respond(json=ok({"count": 1, "results": [series]}))
    out = data(await call("search_datasets", {"query": "KFÜ dataset_type:collection"}))
    assert out["datasets"][0]["is_series"] is True
