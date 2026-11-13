"""MCP server exposing the Schleswig-Holstein open data portal (CKAN) to LLM clients."""

from __future__ import annotations

import argparse
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__
from .analyze import Metric, RowFilter, analyze, load_records
from .ckan import CkanClient, PortalError
from .preview import build_preview
from .spreadsheet import SpreadsheetError, is_spreadsheet

INSTRUCTIONS = """\
Tools for the open data portal of the German state Schleswig-Holstein
(opendata.schleswig-holstein.de, a CKAN catalogue with about 33,000 datasets
from state ministries, municipalities such as Kiel and Lübeck, and state agencies).

Typical flow: search_datasets (German keywords work best, e.g. "Badegewässer",
"Haushalt", "Verkehr Kiel") -> get_dataset for resources -> preview_resource to
peek at CSV/Excel/JSON contents (query_datastore only for resources with datastore_active).
Data series (Datenserien) bundle periodic releases (daily measurements, monthly reports); a plain
search returns hundreds of individual releases. To get the newest one, find the series with
query "<topic> dataset_type:collection" (results marked is_series) and call preview_series_latest,
or search with sort="modified".
License ids are DCAT-AP.de URIs; category (group) ids are EU theme codes like "tran", "envi", "gove".
Use list_organizations / list_groups / get_facets to discover valid filter values.
For any number you report (totals, averages, counts, maxima), call analyze_resource, which
computes it exactly over the whole file; previews are samples, not the full data.
Tool results that touch data carry a "source" block (dataset_url, resource_url, last_modified,
license, attribution). Cite it with every answer based on the data.
"""

client = CkanClient()


@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await client.aclose()


mcp = MCPServer(
    name="opendata-sh",
    title="Open Data Schleswig-Holstein",
    instructions=INSTRUCTIONS,
    website_url="https://opendata.schleswig-holstein.de",
    version=__version__,
    lifespan=lifespan,
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=True)

