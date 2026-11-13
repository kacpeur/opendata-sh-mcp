"""Read Excel workbooks (.xlsx, .xls) into plain rows, one grid per sheet.

About 4,200 datasets on the portal offer Excel files, often as the only tabular format.
Cells keep their type: numbers stay numbers (so there is no decimal-separator guessing),
dates become ISO strings, everything else is text. Finding the table inside a sheet is
left to table.detect_table, the same code that handles CSV layouts.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, time
from typing import Any

XLSX_MAGIC = b"PK\x03\x04"
XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
SPREADSHEET_FORMATS = {"xlsx", "xlsm", "xls"}
# A workbook is a ZIP of XML; refuse ones that unpack to something absurd.
MAX_UNPACKED_BYTES = 400 * 1024 * 1024
MAX_CELLS = 5_000_000

Grid = list[list[Any]]


class SpreadsheetError(ValueError):
    """The workbook cannot be read; the message is safe to show to the model."""


def is_spreadsheet(fmt: str, content_type: str, data: bytes = b"") -> bool:
    ctype = (content_type or "").lower()
    if (fmt or "").lower() in SPREADSHEET_FORMATS:
        return True
    return "spreadsheetml" in ctype or "ms-excel" in ctype


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return int(value) if value.is_integer() and abs(value) < 2**53 else value
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value).strip()


def _trim(grid: Grid) -> Grid:
    """Drop the empty rows a sheet's declared dimensions often add at the end."""
    while grid and all(c == "" for c in grid[-1]):
        grid.pop()
    return grid


def _read_xlsx(data: bytes) -> list[tuple[str, Grid]]:
    import openpyxl

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if "[Content_Types].xml" not in names:
                inner = [n for n in names if n.lower().endswith((".xlsx", ".xlsm", ".xls"))]
                if len(inner) == 1 and zf.getinfo(inner[0]).file_size <= MAX_UNPACKED_BYTES:
                    return read_workbook(zf.read(inner[0]))  # a workbook zipped together with its documentation
                listing = ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
                raise SpreadsheetError(f"the file is labelled as Excel but is a ZIP archive holding: {listing}")
            if sum(m.file_size for m in zf.infolist()) > MAX_UNPACKED_BYTES:
                raise SpreadsheetError("the workbook is too large to read")
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except SpreadsheetError:
        raise
    except Exception as exc:  # openpyxl raises many types for damaged files
        raise SpreadsheetError(f"cannot read the Excel file: {type(exc).__name__}: {exc}") from exc
    sheets: list[tuple[str, Grid]] = []
    cells = 0
    try:
        for ws in wb.worksheets:
            if getattr(ws, "sheet_state", "visible") != "visible":
                continue
            grid: Grid = []
            for row in ws.iter_rows(values_only=True):
                cells += len(row)
                if cells > MAX_CELLS:
                    raise SpreadsheetError(f"the workbook has more than {MAX_CELLS:,} cells and is too large to read")
                grid.append([_cell(v) for v in row])
            sheets.append((ws.title, _trim(grid)))
    finally:
        wb.close()
    return sheets


def _read_xls(data: bytes) -> list[tuple[str, Grid]]:
    import xlrd

    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        raise SpreadsheetError(f"cannot read the Excel file: {type(exc).__name__}: {exc}") from exc
    sheets: list[tuple[str, Grid]] = []
    for sh in book.sheets():
        if sh.nrows * sh.ncols > MAX_CELLS:
            raise SpreadsheetError(f"the workbook has more than {MAX_CELLS:,} cells and is too large to read")
        grid: Grid = []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                value: Any = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        value = xlrd.xldate_as_datetime(value, book.datemode)
                    except Exception:
                        pass
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    value = bool(value)
                elif cell.ctype == xlrd.XL_CELL_ERROR:
                    value = None
                row.append(_cell(value))
            grid.append(row)
        sheets.append((sh.name, _trim(grid)))
    return sheets


def read_workbook(data: bytes) -> list[tuple[str, Grid]]:
    """All visible sheets as (name, rows). The reader is chosen by content, not by the label."""
    if data.startswith(XLSX_MAGIC):
        return _read_xlsx(data)
    if data.startswith(XLS_MAGIC):
        return _read_xls(data)
    raise SpreadsheetError("the file is labelled as Excel but is neither an .xlsx nor an .xls workbook")


def pick_sheet(sheets: list[tuple[str, Grid]], wanted: str | None) -> int:
    """Index of the requested sheet (name, or 1-based position), or of the first sheet that
    looks like a table when none is requested."""
    if not sheets:
        raise SpreadsheetError("the workbook has no visible sheets")
    names = [name for name, _ in sheets]
    if wanted is not None and str(wanted).strip():
        text = str(wanted).strip()
        for i, name in enumerate(names):
            if name.strip().lower() == text.lower():
                return i
        if text.isdigit() and 1 <= int(text) <= len(sheets):
            return int(text) - 1
        raise SpreadsheetError(f"no sheet named '{text}'; sheets are: {names}")
    # Report workbooks open with a cover page and an imprint; prefer the first sheet with
    # several rows of three or more filled cells, then settle for anything two cells wide.
    for min_cells, min_rows in ((3, 3), (2, 2)):
        for i, (_, grid) in enumerate(sheets):
            if sum(1 for r in grid if sum(1 for c in r if c != "") >= min_cells) >= min_rows:
                return i
    return 0
