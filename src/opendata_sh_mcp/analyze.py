"""Exact aggregations over a whole downloaded file: filter, group and compute metrics in Python."""

from __future__ import annotations

import json
import math
import re
import statistics
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .ckan import PortalError
from .preview import ArchiveError, decode, parse_csv_table, parse_spreadsheet, read_descriptor, sheet_extras, table_extras, unwrap_zip
from .spreadsheet import SpreadsheetError, is_spreadsheet

FilterOp = Literal["eq", "ne", "gt", "ge", "lt", "le", "contains", "in", "empty", "not_empty"]
Agg = Literal["count", "count_distinct", "sum", "mean", "median", "min", "max"]


class DescriptorError(PortalError):
    """The file is a descriptor pointing at the real data file, whose URL is in ``url``."""

    def __init__(self, url: str) -> None:
        super().__init__(f"this JSON file is a descriptor for {url}, not data; analyse that resource instead")
        self.url = url


class RowFilter(BaseModel):
    column: str
    op: FilterOp = "eq"
    value: str | float | list[str | float] | None = Field(
        default=None, description="Compared as a number when both sides parse as numbers, else as text"
    )


class Metric(BaseModel):
    agg: Agg
    column: str | None = Field(default=None, description="Not needed for count")

    @property
    def label(self) -> str:
        return f"{self.agg}({self.column or '*'})"


_SPACES = re.compile(r"[\s\u00a0\u202f\u2009]")
_THOUSANDS_DOT = re.compile(r"^[+-]?\d{1,3}(\.\d{3})+$")
_NUMBER = re.compile(r"^[+-]?(\d+([.,]\d*)?|[.,]\d+)([eE][+-]?\d+)?$")


def to_number(value: Any, dot_decimal: bool = False) -> float | None:
    """Parse numbers as German exports write them: 1.234,5 / 1234,5 / 1.234 (thousands) / 1234.5.

    With dot_decimal, a lone dot is always a decimal point (1.234 == 1.234).
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = _SPACES.sub("", str(value)).replace("\u2212", "-")
    if text.endswith("%"):
        text = text[:-1]
    if not text:
        return None
    if "," in text and "." in text:
        # whichever comes last is the decimal separator
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif not dot_decimal and _THOUSANDS_DOT.match(text):
        text = text.replace(".", "")
    elif text.count(",") == 1:
        text = text.replace(",", ".")
    if not _NUMBER.match(text):
        return None
    try:
        num = float(text)
    except ValueError:
        return None
    return num if math.isfinite(num) else None


class NumberStyles:
    """Decides once per column, over the whole file, whether a lone dot is a decimal point.

    "1.234" is 1234 in a German export and 1.234 in an English one. A column that has dotted
    values which cannot be thousands groups (1.5, 12.34) and no commas uses decimal points.
    The decision must not depend on which rows a filter or group happens to select, or the
    same cell would count as 1234 in one group and 1.234 in another.
    """

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self._dot_decimal: dict[str, bool] = {}

    def dot_decimal(self, column: str | None) -> bool:
        if column is None:
            return False
        if column not in self._dot_decimal:
            texts = [v.strip() for r in self._records if isinstance(v := r.get(column), str)]
            self._dot_decimal[column] = not any("," in t for t in texts) and any(
                "." in t and not _THOUSANDS_DOT.match(t) and _NUMBER.match(t) for t in texts
            )
        return self._dot_decimal[column]

    def number(self, value: Any, column: str | None) -> float | None:
        return to_number(value, self.dot_decimal(column))


_GERMAN_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")


def to_date(value: Any) -> datetime | None:
    """Parse 29.09.2026, 29.09.2026 13:00(:00) and ISO dates; None for anything else."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    m = _GERMAN_DATE.match(text)
    try:
        if m:
            d, mo, y, h, mi, sec = m.groups()
            return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0))
        if re.match(r"^\d{4}-\d{2}-\d{2}", text):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
    return None


