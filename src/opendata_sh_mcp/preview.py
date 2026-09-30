"""Turn the first bytes of a downloaded resource into a small, model-friendly preview."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

TEXT_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


def decode(data: bytes) -> str:
    for enc in TEXT_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _drop_partial_last_line(text: str, truncated: bool) -> str:
    if truncated and "\n" in text:
        return text[: text.rfind("\n")]
    return text


def sniff_delimiter(text: str) -> str:
    sample = text[:20000]
    try:
        # German open data frequently uses ';' with ',' as decimal separator.
        return csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        return ";" if sample.count(";") > sample.count(",") else ","


_NUMERIC_CELL = re.compile(r"^[+-]?[\d.,]+([eE][+-]?\d+)?$")


def _blank(row: list[str]) -> bool:
    return not any(cell.strip() for cell in row)


def parse_csv_table(text: str) -> dict[str, Any]:
    """Split a CSV export into header, units row, data rows and any trailing section.

    Some portal files (e.g. KFÜ-Messwerte) put units in the second line and append a
    station list or comments after a blank line; those must not be counted as data.
    """
    delimiter = sniff_delimiter(text)
    lines = text.splitlines(keepends=True)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    parsed: list[tuple[int, list[str]]] = []  # (start line index, row)
    start = 0
    for row in reader:
        parsed.append((start, row))
        start = reader.line_num
    rows = [(i, r) for i, r in parsed if not _blank(r)]
    if not rows:
        return {"delimiter": delimiter, "header": [], "units": None, "body": [], "footer": None}
    header = [h.strip() for h in rows[0][1]]
    body: list[list[str]] = []
    footer = None
    saw_blank = False
    for (i, r) in parsed[parsed.index(rows[0]) + 1 :]:
        if _blank(r):
            saw_blank = True
            continue
        if saw_blank and len(header) >= 2 and len(r) < len(header) / 2:
            footer = "".join(lines[i:]).strip()
            break
        saw_blank = False
        body.append(r)
    units = None
    if len(body) >= 2:
        first, second = body[0], body[1]
        first_numeric = any(_NUMERIC_CELL.match(c.strip()) for c in first if c.strip())
        second_numeric = any(_NUMERIC_CELL.match(c.strip()) for c in second if c.strip())
        if not first_numeric and second_numeric and not first[0].strip():
            units = {h: u.strip() for h, u in zip(header, first) if h and u.strip()}
            body = body[1:]
    return {"delimiter": delimiter, "header": header, "units": units, "body": body, "footer": footer}


def preview_csv(text: str, max_rows: int, truncated: bool) -> dict[str, Any]:
    text = _drop_partial_last_line(text, truncated)
    table = parse_csv_table(text)
    header, body = table["header"], table["body"]
    if not header:
        return {"kind": "csv", "columns": [], "rows": [], "note": "empty file"}
    out = {
        "kind": "csv",
        "delimiter": table["delimiter"],
        "columns": header,
        "rows": [dict(zip(header, r)) for r in body[:max_rows]],
        "rows_in_sample": len(body),
        "sample_truncated": truncated,
    }
    if table["units"]:
        out["units"] = table["units"]
    if table["footer"]:
        # often a legend of column codes; worth reading, but not data rows
        out["trailing_text"] = table["footer"][:4000]
    return out


def preview_json(text: str, max_rows: int, truncated: bool) -> dict[str, Any]:
    if truncated:
        return {"kind": "json", "note": "file larger than the preview limit; raw head shown", "head": text[:4000]}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"kind": "json", "note": f"invalid JSON: {exc}", "head": text[:4000]}
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        feats = data.get("features") or []
        return {
            "kind": "geojson",
            "feature_count": len(feats),
            "property_keys": sorted({k for f in feats[:200] for k in (f.get("properties") or {})}),
            "geometry_types": sorted({(f.get("geometry") or {}).get("type", "None") for f in feats[:200]}),
            "features": [
                {"properties": f.get("properties"), "geometry_type": (f.get("geometry") or {}).get("type")}
                for f in feats[:max_rows]
            ],
        }
    if isinstance(data, list):
        return {"kind": "json", "length": len(data), "items": data[:max_rows]}
    if isinstance(data, dict):
        # Show the shape: top-level keys and the first items of any list values.
        return {
            "kind": "json",
            "keys": list(data)[:100],
            "sample": {k: (v[:max_rows] if isinstance(v, list) else v) for k, v in list(data.items())[:30]},
        }
    return {"kind": "json", "value": data}


def build_preview(data: bytes, fmt: str, content_type: str, max_rows: int, truncated: bool) -> dict[str, Any]:
    fmt = (fmt or "").lower()
    ctype = (content_type or "").lower()
    if fmt in {"csv", "tsv", "txt"} or "csv" in ctype:
        return preview_csv(decode(data), max_rows, truncated)
    if fmt in {"json", "geojson"} or "json" in ctype:
        return preview_json(decode(data), max_rows, truncated)
    if fmt in {"xml", "rdf", "gml", "kml", "wfs", "wms", "html"} or re.search(r"[/+]xml\b", ctype) or "html" in ctype:
        return {"kind": fmt or "text", "head": decode(data)[:4000], "sample_truncated": truncated}
    return {
        "kind": "binary",
        "note": f"format '{fmt or ctype}' is not previewed; use the download URL directly",
        "bytes_read": len(data),
    }
