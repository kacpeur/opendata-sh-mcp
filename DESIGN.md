# Design: MCP server for opendata.schleswig-holstein.de

## What the portal is

- CKAN 2.10 with the state's own extension `ckanext-odsh`, about 32,000 datasets.
- API: `https://opendata.schleswig-holstein.de/api/3/action/<action>` (standard CKAN, no key needed for reads).
- Metadata follows DCAT-AP.de:
  - license ids are URIs (`http://dcat-ap.de/def/licenses/cc-by/4.0`);
  - categories (CKAN groups) are EU theme codes (`tran`, `envi`, `gove`, `regi`, ...);
  - many fields live in `extras` (`temporal_start`, `spatial_text`, `publisher_name`, `licenseAttributionByText`).
- Data series ("Datenserien", `type: collection`, about 136 of them) link periodic releases to a parent dataset via `child_of` relationships; big series have 100+ members. `/collection/<name>/aktuell` redirects to the newest release. `/collection/<name>/aktuell.csv` only works when that release has a CSV (404 otherwise, 500 for unknown series), so the server follows the redirect instead.
- Resource `format` values are EU vocabulary URIs (`http://publications.europa.eu/resource/authority/file-type/CSV`); the server reduces them to `CSV`. Search filters (`res_format:"CSV"`) match either case.
- Also offered: a DCAT feed (`/catalog.ttl`, `/catalog.jsonld`, `/dataset/<name>.rdf`). The server doesn't use it; the action API returns the same data and is easier to query.
- There is no DataStore: `datastore_search` is an unknown action (the portal answers with a bare JSON string). `preview_resource` therefore always downloads the file; `query_datastore` stays for other CKAN portals and reports the missing DataStore plainly.
- `organization_list?all_fields=true` is capped at 25 entries (131 organizations exist), so `list_organizations` reads the organization facet of `package_search` instead.
- An nginx firewall rule returns 403 when a double quote is followed anywhere later in the URL by SQL-like words (`created`, `update`, `drop`, `select`). Quoted `fq` filters plus `sort=metadata_created` tripped it, so `search_datasets` sends `sort` first. After a few 403s the portal briefly resets connections.
- Some CSVs have several sections. KFÜ-Messwerte (series `kfue-7t`, daily releases, 7 days of hourly values) has a units row under the header, values like `7,335e-02`, then a blank line, a station legend and comments. The CSV parser treats the units row and the trailing section as metadata (`units`, `trailing_text`), not data, and German dates (`29.09.2026 00:00:00`) compare chronologically in filters and in min/max.
- A plain search for a periodic dataset returns its individual releases (2,454 for "KFÜ-Messwerte"), ranked by relevance rather than date. Series are found with `dataset_type:collection` in the query and are flagged `is_series`.
- Independently, about one connection in four is reset. API calls, downloads and redirects retry transport errors up to 4 times (0.5, 1, 2 s).
- Everything sits behind the **Anubis** bot filter:
  - browser-like user agents (`Mozilla...`) get a proof-of-work challenge;
  - AI-assistant agents (`Claude-User`, `ChatGPT-User`) are denied.

## Design principles

1. **Task-shaped tools, not a 1:1 API mirror.** The model gets a small set of tools that match how people explore a catalogue: search, inspect, peek, drill down. Raw CKAN payloads run to 10 to 50 KB per dataset and are trimmed to the fields a model needs, plus a `portal_url` for citation.
2. **Discoverable filters.** `list_organizations`, `list_groups` and `get_facets` expose the valid values for `search_datasets` filters. The model never has to guess slugs or license URIs.
3. **Safe previews of real data.** `preview_resource` streams at most `max_kb` (default 512 KiB) of a file and parses it. For CSV it sniffs `;` vs `,` and falls back from UTF-8 to cp1252. For JSON it reports structure, and for GeoJSON it reports feature properties and geometry types. It never loads a whole large file into context.
4. **Read-only and idempotent.** All tools carry `readOnlyHint` and `openWorldHint`. There is no auth and nothing is written.
5. **Exact numbers and visible sources.** Previews are samples and say so (`sample_note`). Every figure an answer needs comes from `analyze_resource`, which reads the whole file (up to 50 MB, refusing rather than guessing beyond that) and computes in Python. Data tools return a `source` block so each answer can cite dataset link, file link, date and license.
6. **Good failure messages.** CKAN errors (404 "Not found", validation errors) and Anubis challenge pages become readable tool errors, so the model can correct itself.
7. **Polite client.** The server sends a descriptive non-browser User-Agent, caches metadata for 5 minutes, and retries 429/5xx and connection resets with backoff.

