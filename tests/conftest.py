"""Shared fixtures: realistic CKAN payloads for opendata.schleswig-holstein.de and HTTP mocking."""

from __future__ import annotations

import json
import types
from typing import Any

import httpx
import pytest
import respx

from opendata_sh_mcp import ckan, server

BASE = "https://opendata.schleswig-holstein.de"
API = "/api/3/action/"


def ok(result: Any) -> dict[str, Any]:
    return {"help": f"{BASE}/api/3/action/help_show", "success": True, "result": result}


NOT_FOUND_BODY = {
    "help": f"{BASE}/api/3/action/help_show?name=package_show",
    "success": False,
    "error": {"message": "Not found", "__type": "Not Found Error"},
}

ANUBIS_HTML = """<!doctype html>
<html lang="en"><head><title>Making sure you&#39;re not a bot!</title>
<script id="anubis_challenge" type="application/json">{"rules":{"difficulty":4}}</script>
</head><body><h1>Making sure you're not a bot!</h1>
<p>Protected by <a href="https://github.com/TecharoHQ/anubis">Anubis</a> From Techaro.</p>
</body></html>"""

ORG_KIEL = {
    "id": "0b6a2c6e-3f5a-4a8b-9d1e-1a2b3c4d5e6f",
    "name": "landeshauptstadt-kiel",
    "title": "Landeshauptstadt Kiel",
    "type": "organization",
    "description": "Offene Daten der Landeshauptstadt Kiel",
    "image_url": "",
    "is_organization": True,
    "state": "active",
}

CSV_RESOURCE = {
    "id": "c1a5e2f0-1111-4c2b-8a3d-aaaaaaaaaaaa",
    "package_id": "d7e3f9a1-2222-4b5c-9d6e-bbbbbbbbbbbb",
    "name": "Einwohner nach Stadtteilen 2023 (CSV)",
    "description": "<p>Einwohnerinnen und Einwohner mit <b>Hauptwohnsitz</b> je Stadtteil.</p>",
    "format": "csv",
    "mimetype": "text/csv",
    "url": f"{BASE}/dataset/kiel-bevoelkerung-stadtteile/resource/c1a5/download/einwohner_2023.csv",
    "size": 2048,
    "created": "2024-02-01T09:15:00.000000",
    "last_modified": "2024-03-05T10:00:00.000000",
    "metadata_modified": "2024-03-05T10:00:01.000000",
    "datastore_active": False,
}

DATASTORE_RESOURCE = {
    "id": "e9b8c7d6-3333-4a1b-8c2d-cccccccccccc",
    "package_id": "d7e3f9a1-2222-4b5c-9d6e-bbbbbbbbbbbb",
    "name": "Einwohner nach Stadtteilen (DataStore)",
    "description": "Tabellarische Daten im DataStore",
    "format": "CSV",
    "mimetype": "text/csv",
    "url": f"{BASE}/dataset/kiel-bevoelkerung-stadtteile/resource/e9b8/download/einwohner_ds.csv",
    "size": None,
    "created": "2024-02-01T09:15:00.000000",
    "last_modified": None,
    "metadata_modified": "2024-03-06T08:00:00.000000",
    "datastore_active": True,
}

GEOJSON_RESOURCE = {
    "id": "f0f0f0f0-4444-4d4d-8e8e-dddddddddddd",
    "package_id": "d7e3f9a1-2222-4b5c-9d6e-bbbbbbbbbbbb",
    "name": "Stadtteilgrenzen",
    "description": "",
    "format": "GeoJSON",
    "mimetype": "application/geo+json",
    "url": f"{BASE}/dataset/kiel-bevoelkerung-stadtteile/resource/f0f0/download/stadtteile.geojson",
    "size": 512,
    "datastore_active": False,
}

WMS_RESOURCE = {
    "id": "a0a0a0a0-5555-4e4e-8f8f-eeeeeeeeeeee",
    "package_id": "d7e3f9a1-2222-4b5c-9d6e-bbbbbbbbbbbb",
    "name": "",
    "description": "WMS-Dienst Stadtteile",
    "format": "",
    "url": "",
    "datastore_active": False,
}

