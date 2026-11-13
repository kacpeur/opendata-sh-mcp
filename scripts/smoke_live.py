"""Exercise every tool against the live portal. Run: python scripts/smoke_live.py"""

import asyncio
import json
import sys

from mcp import Client

from opendata_sh_mcp.server import mcp

failures: list[str] = []


def show(label: str, result, expect_error: bool = False):
    text = result.content[0].text if result.content else ""
    if result.is_error != expect_error:
        failures.append(label)
        print(f"FAIL {label}: {text[:300]}")
        return None
    if expect_error:
        print(f"ok   {label} (expected error): {text[:200]}")
        return None
    data = result.structured_content
    data = data["result"] if isinstance(data, dict) and set(data) == {"result"} else data
    print(f"ok   {label}: {json.dumps(data, ensure_ascii=False)[:300]}")
    return data


async def main() -> None:
    async with Client(mcp) as c:
        call = c.call_tool
        orgs = show("list_organizations", await call("list_organizations", {}))
        groups = show("list_groups", await call("list_groups", {}))
        for field in ("organization", "groups", "tags", "res_format", "license_id"):
            show(f"get_facets {field}", await call("get_facets", {"field": field, "limit": 5}))
        licenses = show("get_facets license_id in search", await call("get_facets", {"field": "license_id", "query": "Kiel", "limit": 3}))

        found = show("search_datasets", await call("search_datasets", {"query": "Kiel", "res_format": "CSV", "limit": 5}))
        show("search by org", await call("search_datasets", {"organization": orgs[0]["name"], "sort": "modified", "limit": 3}))
        show("search by group+license", await call("search_datasets", {"groups": [groups[0]["name"]], "license_id": licenses[0]["name"], "limit": 3}))
        show("search by tag+modified_since", await call("search_datasets", {"tags": ["Kiel"], "modified_since": "2024-01-01", "sort": "created", "limit": 3}))
        show("search sort title, page 2", await call("search_datasets", {"query": "Badegewässer", "sort": "title", "limit": 3, "offset": 3}))

        name = found["datasets"][0]["name"]
        ds = show("get_dataset", await call("get_dataset", {"dataset": name}))
        res = next(r for r in ds["resources"] if r.get("format") == "CSV")
        show("get_resource", await call("get_resource", {"resource_id": res["id"]}))
        prev = show("preview_resource CSV", await call("preview_resource", {"resource_id": res["id"], "max_rows": 3}))
        for key in ("dataset_url", "resource_url", "last_modified", "license"):
            if key not in prev["source"]:
                failures.append(f"source.{key}")
                print(f"FAIL preview source lacks {key}")

        # Kiel Stadtwerke: electricity delivered 2010-2023 is 18,357,698 MWh (checked by hand against the CSV).
        kiel = show("get_dataset stadtwerke-kiel", await call("get_dataset", {"dataset": "stadtwerke-kiel"}))
        stats = show(
            "analyze_resource sum",
            await call(
                "analyze_resource",
                {
                    "resource_id": kiel["resources"][0]["id"],
                    "filters": [{"column": "Jahr", "op": "ge", "value": 2010}, {"column": "Jahr", "op": "le", "value": 2023}],
                    "metrics": [{"agg": "sum", "column": "Stromabgabe ans Netz (MWh)"}, {"agg": "count"}],
                },
            ),
        )
        if stats and stats["result"] != {"sum(Stromabgabe ans Netz (MWh))": 18357698, "count(*)": 14}:
            failures.append("analyze_resource value")
            print(f"FAIL analyze_resource value: {stats['result']}")
        show(
            "analyze_resource group_by",
            await call("analyze_resource", {"resource_id": res["id"], "group_by": [prev["columns"][0]], "max_groups": 3}),
        )

        for fmt in ("JSON", "GEOJSON", "XML"):
            hit = show(f"search {fmt}", await call("search_datasets", {"res_format": fmt, "limit": 1}))
            d = show(f"get_dataset {fmt}", await call("get_dataset", {"dataset": hit["datasets"][0]["name"]}))
            r = next((r for r in d["resources"] if r.get("format") == fmt), d["resources"][0])
            show(f"preview_resource {fmt}", await call("preview_resource", {"resource_id": r["id"], "max_rows": 2}))

        # Real layouts the table detection must handle (see DESIGN.md).
        async def csv_of(dataset: str) -> str:
            d = show(f"get_dataset {dataset}", await call("get_dataset", {"dataset": dataset}))
            return next(r["id"] for r in d["resources"] if r.get("format") == "CSV")

        stat = show(
            "analyze Statistikamt layout",
            await call("analyze_resource", {"resource_id": await csv_of("bevolkerungsstand-nach-altersgruppen-in-zarpen-am-31-12")}),
        )
        if stat and not ("Insgesamt Anzahl" in stat["columns"] and stat.get("leading_text")):
            failures.append("Statistikamt columns")
            print(f"FAIL Statistikamt columns: {stat['columns']}")
        bad = show("analyze headerless pipe file", await call("analyze_resource", {"resource_id": await csv_of("badegewasser-infrastruktur-2023-05-01")}))
        if bad and bad["columns"] != ["col_1", "col_2", "col_3"]:
            failures.append("headerless columns")
        zipped = show("analyze ZIP labelled CSV", await call("analyze_resource", {"resource_id": await csv_of("automatische-zahlstelle-elsensee-2010")}))
        if zipped and zipped["rows_total"] != 8760:
            failures.append("zip rows")

        haushalt = show("get_dataset haushalt-sh-2021", await call("get_dataset", {"dataset": "haushalt-sh-2021"}))
        xlsx_id = next(r["id"] for r in haushalt["resources"] if r.get("format") == "XLSX")
        show("preview_resource XLSX", await call("preview_resource", {"resource_id": xlsx_id, "max_rows": 2}))
        budget = show(
            "analyze_resource XLSX",
            await call("analyze_resource", {"resource_id": xlsx_id, "metrics": [{"agg": "sum", "column": "Ansatz 2021 T€"}]}),
        )
        if budget and budget["rows_total"] != 5974:
            failures.append("xlsx rows")

        show("query_datastore", await call("query_datastore", {"resource_id": res["id"]}), expect_error=True)

        series = show("search series", await call("search_datasets", {"query": "dataset_type:collection", "limit": 3}))
        sname = series["datasets"][0]["name"]
        show("get_dataset series", await call("get_dataset", {"dataset": sname}))
        show("preview_series_latest", await call("preview_series_latest", {"series": sname, "max_rows": 3}))
        show("preview_series_latest unknown", await call("preview_series_latest", {"series": "gibt-es-nicht-xyz"}), expect_error=True)
        show("get_dataset unknown", await call("get_dataset", {"dataset": "gibt-es-nicht-xyz"}), expect_error=True)

        text = (await c.read_resource(f"opendata-sh://dataset/{name}")).contents[0].text
        print(f"ok   resource read: {text[:120]}")
        prompt = await c.get_prompt("find_data", {"topic": "Badegewässer"})
        print(f"ok   prompt: {prompt.messages[0].content.text[:80]}")

    if failures:
        print(f"\n{len(failures)} failed: {', '.join(failures)}")
        sys.exit(1)
    print("\nall live checks passed")


asyncio.run(main())