def load_records(
    data: bytes,
    fmt: str,
    content_type: str,
    info: dict[str, Any] | None = None,
    max_unpacked: int = 200 * 1024 * 1024,
    sheet: str | None = None,
) -> list[dict[str, Any]]:
    """Turn a whole CSV, Excel sheet, JSON array of objects or GeoJSON file into a list of flat records.

    For CSV, title lines, a units row and any trailing section are left out of the records and
    put in info. A ZIP labelled as one of these formats is opened (up to max_unpacked bytes).
    """
    fmt = (fmt or "").lower()
    ctype = (content_type or "").lower()
    if is_spreadsheet(fmt, ctype):
        try:
            table = parse_spreadsheet(data, sheet)
        except SpreadsheetError as exc:
            raise PortalError(str(exc)) from exc
        if info is not None:
            info.update(sheet_extras(table))
            info.update(table_extras(table))
        return [dict(zip(table["header"], r)) for r in table["body"]]
    try:
        data, _, archive = unwrap_zip(data, fmt, False, max_unpacked, strict=True)
    except ArchiveError as exc:
        raise PortalError(str(exc)) from exc
    if archive:
        ctype = ""
        if info is not None:
            info.update(archive)
    if fmt in {"csv", "tsv", "txt"} or "csv" in ctype:
        if b"\x00" in data[:4096] and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
            raise PortalError(f"the file is labelled {fmt or 'csv'} but its content is binary, not text")
        table = parse_csv_table(decode(data))
        if info is not None:
            info.update(table_extras(table))
        return [dict(zip(table["header"], r)) for r in table["body"]]
    if fmt in {"json", "geojson"} or "json" in ctype:
        try:
            doc = json.loads(decode(data))
        except json.JSONDecodeError as exc:
            raise PortalError(f"file is not valid JSON: {exc}") from exc
        if descriptor := read_descriptor(doc):
            raise DescriptorError(descriptor["describes_url"])
        if isinstance(doc, dict) and doc.get("type") == "FeatureCollection":
            return [dict(f.get("properties") or {}) for f in doc.get("features") or []]
        if isinstance(doc, dict):
            # a single top-level list of objects, e.g. {"data": [...]}
            lists = [v for v in doc.values() if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)]
            if len(lists) == 1:
                doc = lists[0]
        if isinstance(doc, list) and all(isinstance(i, dict) for i in doc):
            return doc
        if isinstance(doc, dict) and len(doc) >= 2 and all(isinstance(v, dict) for v in doc.values()):
            # records keyed by an id, e.g. {"010580005005": {"amount_t": 189, ...}, ...}
            return [{"key": k, **v} for k, v in doc.items()]
        raise PortalError("JSON file is not a list of records or a GeoJSON FeatureCollection; use preview_resource to inspect it")
    raise PortalError(f"format '{fmt or ctype}' cannot be analysed; supported are CSV, Excel, JSON records and GeoJSON")


def _cell(row: dict[str, Any], column: str) -> Any:
    value = row.get(column)
    return value.strip() if isinstance(value, str) else value


def _matches(row: dict[str, Any], f: RowFilter, styles: NumberStyles) -> bool:
    cell = _cell(row, f.column)
    empty = cell is None or cell == ""
    if f.op == "empty":
        return empty
    if f.op == "not_empty":
        return not empty
    if f.op == "in":
        values = f.value if isinstance(f.value, list) else [f.value]
        return any(_equal(cell, v, styles, f.column) for v in values)
    if f.op == "eq":
        return _equal(cell, f.value, styles, f.column)
    if f.op == "ne":
        return not _equal(cell, f.value, styles, f.column)
    if f.op == "contains":
        return str(f.value).lower() in str(cell or "").lower()
    a, b = styles.number(cell, f.column), styles.number(f.value, f.column)
    if a is None or b is None:
        if empty:
            return False
        a, b = to_date(cell), to_date(f.value)
        if a is None or b is None:
            a, b = str(cell), str(f.value)
    return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[f.op]


