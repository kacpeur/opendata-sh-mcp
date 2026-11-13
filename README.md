# opendata-sh-mcp

An MCP server that lets Claude (or any MCP client) search and read the
[Open Data Portal Schleswig-Holstein](https://opendata.schleswig-holstein.de),
a CKAN catalogue run by the state.

## Tools

| Tool | What it does |
| --- | --- |
| `search_datasets` | Full-text search with filters (organization, category, tags, format, license, modified since), sorting and paging |
| `get_dataset` | Full metadata of one dataset: publisher, license, time range, tags, resources |
| `get_resource` | Metadata of one file or service endpoint |
| `preview_resource` | Parsed first rows of CSV (detects delimiter, encoding, title lines, two-row headers, missing headers, ZIP-wrapped files) and Excel sheets (`sheet` picks one), structure of JSON/GeoJSON, head of XML. Uses the DataStore on portals that have one |
| `query_datastore` | Filter, search, sort and page rows of a DataStore table (the SH portal has no DataStore, so this reports that clearly; kept for other CKAN portals) |
| `list_organizations` | Publishers with dataset counts |
| `list_groups` | Thematic categories with dataset counts |
| `preview_series_latest` | Newest release of a data series (Datenserie) via `/collection/<name>/aktuell`, previewing its best file (CSV first) |
| `analyze_resource` | Exact figures over the whole file (CSV, Excel, JSON records, GeoJSON): row filters, group-by, count, count_distinct, sum, mean, median, min, max. Parses German numbers (`1.234,5`, `7,3e-02`) and dates (`29.09.2026 13:00`); separates title lines, units rows and trailing legends from data |
| `get_facets` | Top values of organization, groups, tags, res_format or license_id, optionally within a search |

Every tool that touches data returns a `source` block (dataset and file links,
last-modified date, publisher, license, attribution, retrieval time), and the
server instructions and `find_data` prompt tell the assistant to cite it and to
compute numbers with `analyze_resource` rather than read them off a preview.

Also: resource template `opendata-sh://dataset/{name}` and prompt `find_data(topic, region)`.

## Install and run

```bash
pip install -e .            # or: uv pip install -e .
opendata-sh-mcp             # stdio (default)
opendata-sh-mcp --transport streamable-http --port 8000   # remote/HTTP
```

Claude Desktop / Claude Code config:

```json
{
  "mcpServers": {
    "opendata-sh": { "command": "opendata-sh-mcp" }
  }
}
```

or `claude mcp add opendata-sh -- opendata-sh-mcp`.

## Configuration (environment variables)

| Variable | Default |
| --- | --- |
| `OPENDATA_SH_BASE_URL` | `https://opendata.schleswig-holstein.de` |
| `OPENDATA_SH_USER_AGENT` | `opendata-sh-mcp/<version> (...)` (must not look like a browser or AI assistant, see below) |
| `OPENDATA_SH_TIMEOUT` | `30` seconds |
| `OPENDATA_SH_CACHE_TTL` | `300` seconds for metadata calls and parsed files in `analyze_resource` |
| `OPENDATA_SH_MAX_ANALYZE_MB` | `50`: larger files are refused by `analyze_resource` rather than analysed partially |

The portal is protected by the Anubis bot filter, which serves a JavaScript
challenge to browser-like user agents. The server therefore sends a plain,
descriptive User-Agent and reports a clear error if it ever receives a challenge
page instead of JSON.

## Tests

```bash
pip install -e '.[dev]'
pytest
```

The suite mocks the portal over HTTP (respx), so it runs offline.
`scripts/smoke_live.py` exercises every tool against the real portal
(last run 2026-09-30: all checks passed).