# ---------------------------------------------------------------------------
# shaping helpers: CKAN payloads are verbose, so tools return trimmed views
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return text
    text = _TAG_RE.sub("", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _extras(pkg: dict[str, Any]) -> dict[str, Any]:
    return {e["key"]: e["value"] for e in pkg.get("extras") or [] if "key" in e}


def _temporal(pkg: dict[str, Any]) -> dict[str, Any] | None:
    ex = _extras(pkg)
    start = pkg.get("temporal_start") or ex.get("temporal_start")
    end = pkg.get("temporal_end") or ex.get("temporal_end")
    return {"start": start, "end": end} if start or end else None


def normalize_format(fmt: str | None) -> str | None:
    """Uppercase format code; the portal stores EU vocabulary URIs like .../file-type/CSV."""
    fmt = (fmt or "").strip().rstrip("/")
    return fmt.rsplit("/", 1)[-1].upper() or None


def resource_summary(res: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": res.get("id"),
        "name": res.get("name") or res.get("description") or res.get("id"),
        "format": normalize_format(res.get("format")),
        "url": res.get("url"),
        "size": res.get("size"),
        "last_modified": res.get("last_modified") or res.get("metadata_modified"),
        "datastore_active": bool(res.get("datastore_active")),
    }


def dataset_summary(pkg: dict[str, Any], client: CkanClient) -> dict[str, Any]:
    org = pkg.get("organization") or {}
    return {
        "name": pkg.get("name"),
        "title": pkg.get("title"),
        "organization": org.get("title") or org.get("name"),
        "notes": _clip(pkg.get("notes"), 300),
        "groups": [g.get("name") for g in pkg.get("groups") or []],
        "formats": sorted({f for r in pkg.get("resources") or [] if (f := normalize_format(r.get("format")))}),
        "license": pkg.get("license_title") or pkg.get("license_id"),
        "metadata_modified": pkg.get("metadata_modified"),
        "portal_url": client.dataset_url(pkg.get("name", "")),
        **({"is_series": True} if pkg.get("type") == "collection" else {}),
    }


MAX_SERIES_MEMBERS = 10


def dataset_detail(pkg: dict[str, Any], client: CkanClient) -> dict[str, Any]:
    org = pkg.get("organization") or {}
    ex = _extras(pkg)
    # Large series have hundreds of members; list a few ids and the count.
    members = [
        (rel.get("__extras") or {}).get("subject_package_id")
        for rel in pkg.get("relationships_as_object") or []
        if rel.get("type") == "child_of"
    ]
    detail = dataset_summary(pkg, client)
    detail.update(
        {
            "id": pkg.get("id"),
            "notes": _clip(pkg.get("notes"), 4000),
            "organization": {"name": org.get("name"), "title": org.get("title")},
            "tags": [t.get("name") for t in pkg.get("tags") or []],
            "license_id": pkg.get("license_id"),
            "license_url": pkg.get("license_url"),
            "author": pkg.get("author"),
            "maintainer": pkg.get("maintainer"),
            "metadata_created": pkg.get("metadata_created"),
            "temporal": _temporal(pkg),
            "spatial": pkg.get("spatial_text") or ex.get("spatial_text") or ex.get("spatial_uri"),
            "issued": pkg.get("issued") or ex.get("issued"),
            "frequency": pkg.get("frequency"),
            "openness_score": pkg.get("openness_score"),
            "publisher": ex.get("publisher_name"),
            "contact_email": ex.get("contact_email") or pkg.get("maintainer_email"),
            "attribution": ex.get("licenseAttributionByText"),
            "series_member_count": len(members) or None,
            "series_members": members[:MAX_SERIES_MEMBERS],
            "resources": [resource_summary(r) for r in pkg.get("resources") or []],
        }
    )
    return {k: v for k, v in detail.items() if v not in (None, "", [], {})}


def source_block(pkg: dict[str, Any] | None, res: dict[str, Any] | None = None) -> dict[str, Any]:
    """Provenance to cite with any answer built from this data."""
    pkg = pkg or {}
    res = res or {}
    org = pkg.get("organization") or {}
    ex = _extras(pkg)
    name = pkg.get("name")
    out = {
        "dataset": pkg.get("title"),
        "dataset_url": client.dataset_url(name) if name else None,
        "resource": res.get("name") or None,
        "resource_url": res.get("url"),
        "last_modified": res.get("last_modified") or res.get("metadata_modified") or pkg.get("metadata_modified"),
        "publisher": ex.get("publisher_name") or org.get("title"),
        "license": pkg.get("license_title") or pkg.get("license_id"),
        "license_url": pkg.get("license_url"),
        "attribution": ex.get("licenseAttributionByText"),
        "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


async def _resource_with_source(resource_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """resource_show plus the parent dataset's provenance (package_show is cached)."""
    res = await client.action("resource_show", id=resource_id)
    try:
        pkg = await client.action("package_show", id=res["package_id"]) if res.get("package_id") else None
    except PortalError:
        pkg = None  # provenance is best effort; the resource itself was found
    return res, source_block(pkg, res)


def _q(value: str) -> str:
    """Quote a value for a Solr fq clause."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_fq(
    organization: str | None = None,
    groups: list[str] | None = None,
    tags: list[str] | None = None,
    res_format: str | None = None,
    license_id: str | None = None,
    modified_since: str | None = None,
) -> str | None:
    clauses: list[str] = []
    if organization:
        clauses.append(f"organization:{_q(organization)}")
    for g in groups or []:
        clauses.append(f"groups:{_q(g)}")
    for t in tags or []:
        clauses.append(f"tags:{_q(t)}")
    if res_format:
        clauses.append(f"res_format:{_q(res_format.upper())}")
    if license_id:
        clauses.append(f"license_id:{_q(license_id)}")
    if modified_since:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?Z?)?", modified_since):
            raise PortalError("modified_since must be an ISO date like 2024-01-31 or 2024-01-31T12:00:00Z")
        ts = modified_since if "T" in modified_since else f"{modified_since}T00:00:00Z"
        if not ts.endswith("Z"):
            ts += "Z"
        clauses.append(f"metadata_modified:[{ts} TO *]")
    return " AND ".join(clauses) or None


SortKey = Literal["relevance", "modified", "created", "title"]
_SORTS = {
    "relevance": "score desc, metadata_modified desc",
    "modified": "metadata_modified desc",
    "created": "metadata_created desc",
    "title": "title_string asc",
}

# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def search_datasets(
    query: Annotated[str, Field(description="Free-text search (Solr syntax allowed). German terms match best. Empty lists everything.")] = "",
    organization: Annotated[str | None, Field(description="Organization slug, see list_organizations")] = None,
    groups: Annotated[list[str] | None, Field(description="Category ids, EU theme codes like 'tran' or 'envi' (all must match), see list_groups")] = None,
    tags: Annotated[list[str] | None, Field(description="Keywords (all must match)")] = None,
    res_format: Annotated[str | None, Field(description="Resource format, e.g. CSV, JSON, GEOJSON, XLSX, WMS")] = None,
    license_id: Annotated[str | None, Field(description="License id, see get_facets('license_id')")] = None,
    modified_since: Annotated[str | None, Field(description="Only datasets changed on/after this ISO date")] = None,
    sort: SortKey = "relevance",
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    """Search the Schleswig-Holstein open data catalogue and return matching datasets."""
    # Parameter order matters: the portal's firewall rejects a double quote followed later in
    # the URL by SQL-like words, and the sort key "metadata_created" is one. So sort goes first.
    result = await client.action(
        "package_search",
        sort=_SORTS[sort],
        rows=limit,
        start=offset,
        fq=build_fq(organization, groups, tags, res_format, license_id, modified_since),
        q=query or "*:*",
    )
    items = [dataset_summary(p, client) for p in result.get("results", [])]
    total = result.get("count", 0)
    return {
        "total": total,
        "offset": offset,
        "returned": len(items),
        "next_offset": offset + len(items) if offset + len(items) < total else None,
        "datasets": items,
    }


@mcp.tool(annotations=READ_ONLY)
async def get_dataset(
    dataset: Annotated[str, Field(description="Dataset name (slug) or id from search_datasets")],
) -> dict[str, Any]:
    """Full metadata for one dataset, including its resources (files/services) and license."""
    pkg = await client.action("package_show", id=dataset)
    return {**dataset_detail(pkg, client), "source": source_block(pkg)}


@mcp.tool(annotations=READ_ONLY)
async def get_resource(
    resource_id: Annotated[str, Field(description="Resource id from get_dataset")],
) -> dict[str, Any]:
    """Metadata for a single resource (file or service endpoint) of a dataset."""
    res, source = await _resource_with_source(resource_id)
    out = resource_summary(res)
    out.update(
        {
            "description": _clip(res.get("description"), 2000),
            "mimetype": res.get("mimetype"),
            "dataset_id": res.get("package_id"),
            "created": res.get("created"),
            "source": source,
        }
    )
    return {k: v for k, v in out.items() if v not in (None, "")}


@mcp.tool(annotations=READ_ONLY)
async def preview_resource(
    resource_id: Annotated[str, Field(description="Resource id from get_dataset")],
    max_rows: Annotated[int, Field(ge=1, le=200)] = 20,
    max_kb: Annotated[int, Field(ge=16, le=5120, description="Download at most this many KiB")] = 512,
    sheet: Annotated[str | None, Field(description="Excel only: sheet name or 1-based position. Default: first sheet holding a table")] = None,
) -> dict[str, Any]:
    """Peek into a resource's contents: parsed rows for CSV and Excel, structure for JSON/GeoJSON, raw head for XML.

    An Excel workbook lists its sheets under "sheets"; pass sheet= to read another one.

    Uses the CKAN DataStore when the resource is loaded there, otherwise downloads the
    first max_kb of the file.
    """
    res, source = await _resource_with_source(resource_id)
    meta = resource_summary(res)
    if res.get("datastore_active"):
        try:
            ds = await client.action("datastore_search", cache=False, resource_id=resource_id, limit=max_rows)
            rows = ds.get("records", [])
            return {
                "resource": meta,
                "read_from": "datastore",
                "source": source,
                "total_rows": ds.get("total"),
                "sample_note": f"first {len(rows)} of {ds.get('total')} rows; use analyze_resource for exact figures",
                "columns": [f["id"] for f in ds.get("fields", []) if f["id"] != "_id"],
                "rows": rows,
            }
        except PortalError:
            pass  # fall back to downloading the file
    url = res.get("url")
    if not url:
        raise PortalError("resource has no download URL")
    preview = await _download_preview(url, meta["format"] or "", max_rows, max_kb, sheet)
    return {"resource": meta, "read_from": "download", "source": source, **_sample_note(preview), **preview}


# A workbook cannot be read from its first bytes (it is a ZIP with the index at the end), so
# previews fetch it whole up to this size. Excel files on the portal are mostly under 1 MB.
SPREADSHEET_PREVIEW_BYTES = 20 * 1024 * 1024


async def _download_preview(url: str, fmt: str, max_rows: int, max_kb: int, sheet: str | None = None) -> dict[str, Any]:
    limit = SPREADSHEET_PREVIEW_BYTES if is_spreadsheet(fmt, "") else max_kb * 1024
    data, ctype, truncated = await client.download(url, limit)
    try:
        return build_preview(data, fmt, ctype, max_rows, truncated, sheet)
    except SpreadsheetError as exc:
        raise PortalError(str(exc)) from exc


def _sample_note(preview: dict[str, Any]) -> dict[str, Any]:
    """Say plainly that a preview is a sample, so it is not mistaken for the whole file."""
    if "rows" not in preview and "features" not in preview:
        return {}
    shown = len(preview.get("rows") or preview.get("features") or [])
    if preview.get("sample_truncated"):
        note = f"first {shown} rows of a partial download; the file is larger. Use analyze_resource for exact figures"
    elif "rows_in_sample" in preview:
        note = f"first {shown} of {preview['rows_in_sample']} rows"
    else:
        note = f"first {shown} of {preview.get('feature_count', shown)} features"
    return {"sample_note": note}


@mcp.tool(annotations=READ_ONLY)
async def query_datastore(
    resource_id: Annotated[str, Field(description="Resource id whose datastore_active is true")],
    q: Annotated[str | None, Field(description="Full-text search across all fields")] = None,
    filters: Annotated[dict[str, Any] | None, Field(description='Exact-match column filters, e.g. {"Jahr": "2023"}')] = None,
    fields: Annotated[list[str] | None, Field(description="Columns to return (default all)")] = None,
    sort: Annotated[str | None, Field(description='e.g. "Jahr desc"')] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    """Query tabular rows of a resource stored in the CKAN DataStore (filter, search, sort, page).

    Only works for resources with datastore_active=true. The Schleswig-Holstein portal currently
    runs without a DataStore, so use preview_resource there.
    """
    import json

    try:
        ds = await client.action(
        "datastore_search",
            cache=False,
            resource_id=resource_id,
            q=q,
            filters=json.dumps(filters) if filters else None,
            fields=",".join(fields) if fields else None,
            sort=sort,
            limit=limit,
            offset=offset,
        )
    except PortalError as exc:
        if "action name not known" in str(exc).lower():
            raise PortalError("this portal has no DataStore; use preview_resource to read the file instead") from exc
        raise
    _, source = await _resource_with_source(resource_id)
    return {
        "source": source,
        "total_rows": ds.get("total"),
        "columns": [{"id": f["id"], "type": f.get("type")} for f in ds.get("fields", []) if f["id"] != "_id"],
        "rows": ds.get("records", []),
    }


MAX_ANALYZE_MB = float(os.environ.get("OPENDATA_SH_MAX_ANALYZE_MB", "50"))
# Follow-up questions usually hit the same file, so keep the last few parsed files briefly.
_RECORDS_CACHE: dict[str, tuple[float, tuple[list[dict[str, Any]], dict[str, Any]]]] = {}
_RECORDS_CACHE_SIZE = 3


async def _load_full(url: str, fmt: str, sheet: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now = time.monotonic()
    key = f"{url}#{sheet or ''}"
    hit = _RECORDS_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    cap = int(MAX_ANALYZE_MB * 1024 * 1024)
    data, ctype, truncated = await client.download(url, cap + 1)
    if truncated:
        raise PortalError(
            f"file is larger than {MAX_ANALYZE_MB:g} MB, so exact figures are not possible here "
            "(raise OPENDATA_SH_MAX_ANALYZE_MB to allow bigger files)"
        )
    info: dict[str, Any] = {}
    records = load_records(data, fmt, ctype, info, max_unpacked=4 * cap, sheet=sheet)
    _RECORDS_CACHE.pop(key, None)
    _RECORDS_CACHE[key] = (now + client.cache_ttl, (records, info))
    while len(_RECORDS_CACHE) > _RECORDS_CACHE_SIZE:
        _RECORDS_CACHE.pop(next(iter(_RECORDS_CACHE)))
    return records, info


@mcp.tool(annotations=READ_ONLY)
async def analyze_resource(
    resource_id: Annotated[str, Field(description="Resource id from get_dataset (CSV, Excel, JSON records or GeoJSON)")],
    filters: Annotated[
        list[RowFilter] | None,
        Field(description='Row filters, all must match, e.g. [{"column": "Jahr", "op": "ge", "value": 2020}]'),
    ] = None,
    group_by: Annotated[list[str] | None, Field(description="Columns to group by, e.g. [\"Stadtteil\"]")] = None,
    metrics: Annotated[
        list[Metric] | None,
        Field(description='What to compute, e.g. [{"agg": "sum", "column": "Einwohner"}]. Default: row count'),
    ] = None,
    sort: Annotated[
        Literal["value_desc", "value_asc", "key"], Field(description="Group order: by the first metric, or by group key")
    ] = "value_desc",
    max_groups: Annotated[int, Field(ge=1, le=500)] = 50,
    sheet: Annotated[str | None, Field(description="Excel only: sheet name or 1-based position. Default: first sheet holding a table")] = None,
) -> dict[str, Any]:
    """Compute exact figures over the WHOLE file: filter rows, group them, and count, sum, average,
    median, min or max columns. Use this for every number you report instead of estimating from a preview.

    Numbers are parsed the German way (1.234,5) as well as the English way; dates like
    29.09.2026 13:00 work in gt/ge/lt/le filters, and min/max of a date column give the
    earliest/latest date (e.g. the newest entry). Values that are not
    numeric are skipped and counted under values_not_numeric. Call preview_resource first to see
    the column names: they can differ from the raw file (two header rows are combined, files
    without a header get col_1, col_2, ...; header_note says so). Title lines above the table
    come back as leading_text and notes below it as trailing_text; neither is counted as rows.
    """
    res, source = await _resource_with_source(resource_id)
    meta = resource_summary(res)
    if not res.get("url"):
        raise PortalError("resource has no download URL")
    records, info = await _load_full(res["url"], meta["format"] or "", sheet)
    result = analyze(records, filters or [], group_by, metrics, sort, max_groups)
    return {"resource": meta, "source": source, "complete": True, **result, **info}


_PREVIEW_ORDER = ("CSV", "JSON", "GEOJSON", "XLSX", "XLS", "XML", "TXT")


def _best_resource(resources: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = sorted(
        (r for r in resources if r.get("url")),
        key=lambda r: _PREVIEW_ORDER.index(f) if (f := normalize_format(r.get("format"))) in _PREVIEW_ORDER else len(_PREVIEW_ORDER),
    )
    return ranked[0] if ranked else None


@mcp.tool(annotations=READ_ONLY)
async def preview_series_latest(
    series: Annotated[str, Field(description="Name of a data series (Datenserie) parent dataset, e.g. epayment")],
    max_rows: Annotated[int, Field(ge=1, le=200)] = 20,
    max_kb: Annotated[int, Field(ge=16, le=5120)] = 512,
) -> dict[str, Any]:
    """Find the newest release of a data series and preview its best machine-readable file.

    Data series group periodic datasets (e.g. yearly or monthly releases); the portal's
    /collection/<name>/aktuell link points at the newest one. Search with
    query "dataset_type:collection" to find series.
    """
    link = f"{client.base_url}/collection/{quote(series, safe='')}/aktuell"
    try:
        final = await client.resolve(link)
    except PortalError as exc:
        raise PortalError(f"unknown data series '{series}' ({exc})") from exc
    m = re.search(r"/dataset/([^/?#]+)/?$", final)
    if not m:
        raise PortalError(f"data series '{series}' has no current release ({final})")
    pkg = await client.action("package_show", id=m.group(1))
    latest = dataset_summary(pkg, client)
    res = _best_resource(pkg.get("resources") or [])
    if res is None:
        return {
            "series": series,
            "latest": latest,
            "source": source_block(pkg),
            "note": "the newest release has no downloadable resource",
        }
    meta = resource_summary(res)
    preview = await _download_preview(res["url"], meta["format"] or "", max_rows, max_kb)
    return {
        "series": series,
        "latest": latest,
        "resource": meta,
        "source": source_block(pkg, res),
        **_sample_note(preview),
        **preview,
    }


@mcp.tool(annotations=READ_ONLY)
async def list_organizations() -> list[dict[str, Any]]:
    """All publishing organizations (ministries, municipalities, agencies) that have datasets, with counts."""
    # organization_list?all_fields=true is capped at 25 entries by CKAN, so read the
    # organization facet instead: one call, every publisher with at least one dataset.
    result = await client.action(
        "package_search", q="*:*", rows=0, **{"facet.field": '["organization"]', "facet.limit": -1, "facet.mincount": 1}
    )
    items = (result.get("search_facets", {}).get("organization") or {}).get("items", [])
    return sorted(
        ({"name": i.get("name"), "title": i.get("display_name"), "datasets": i.get("count")} for i in items),
        key=lambda o: -(o["datasets"] or 0),
    )


@mcp.tool(annotations=READ_ONLY)
async def list_groups() -> list[dict[str, Any]]:
    """Thematic categories (EU DCAT-AP themes, ids like 'envi', 'tran', 'gove') with dataset counts."""
    groups = await client.action("group_list", all_fields=True)
    return [{"name": g.get("name"), "title": g.get("title") or g.get("display_name"), "datasets": g.get("package_count")} for g in groups]


FacetField = Literal["organization", "groups", "tags", "res_format", "license_id"]


@mcp.tool(annotations=READ_ONLY)
async def get_facets(
    field: FacetField,
    query: Annotated[str, Field(description="Optional search to scope the counts")] = "",
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """Most common values of a metadata field (e.g. top tags or formats), optionally within a search."""
    import json

    result = await client.action(
        "package_search",
        q=query or "*:*",
        rows=0,
        **{"facet.field": json.dumps([field]), "facet.limit": limit, "facet.mincount": 1},
    )
    items = (result.get("search_facets", {}).get(field) or {}).get("items", [])
    return sorted(
        ({"name": i.get("name"), "display_name": i.get("display_name"), "count": i.get("count")} for i in items),
        key=lambda i: -(i["count"] or 0),
    )


# ---------------------------------------------------------------------------
# resources and prompts
# ---------------------------------------------------------------------------


@mcp.resource("opendata-sh://dataset/{name}", mime_type="application/json", description="Dataset metadata as JSON")
async def dataset_resource(name: str) -> dict[str, Any]:
    try:
        pkg = await client.action("package_show", id=name)
    except PortalError as exc:
        raise ResourceError(str(exc)) from exc
    return dataset_detail(pkg, client)


@mcp.prompt()
def find_data(topic: str, region: str = "Schleswig-Holstein") -> str:
    """Guide the model through finding and summarising open data on a topic."""
    return (
        f"Find open datasets about '{topic}' for {region} on the Schleswig-Holstein open data portal. "
        "Search with German keywords (try synonyms if results are thin), inspect the 2-3 most relevant "
        "datasets with get_dataset, and preview one machine-readable resource to learn its columns. "
        "Compute every figure you report with analyze_resource (exact, whole file); never estimate from "
        "a preview, which is only a sample. If several datasets fit equally well, name them and ask which "
        "one is meant. End with a Sources list built from each tool result's source block: dataset title "
        "and dataset_url, resource_url, last_modified, publisher and license (plus attribution if given)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for opendata.schleswig-holstein.de")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
