"""Turn the first bytes of a downloaded resource into a small, model-friendly preview."""

from __future__ import annotations

import csv
import io
import json
import re
import struct
import zipfile
import zlib
from collections import Counter
from typing import Any

from .spreadsheet import is_spreadsheet, pick_sheet, read_workbook
from .table import Table, detect_table

TEXT_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


def decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 with BOM, as Excel's "Unicode text" export writes
        return data.decode("utf-16", errors="replace")
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


DELIMITERS = (";", "\t", "|", ",")  # preference order when two fit equally well
_SNIFF_LINES = 200


def _read_rows(text: str, delimiter: str) -> list[list[str]]:
    try:
        # newline="" lets the csv module handle bare \r and line breaks inside quotes itself
        return list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error:
        # broken quoting somewhere: fall back to plain splitting rather than failing the file
        return [line.split(delimiter) for line in text.splitlines()]


def sniff_delimiter(text: str) -> str:
    """Pick the delimiter that splits the most lines into the same number of fields.

    csv.Sniffer guesses from character frequencies and gets German files wrong when free text
    holds semicolons or decimal commas. Counting fields per line is more reliable: the real
    delimiter gives one dominant field count; score it by lines at that count times fields gained.
    """
    lines = [ln for ln in text.splitlines()[: _SNIFF_LINES * 2] if ln.strip()][:_SNIFF_LINES]
    sample = "\n".join(lines)
    best, best_score = ";" if sample.count(";") > sample.count(",") else ",", 0
    for delimiter in DELIMITERS:
        if delimiter not in sample:
            continue
        counts = Counter(len(row) for row in _read_rows(sample, delimiter) if row)
        fields, hits = max(counts.items(), key=lambda kv: (kv[1] * (kv[0] - 1), kv[0]))
        score = hits * (fields - 1)
        if score > best_score:
            best, best_score = delimiter, score
    return best


def parse_csv_table(text: str) -> dict[str, Any]:
    """Split a CSV export into header, units row, data rows and the text around the table.

    The layout rules live in table.detect_table; this adds delimiter detection.
    """
    delimiter = sniff_delimiter(text)
    return {"delimiter": delimiter, **_table_dict(detect_table(_read_rows(text, delimiter)))}


def _table_dict(table: Table) -> dict[str, Any]:
    return {
        "header": table.header,
        "units": table.units,
        "body": table.body,
        "footer": table.footer,
        "leading_text": table.leading_text,
        "header_note": table.header_note,
    }


def table_extras(table: dict[str, Any]) -> dict[str, Any]:
    """The parts of a parsed table that are context rather than rows, for tool output."""
    out: dict[str, Any] = {}
    if table.get("header_note"):
        out["header_note"] = table["header_note"]
    if table.get("leading_text"):
        out["leading_text"] = table["leading_text"]  # title lines above the table
    if table.get("units"):
        out["units"] = table["units"]
    if table.get("footer"):
        out["trailing_text"] = table["footer"]  # often a legend of column codes; not data rows
    return out


def preview_csv(text: str, max_rows: int, truncated: bool) -> dict[str, Any]:
    text = _drop_partial_last_line(text, truncated)
    table = parse_csv_table(text)
    header, body = table["header"], table["body"]
    if not header:
        return {"kind": "csv", "columns": [], "rows": [], "note": "empty file"}
    return {
        "kind": "csv",
        "delimiter": table["delimiter"],
        "columns": header,
        "rows": [dict(zip(header, r)) for r in body[:max_rows]],
        "rows_in_sample": len(body),
        "sample_truncated": truncated,
        **table_extras(table),
    }


def parse_spreadsheet(data: bytes, sheet: str | None = None) -> dict[str, Any]:
    """The table on one sheet of a workbook, plus the list of sheets so another can be asked for.

    Raises SpreadsheetError with a message fit for the model.
    """
    sheets = read_workbook(data)
    index = pick_sheet(sheets, sheet)
    name, grid = sheets[index]
    out = _table_dict(detect_table(grid))
    out["sheet"] = name
    if len(sheets) > 1:
        out["sheets"] = [{"name": n, "rows": len(g)} for n, g in sheets]
    return out


def sheet_extras(table: dict[str, Any]) -> dict[str, Any]:
    return {k: table[k] for k in ("sheet", "sheets") if k in table}


def preview_spreadsheet(data: bytes, max_rows: int, truncated: bool, sheet: str | None) -> dict[str, Any]:
    if truncated:
        return {"kind": "spreadsheet", "note": "the workbook is larger than the preview limit; analyze_resource reads it whole"}
    table = parse_spreadsheet(data, sheet)
    header, body = table["header"], table["body"]
    return {
        "kind": "spreadsheet",
        **sheet_extras(table),
        "columns": header,
        "rows": [dict(zip(header, r)) for r in body[:max_rows]],
        "rows_in_sample": len(body),
        "sample_truncated": False,
        **table_extras(table),
        **({} if header else {"note": "this sheet is empty; pass sheet= to read another one"}),
    }


TEXT_FORMATS = {"csv", "tsv", "txt", "json", "geojson"}
_MEMBER_EXTENSIONS = {"csv": (".csv", ".txt", ".tsv"), "tsv": (".tsv", ".txt", ".csv"), "txt": (".txt", ".csv"),
                      "json": (".json", ".geojson"), "geojson": (".geojson", ".json")}