KIEL_PACKAGE = {
    "id": "d7e3f9a1-2222-4b5c-9d6e-bbbbbbbbbbbb",
    "name": "kiel-bevoelkerung-stadtteile",
    "title": "Kiel: Bevölkerung nach Stadtteilen",
    "notes": "<p>Die Tabelle enthält die <strong>Einwohnerzahlen</strong> der Kieler Stadtteile "
    "zum Stichtag 31.12. des jeweiligen Jahres, gegliedert nach Geschlecht und Altersgruppen.</p>",
    "author": "Amt für Digitalisierung, Kiel",
    "maintainer": "Statistikstelle Kiel",
    "maintainer_email": "statistik@kiel.de",
    "license_id": "http://dcat-ap.de/def/licenses/dl-by-de/2.0",
    "license_title": "Datenlizenz Deutschland – Namensnennung – Version 2.0",
    "license_url": "https://www.govdata.de/dl-de/by-2-0",
    "metadata_created": "2019-05-14T08:30:00.000000",
    "metadata_modified": "2024-03-06T08:00:00.000000",
    "organization": ORG_KIEL,
    "owner_org": ORG_KIEL["id"],
    "groups": [
        {"name": "soci", "title": "Bevölkerung und Gesellschaft", "display_name": "Bevölkerung und Gesellschaft"},
        {"name": "regi", "title": "Regionen und Städte", "display_name": "Regionen und Städte"},
    ],
    "tags": [
        {"name": "Bevölkerung", "display_name": "Bevölkerung"},
        {"name": "Einwohner", "display_name": "Einwohner"},
        {"name": "Stadtteile", "display_name": "Stadtteile"},
    ],
    "resources": [CSV_RESOURCE, DATASTORE_RESOURCE, GEOJSON_RESOURCE, WMS_RESOURCE],
    "extras": [
        {"key": "temporal_start", "value": "2010-01-01T00:00:00"},
        {"key": "temporal_end", "value": "2023-12-31T00:00:00"},
        {"key": "spatial_uri", "value": "http://dcat-ap.de/def/politicalGeocoding/districtKey/01002"},
        {"key": "issued", "value": "2019-05-14"},
        {"key": "spatial_text", "value": "Kiel, Landeshauptstadt"},
        {"key": "publisher_name", "value": "Landeshauptstadt Kiel, Statistikstelle"},
        {"key": "contact_email", "value": "opendata@kiel.de"},
        {"key": "licenseAttributionByText", "value": "Landeshauptstadt Kiel (2024)"},
    ],
    "frequency": "http://publications.europa.eu/resource/authority/frequency/ANNUAL",
    "relationships_as_object": [
        {"type": "child_of", "subject": "aaaa", "object": "d7e3f9a1",
         "__extras": {"subject_package_id": "kiel-bevoelkerung-stadtteile-2022", "object_package_id": "d7e3f9a1"}},
        {"type": "child_of", "__extras": {"subject_package_id": "kiel-bevoelkerung-stadtteile-2023"}},
        {"type": "links_to", "__extras": {"subject_package_id": "kiel-wahlbezirke"}},
    ],
    "num_resources": 4,
    "state": "active",
    "type": "dataset",
}

LUEBECK_PACKAGE = {
    "id": "11111111-aaaa-bbbb-cccc-222222222222",
    "name": "badegewaesser-luebeck",
    "title": "Badegewässerqualität Lübeck",
    "notes": "Messwerte der Badestellen an der Lübecker Bucht. " + "Sehr ausführlich. " * 40,
    "license_id": "http://dcat-ap.de/def/licenses/cc-by/4.0",
    "license_title": None,
    "metadata_modified": "2024-07-01T12:00:00.000000",
    "organization": {"name": "hansestadt-luebeck", "title": "Hansestadt Lübeck"},
    "groups": [{"name": "envi"}, {"name": "heal"}],
    "tags": [{"name": "Badestellen"}],
    "resources": [{"format": "json"}, {"format": "CSV"}, {"format": ""}],
}

PACKAGE_SEARCH_RESULT = {
    "count": 42,
    "sort": "score desc, metadata_modified desc",
    "facets": {},
    "results": [KIEL_PACKAGE, LUEBECK_PACKAGE],
    "search_facets": {
        "res_format": {
            "title": "res_format",
            "items": [
                {"name": "JSON", "display_name": "JSON", "count": 17},
                {"name": "CSV", "display_name": "CSV", "count": 311},
                {"name": "GEOJSON", "display_name": "GeoJSON", "count": 58},
            ],
        }
    },
}