def _equal(cell: Any, value: Any, styles: NumberStyles, column: str) -> bool:
    a, b = styles.number(cell, column), styles.number(value, column)
    if a is not None and b is not None:
        return a == b
    return str(cell if cell is not None else "").strip().lower() == str(value if value is not None else "").strip().lower()


def _round(x: float) -> float | int:
    x = round(x, 10)
    return int(x) if x.is_integer() and abs(x) < 2**53 else x


def _compute(metric: Metric, rows: list[dict[str, Any]], unparsed: dict[str, int], styles: NumberStyles) -> Any:
    if metric.agg == "count":
        if metric.column is None:
            return len(rows)
        return sum(1 for r in rows if _cell(r, metric.column) not in (None, ""))
    values = [_cell(r, metric.column) for r in rows]
    values = [v for v in values if v not in (None, "")]
    if metric.agg == "count_distinct":
        return len({str(v) for v in values})
    nums = []
    for v in values:
        n = styles.number(v, metric.column)
        if n is None:
            unparsed[metric.label] = unparsed.get(metric.label, 0) + 1
        else:
            nums.append(n)
    if metric.agg in ("min", "max") and values and len(nums) < len(values):
        # a date column (German or ISO): min/max give the earliest/latest date
        dates = [to_date(v) for v in values]
        if all(dates):
            unparsed.pop(metric.label, None)
            return (min(dates) if metric.agg == "min" else max(dates)).isoformat()
    if not nums:
        return None
    if metric.agg == "sum":
        return _round(math.fsum(nums))
    if metric.agg == "mean":
        return _round(math.fsum(nums) / len(nums))
    if metric.agg == "median":
        return _round(statistics.median(nums))
    return _round(min(nums) if metric.agg == "min" else max(nums))


def analyze(
    records: list[dict[str, Any]],
    filters: Iterable[RowFilter] = (),
    group_by: list[str] | None = None,
    metrics: list[Metric] | None = None,
    sort: Literal["value_desc", "value_asc", "key"] = "value_desc",
    max_groups: int = 50,
) -> dict[str, Any]:
    filters = list(filters)
    group_by = group_by or []
    metrics = metrics or [Metric(agg="count")]
    columns = list(dict.fromkeys(k for r in records[:1000] for k in r))
    wanted = [f.column for f in filters] + group_by + [m.column for m in metrics if m.column]
    missing = [c for c in dict.fromkeys(wanted) if c not in columns]
    if missing:
        raise PortalError(f"unknown column(s) {missing}; available columns: {columns}")
    for m in metrics:
        if m.agg != "count" and not m.column:
            raise PortalError(f"{m.agg} needs a column")

    styles = NumberStyles(records)
    matched = [r for r in records if all(_matches(r, f, styles) for f in filters)]
    unparsed: dict[str, int] = {}
    out: dict[str, Any] = {
        "rows_total": len(records),
        "rows_matched": len(matched),
        "columns": columns,
    }
    if not group_by:
        out["result"] = {m.label: _compute(m, matched, unparsed, styles) for m in metrics}
    else:
        buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for r in matched:
            key = tuple(str(_cell(r, c) if _cell(r, c) is not None else "") for c in group_by)
            buckets.setdefault(key, []).append(r)
        groups = [
            {"key": dict(zip(group_by, key)), **{m.label: _compute(m, rows, unparsed, styles) for m in metrics}}
            for key, rows in buckets.items()
        ]
        first = metrics[0].label
        if sort == "key":
            groups.sort(key=lambda g: tuple(g["key"].values()))
        else:
            present = [g for g in groups if g[first] is not None]
            present.sort(key=lambda g: g[first], reverse=sort == "value_desc")
            groups = present + [g for g in groups if g[first] is None]
        out["group_count"] = len(groups)
        out["groups"] = groups[:max_groups]
        if len(groups) > max_groups:
            out["note"] = f"showing {max_groups} of {len(groups)} groups; raise max_groups or add filters for the rest"
    if unparsed:
        out["values_not_numeric"] = unparsed  # skipped in sum/mean/min/max/median
    return out