class ArchiveError(ValueError):
    """A ZIP file could not be opened, or what it holds is too large."""


def unwrap_zip(data: bytes, fmt: str, truncated: bool, max_out: int, strict: bool = False) -> tuple[bytes, bool, dict[str, Any]]:
    """Some publishers upload a ZIP and label it CSV. Return the data file inside.

    Gives (data, truncated, info). A complete archive is opened properly and the member that
    fits the declared format is chosen. A partial download has no central directory, so only
    its first member can be read, straight from the local file header. ``strict`` refuses to
    return a cut-off member (exact analysis needs the whole file).
    """
    if not data.startswith(b"PK\x03\x04") or fmt not in TEXT_FORMATS:
        return data, truncated, {}
    if truncated:
        if strict:
            raise ArchiveError("the ZIP archive was not downloaded completely")
        method, name_len, extra_len = struct.unpack("<H", data[8:10])[0], *struct.unpack("<HH", data[26:30])
        name = data[30 : 30 + name_len].decode("cp437", errors="replace")
        payload = data[30 + name_len + extra_len :]
        if method == 8:
            try:
                payload = zlib.decompressobj(-15).decompress(payload, max_out)
            except zlib.error as exc:
                raise ArchiveError(f"cannot read the ZIP archive: {exc}") from exc
        elif method != 0:
            raise ArchiveError("the ZIP archive uses a compression method that is not supported")
        return payload[:max_out], True, {"archive_member": name}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            if not members:
                raise ArchiveError("the ZIP archive is empty")
            wanted = _MEMBER_EXTENSIONS.get(fmt, ())
            member = next((m for ext in wanted for m in members if m.filename.lower().endswith(ext)), members[0])
            if strict and member.file_size > max_out:
                raise ArchiveError(f"'{member.filename}' in the ZIP archive unpacks to {member.file_size / 1048576:.0f} MB, over the limit")
            with zf.open(member) as fh:
                payload = fh.read(max_out + 1)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError) as exc:
        raise ArchiveError(f"cannot read the ZIP archive: {exc}") from exc
    info: dict[str, Any] = {"archive_member": member.filename}
    if len(members) > 1:
        info["archive_other_members"] = [m.filename for m in members if m is not member][:20]
    return payload[:max_out], len(payload) > max_out, info


DESCRIPTOR_NOTE = (
    "This JSON file is not data. It is a descriptor (Frictionless Data Resource) for the file at "
    "describes_url; preview or analyse that resource instead."
)


def read_descriptor(doc: Any) -> dict[str, Any] | None:
    """Recognise a Frictionless Data Resource descriptor.

    Most JSON resources on the portal are these: a few hundred bytes naming the sibling CSV
    ("path") with its dialect and schema. They are metadata, and treating them as data only
    produces confusing errors.
    """
    if not isinstance(doc, dict):
        return None
    path = doc.get("path")
    if not isinstance(path, str) or not path.startswith(("http://", "https://")):
        return None
    if not {"schema", "dialect", "profile", "mediatype", "format", "encoding"} & set(doc):
        return None
    out: dict[str, Any] = {"describes_url": path}
    schema = doc.get("schema")
    if isinstance(schema, str):
        out["schema_url"] = schema
    elif isinstance(schema, dict) and isinstance(schema.get("fields"), list):
        out["fields"] = [{"name": f.get("name"), "type": f.get("type")} for f in schema["fields"] if isinstance(f, dict)][:200]
    return out


def preview_json(text: str, max_rows: int, truncated: bool) -> dict[str, Any]:
    if truncated:
        return {"kind": "json", "note": "file larger than the preview limit; raw head shown", "head": text[:4000]}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"kind": "json", "note": f"invalid JSON: {exc}", "head": text[:4000]}
    if descriptor := read_descriptor(data):
        return {"kind": "descriptor", "note": DESCRIPTOR_NOTE, **descriptor}
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


def build_preview(
    data: bytes, fmt: str, content_type: str, max_rows: int, truncated: bool, sheet: str | None = None
) -> dict[str, Any]:
    fmt = (fmt or "").lower()
    ctype = (content_type or "").lower()
    if is_spreadsheet(fmt, ctype):
        return preview_spreadsheet(data, max_rows, truncated, sheet)
    try:
        data, truncated, archive = unwrap_zip(data, fmt, truncated, PREVIEW_UNPACK_BYTES)
    except ArchiveError as exc:
        return {"kind": "binary", "note": str(exc), "bytes_read": len(data)}
    if archive:
        ctype = ""  # the content type described the archive, not the file inside
        return {**_build(data, fmt, ctype, max_rows, truncated), **archive}
    return _build(data, fmt, ctype, max_rows, truncated)


PREVIEW_UNPACK_BYTES = 2 * 1024 * 1024


def _build(data: bytes, fmt: str, ctype: str, max_rows: int, truncated: bool) -> dict[str, Any]:
    if fmt in {"csv", "tsv", "txt"} or "csv" in ctype:
        if b"\x00" in data[:4096] and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
            return {"kind": "binary", "note": f"labelled {fmt or 'csv'} but the content is binary, not text", "bytes_read": len(data)}
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