ORGANIZATION_FACET = [
    {"name": "hansestadt-luebeck", "display_name": "Hansestadt Lübeck", "count": 120},
    {"name": "landeshauptstadt-kiel", "display_name": "Landeshauptstadt Kiel", "count": 481},
    {"name": "melund", "display_name": "Ministerium für Energiewende, Klimaschutz, Umwelt und Natur", "count": 233},
]

GROUP_LIST = [
    {"name": "envi", "title": "Umwelt", "display_name": "Umwelt", "package_count": 812},
    {"name": "tran", "title": "", "display_name": "Verkehr", "package_count": 190},
    {"name": "gove", "title": "Regierung und öffentlicher Sektor", "package_count": 144},
    {"name": "soci", "title": "Bevölkerung und Gesellschaft", "display_name": "Bevölkerung und Gesellschaft", "package_count": 305},
]

DATASTORE_SEARCH_RESULT = {
    "resource_id": DATASTORE_RESOURCE["id"],
    "fields": [
        {"id": "_id", "type": "int"},
        {"id": "Stadtteil", "type": "text"},
        {"id": "Jahr", "type": "int"},
        {"id": "Einwohner", "type": "int"},
        {"id": "Anteil_%", "type": "numeric"},
    ],
    "records": [
        {"_id": 1, "Stadtteil": "Altstadt", "Jahr": 2023, "Einwohner": 1234, "Anteil_%": 0.5},
        {"_id": 2, "Stadtteil": "Düsternbrook", "Jahr": 2023, "Einwohner": 2345, "Anteil_%": 0.9},
        {"_id": 3, "Stadtteil": "Gaarden-Ost", "Jahr": 2023, "Einwohner": 18765, "Anteil_%": 7.4},
    ],
    "total": 30,
    "limit": 3,
    "_links": {"start": "/api/3/action/datastore_search", "next": "/api/3/action/datastore_search?offset=3"},
}

# Semicolon-delimited, Windows-1252 encoded, decimal commas, umlauts: typical German export.
CSV_TEXT = (
    "Stadtteil;Jahr;Einwohner;Anteil in %;Fläche km²\r\n"
    "Altstadt;2023;1.234;0,5;0,35\r\n"
    "Düsternbrook;2023;2.345;0,9;2,41\r\n"
    "Gaarden-Ost;2023;18.765;7,4;1,86\r\n"
    "Schreventeich;2023;9.876;3,9;1,02\r\n"
    "Wik;2023;19.012;7,5;5,64\r\n"
)
CSV_BYTES = CSV_TEXT.encode("cp1252")

GEOJSON = {
    "type": "FeatureCollection",
    "name": "stadtteile",
    "features": [
        {"type": "Feature", "properties": {"name": "Altstadt", "nummer": 1},
         "geometry": {"type": "Polygon", "coordinates": [[[10.13, 54.32], [10.14, 54.32], [10.14, 54.33], [10.13, 54.32]]]}},
        {"type": "Feature", "properties": {"name": "Düsternbrook", "flaeche_km2": 2.41},
         "geometry": {"type": "MultiPolygon", "coordinates": []}},
        {"type": "Feature", "properties": {"name": "Friedrichsort"}, "geometry": None},
    ],
}
GEOJSON_BYTES = json.dumps(GEOJSON, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace asyncio.sleep as seen by ckan.py (only there) and record requested delays."""
    delays: list[float] = []

    async def fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        delays.append(delay)

    fake_asyncio = types.SimpleNamespace(sleep=fake_sleep)
    monkeypatch.setattr(ckan, "asyncio", fake_asyncio)
    return delays


@pytest.fixture(autouse=True)
async def fresh_client(sleeps: list[float]):
    """Reset the module-level CkanClient (cache and pooled http client) around every test."""
    server.client._cache.clear()
    server._RECORDS_CACHE.clear()
    await server.client.aclose()
    yield server.client
    server.client._cache.clear()
    await server.client.aclose()


@pytest.fixture
def portal():
    """respx router bound to the portal base URL; every request must be mocked."""
    with respx.mock(base_url=BASE, assert_all_called=False, assert_all_mocked=True) as router:
        yield router


def params_of(route: respx.Route, index: int = -1) -> httpx.QueryParams:
    return route.calls[index].request.url.params