## Tools

| Tool | CKAN call(s) | Notes |
| --- | --- | --- |
| `search_datasets(query, organization, groups, tags, res_format, license_id, modified_since, sort, limit, offset)` | `package_search` | Filters become a quoted Solr `fq`. The sort is an enum (relevance, modified, created, title). Returns `total` and `next_offset` for paging |
| `get_dataset(dataset)` | `package_show` | Publisher, license and attribution, time range, spatial text, tags, series member count and first 10 ids, resources |
| `get_resource(resource_id)` | `resource_show` | |
| `preview_resource(resource_id, max_rows, max_kb)` | `resource_show`, then `datastore_search` or a download | |
| `query_datastore(resource_id, q, filters, fields, sort, limit, offset)` | `datastore_search` | Only on portals with a DataStore; not the SH portal |
| `analyze_resource(resource_id, filters, group_by, metrics, sort, max_groups)` | `resource_show`, `package_show`, full download | Exact count/sum/mean/median/min/max, German number parsing, column-level decimal detection, refuses files over the cap, caches the last 3 parsed files for 5 minutes. Verified live: Kiel electricity 2010 to 2023 = 18,357,698 MWh, matching a hand calculation |
| `preview_series_latest(series, ...)` | `GET /collection/<name>/aktuell` (redirect), `package_show`, download | Picks CSV, then JSON, GeoJSON, XML |
| `list_organizations()` | `package_search` organization facet | Publishers with at least one dataset, sorted by count |
| `list_groups()` | `group_list?all_fields=true` | |
| `get_facets(field, query, limit)` | `package_search?rows=0&facet.field=...` | Top tags, formats, licenses, publishers |

Also:
- **Resource** `opendata-sh://dataset/{name}`, so clients that support resources can attach a dataset's metadata as context.
- **Prompt** `find_data(topic, region)`, a guided workflow: search in German, inspect, preview, then answer with the license and a link.

## Stack and layout

- **Language and SDK:** Python 3.10+ with the official MCP Python SDK 2.x (`MCPServer`, formerly FastMCP).
- **HTTP:** `httpx` (async).
- **Transports:** stdio by default for Claude Desktop and Claude Code. `--transport streamable-http` hosts it as a remote server.
- **Code layout:**
  - `src/opendata_sh_mcp/ckan.py`: CKAN client (retries, cache, Anubis detection, capped streaming download).
  - `src/opendata_sh_mcp/preview.py`: CSV and JSON preview parsing.
  - `src/opendata_sh_mcp/analyze.py`: whole-file record loading, number parsing and aggregation.
  - `src/opendata_sh_mcp/server.py`: tools, resource, prompt and CLI.
- **Tests:**
  - `tests/` holds the offline pytest suite, with the portal mocked through respx.
  - `scripts/smoke_live.py` is an end-to-end check against the real portal.

## Possible next steps

- A `download_resource` tool that saves a full file locally for analysis (with pandas, for example) instead of returning it inline.
- Geo support: spatial search with the `ext_bbox` parameter, if ckanext-spatial is enabled, and WFS GetFeature previews for `access_services`.
- Package for `uvx opendata-sh-mcp` on PyPI and add an MCP registry entry.
- Generalise to any CKAN portal (GovData, other states) through `OPENDATA_SH_BASE_URL`, which already works for plain CKAN.
