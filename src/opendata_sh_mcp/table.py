"""Find the actual table inside a grid of cells.

Files on the portal are rarely a header line followed by data. Statistikamt Nord puts title
lines and a two-row header on top, KFÜ adds a units row and a station legend, some exports
have no header at all. ``detect_table`` takes raw rows (from a CSV or a spreadsheet sheet)
and separates column names, units, data rows and the text around them, so that previews and
exact analysis see the same clean table.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_NUMERIC_CELL = re.compile(r"^[+-]?[\d.,]*\d[\d.,]*([eE][+-]?\d+)?\s*%?$")
# Placeholders statistical offices write where a number is missing or secret.
_MISSING_MARKERS = {".", "-", "–", "—", "x", "X", "/", "...", "…", "k.A.", "k. A.", "n/a", "NA", "*"}
_SAMPLE_ROWS = 500
MAX_TEXT = 4000


@dataclass
class Table:
    header: list[str] = field(default_factory=list)
    body: list[list[str]] = field(default_factory=list)
    units: dict[str, str] | None = None
    leading_text: str | None = None  # title lines above the table
    footer: str | None = None  # legend or notes below it
    header_note: str | None = None  # how the column names were derived, when not from one plain row


def _blank(row: list[str]) -> bool:
    return not any(cell.strip() for cell in row)


def _is_numeric(cell: str) -> bool:
    return bool(_NUMERIC_CELL.match(cell.strip()))


def _width(row: list[str]) -> int:
    """Number of cells up to the last non-empty one."""
    n = len(row)
    while n and not row[n - 1].strip():
        n -= 1
    return n


def _as_text(rows: list[list[str]]) -> str | None:
    lines = [" | ".join(c.strip() for c in r[: _width(r)] if c.strip()) for r in rows if not _blank(r)]
    return "\n".join(lines)[:MAX_TEXT] or None


def _unique(names: list[str]) -> list[str]:
    """Column names must be distinct and non-empty, or rows lose cells when turned into records."""
    seen: Counter[str] = Counter()
    out = []
    for i, name in enumerate(names, 1):
        name = " ".join(name.split()) or f"col_{i}"
        seen[name] += 1
        out.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return out


def _numeric_columns(rows: list[list[str]], width: int) -> set[int]:
    """Columns whose filled cells are numbers in at least 80% of the sampled rows."""
    cols = set()
    for c in range(width):
        cells = [r[c].strip() for r in rows if c < len(r) and r[c].strip()]
        if cells and sum(_is_numeric(x) for x in cells) >= 0.8 * len(cells):
            cols.add(c)
    return cols


def _label_row(row: list[str], numeric_cols: set[int]) -> bool:
    """A row of labels (second header line or units), not data: no numbers anywhere, and text
    rather than a missing-value marker in at least one column that otherwise holds numbers."""
    cells = [c.strip() for c in row]
    if any(_is_numeric(c) for c in cells if c):
        return False
    in_numeric = [cells[c] for c in numeric_cols if c < len(cells) and cells[c]]
    return bool(in_numeric) and not any(c in _MISSING_MARKERS for c in in_numeric)


def _forward_fill(row: list[str], width: int) -> list[str]:
    out, last = [], ""
    for c in range(width):
        cell = row[c].strip() if c < len(row) else ""
        last = cell or last
        out.append(last)
    return out


def detect_table(rows: list[list[str]]) -> Table:
    """Split raw rows into title lines, column names, units, data and trailing notes."""
    filled = [(i, r) for i, r in enumerate(rows) if not _blank(r)]
    if not filled:
        return Table()
    widths = Counter(_width(r) for _, r in filled[:_SAMPLE_ROWS])
    width = max(widths, key=lambda w: (widths[w], w))  # most common, the wider one on a tie
    width = max(width, 1)

    # Title lines: rows much narrower than the table, before its first full-width row.
    start = next((i for i, r in filled if _width(r) > max(1, width // 2)), filled[0][0]) if width >= 2 else filled[0][0]
    table = Table(leading_text=_as_text(rows[:start]))

    # Trailing section: after a blank line, a row much narrower than the table starts a legend.
    end = len(rows)
    saw_blank = False
    for i in range(start + 1, len(rows)):
        if _blank(rows[i]):
            saw_blank = True
            continue
        if saw_blank and width >= 2 and _width(rows[i]) < width / 2:
            end = i
            table.footer = _as_text(rows[i:])
            break
        saw_blank = False

    block = [r for r in rows[start:end] if not _blank(r)]
    width = max(width, max(_width(r) for r in block))  # never cut a cell off a wider row
    sample = block[1 : 1 + _SAMPLE_ROWS]
    numeric_cols = _numeric_columns(sample[2:] or sample, width)
    first = [c.strip() for c in block[0]]

    # No header at all: the first row has numbers where the data has numbers, and one of its
    # texts shows up again further down the same column (a column name would not).
    first_numeric_ok = bool(numeric_cols) and all(c < len(first) and _is_numeric(first[c]) for c in numeric_cols)
    repeats = any(
        cell and not _is_numeric(cell) and any(c < len(r) and r[c].strip() == cell for r in sample)
        for c, cell in enumerate(first)
    )
    all_numeric = bool(first) and all(_is_numeric(c) for c in first if c) and len(numeric_cols) == width
    if first_numeric_ok and sample and (repeats or all_numeric):
        table.header = [f"col_{i}" for i in range(1, width + 1)]
        table.header_note = "the file has no header row; columns are named col_1, col_2, ..."
        table.body = [_pad(r, width) for r in block]
        return table

    names = _pad(first, width)
    skip = 1
    if len(block) >= 3 and _label_row(block[1], numeric_cols) and not _label_row(block[2], numeric_cols):
        second = _pad([c.strip() for c in block[1]], width)
        if "" in names and "" not in second:
            # Group labels over sub-headers: "0-17 Jahre | (blank)" above "Anzahl | %".
            groups = _forward_fill(names, width)
            names = [f"{g} {s}".strip() if g != s else s for g, s in zip(groups, second)]
            table.header_note = "column names combine two header rows (group label, then sub-header)"
        else:
            table.units = {h: u for h, u in zip(_unique(names), second) if u} or None
        skip = 2
    table.header = _unique(names)
    table.body = [_pad(r, width) for r in block[skip:]]
    return table


def _pad(row: list[str], width: int) -> list[str]:
    cells = [c.strip() for c in row[:width]]
    return cells + [""] * (width - len(cells))
