"""Excel support: workbooks are built in memory, shaped like files on the portal."""

from __future__ import annotations

import io
from datetime import date, datetime

import httpx
import openpyxl
import pytest
import xlwt

from opendata_sh_mcp import server
from opendata_sh_mcp.analyze import Metric, RowFilter, analyze, load_records
from opendata_sh_mcp.ckan import PortalError
from opendata_sh_mcp.preview import build_preview
from opendata_sh_mcp.spreadsheet import SpreadsheetError, pick_sheet, read_workbook

from .conftest import API, BASE, KIEL_PACKAGE, ok

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx(sheets: dict[str, list[list]], hidden: tuple[str, ...] = ()) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
        if name in hidden:
            ws.sheet_state = "hidden"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xls(rows: list[list]) -> bytes:
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Daten")
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            ws.write(r, c, value)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# Haushalt: plain header, text codes with leading zeros, amounts as real numbers.
HAUSHALT = [
    ["EP", "Kapitel", "Zweckbestimmung", "Ist 2019 T€", "Ansatz 2021 T€"],
    ["01", "0101", "Kostenbeiträge", 0, 0],
    ["01", "0101", "Erlöse", 0.3, 1.234],
    ["03", "0301", "Einnahmen", 20.4, 20],
]

# Norderstedt Einwohnerzahlen: title, merged group cells over m/w, a blank row before the data.
EINWOHNER = [
    ["Anzahl Einwohner", None, None, None, None, None, "Quelle: Melderegister"],  # wide, but still a title row
    [None] * 7,
    ["Alter", "gesamte Stadt", None, "Friedrichsgabe", None, "Harksheide", None],
    [None, "m", "w", "m", "w", "m", "w"],
    [None] * 7,
    [0, 305, 286, 37, 34, 85, 95],
    [1, 307, 322, 28, 40, 92, 98],
    [2, 290, 301, 31, 29, 100, 72],
]


class TestReadWorkbook:
    def test_cells_keep_their_type(self):
        data = xlsx({"A": [["Datum", "Zeit", "Wert", "Ganz", "Text", "Leer"], [date(2020, 12, 31), datetime(2026, 9, 29, 13, 0), 525.03, 7.0, " x ", None]]})
        [(name, grid)] = read_workbook(data)
        assert name == "A"
        assert grid[1] == ["2020-12-31", "2026-09-29 13:00:00", 525.03, 7, "x", ""]

    def test_hidden_sheets_and_trailing_empty_rows_are_dropped(self):
        data = xlsx({"Intern": [["x", 1]], "Daten": [["a", "b"], [1, 2], [None, None]]}, hidden=("Intern",))
        assert read_workbook(data) == [("Daten", [["a", "b"], [1, 2]])]

    def test_old_xls_format(self):
        [(name, grid)] = read_workbook(xls([["Ort", "Einwohner"], ["Kiel", 247717.0], ["Husum", 23.5]]))
        assert (name, grid) == ("Daten", [["Ort", "Einwohner"], ["Kiel", 247717], ["Husum", 23.5]])

    def test_reader_follows_content_not_label(self):
        out = build_preview(xls([["a", "b"], [1, 2], [3, 4]]), "XLSX", XLSX_TYPE, 5, False)
        assert out["rows"] == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]

    def test_damaged_or_mislabelled_file(self):
        with pytest.raises(SpreadsheetError, match="cannot read the Excel file"):
            read_workbook(b"PK\x03\x04" + b"\x00" * 100)
        with pytest.raises(SpreadsheetError, match="neither"):
            read_workbook(b"<html>Fehler</html>")


    def test_workbook_zipped_with_documentation(self):
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("BK50_Moore.xlsx", xlsx({"Moore": [["a", "b"], [1, 2]]}))
            zf.writestr("Doku.pdf", b"%PDF")
        assert read_workbook(buf.getvalue()) == [("Moore", [["a", "b"], [1, 2]])]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.shp", b"x")
        with pytest.raises(SpreadsheetError, match="ZIP archive holding: a.shp"):
            read_workbook(buf.getvalue())


class TestPickSheet:
    SHEETS = [("Deckblatt", [["Statistischer Bericht"], [""], ["G I 1"]]), ("Tab. 1", [["a", "b"], [1, 2]]), ("Impressum", [["x"]])]

    def test_default_skips_cover_pages(self):
        assert pick_sheet(self.SHEETS, None) == 1

    def test_default_prefers_a_real_table_over_an_imprint(self):
        imprint = ("Impressum", [["Telefon:", "040 1"], ["E-Mail:", "a@b"], ["Internet:", "x"]])
        table = ("Tab. 1", [["Jahr", "Umsatz", "Beschäftigte"], [2015, 100, 100], [2016, 101.6, 101.6]])
        assert pick_sheet([imprint, table], None) == 1

    def test_by_name_case_insensitive_and_by_position(self):
        assert pick_sheet(self.SHEETS, "impressum") == 2
        assert pick_sheet(self.SHEETS, "1") == 0

    def test_unknown_sheet_lists_the_names(self):
        with pytest.raises(SpreadsheetError, match=r"sheets are: \['Deckblatt', 'Tab. 1', 'Impressum'\]"):
            pick_sheet(self.SHEETS, "Tabelle9")


class TestPreviewAndAnalyze:
    def test_preview(self):
        out = build_preview(xlsx({"Alle": HAUSHALT, "Hinweise": [["Stand 2021"]]}), "XLSX", XLSX_TYPE, 2, False)
        assert out["kind"] == "spreadsheet"
        assert out["sheet"] == "Alle"
        assert out["sheets"] == [{"name": "Alle", "rows": 4}, {"name": "Hinweise", "rows": 1}]
        assert out["columns"] == HAUSHALT[0]
        assert out["rows"][1] == {"EP": "01", "Kapitel": "0101", "Zweckbestimmung": "Erlöse", "Ist 2019 T€": 0.3, "Ansatz 2021 T€": 1.234}
        assert out["rows_in_sample"] == 3

    def test_partial_download_is_not_parsed(self):
        out = build_preview(xlsx({"Alle": HAUSHALT})[:500], "XLSX", XLSX_TYPE, 2, True)
        assert out["kind"] == "spreadsheet"
        assert "analyze_resource" in out["note"]

    def test_real_numbers_are_never_reinterpreted(self):
        # 1.234 is a float in the file; as CSV text it would be read as German 1234.
        rows = load_records(xlsx({"Alle": HAUSHALT}), "xlsx", "")
        out = analyze(rows, group_by=["EP"], metrics=[Metric(agg="sum", column="Ansatz 2021 T€")], sort="key")
        assert [g["sum(Ansatz 2021 T€)"] for g in out["groups"]] == [1.234, 20]
        assert analyze(rows, filters=[RowFilter(column="Kapitel", op="eq", value="0101")])["rows_matched"] == 2

    def test_merged_group_header_with_title(self):
        info: dict = {}
        rows = load_records(xlsx({"Tabelle1": EINWOHNER}), "xlsx", "", info)
        assert list(rows[0]) == ["Alter", "gesamte Stadt m", "gesamte Stadt w", "Friedrichsgabe m", "Friedrichsgabe w", "Harksheide m", "Harksheide w"]
        assert len(rows) == 3
        assert info["leading_text"] == "Anzahl Einwohner | Quelle: Melderegister"
        assert info["sheet"] == "Tabelle1"
        assert analyze(rows, metrics=[Metric(agg="sum", column="gesamte Stadt w")])["result"] == {"sum(gesamte Stadt w)": 909}

    def test_choose_sheet(self):
        data = xlsx({"Alle": HAUSHALT, "Zweite": [["k", "v"], ["a", 1], ["b", 2]]})
        assert load_records(data, "xlsx", "", sheet="Zweite") == [{"k": "a", "v": 1}, {"k": "b", "v": 2}]
        with pytest.raises(PortalError, match="no sheet named"):
            load_records(data, "xlsx", "", sheet="Dritte")


XLSX_RESOURCE = {
    "id": "aaaa1111-0000-4000-8000-00000000e4ce",
    "package_id": KIEL_PACKAGE["id"],
    "name": "Titelliste 2021",
    "format": "http://publications.europa.eu/resource/authority/file-type/XLSX",
    "url": f"{BASE}/data/fm/Titelliste_2021.xlsx",
    "last_modified": "2021-03-01T00:00:00",
}


class TestTools:
    @pytest.fixture
    def workbook(self, portal):
        portal.get(API + "resource_show").respond(json=ok(XLSX_RESOURCE))
        portal.get(API + "package_show").respond(json=ok(KIEL_PACKAGE))
        data = xlsx({"Alle": HAUSHALT, "Zweite": [["k", "v"], ["a", 1], ["b", 2]]})
        return portal.get("/data/fm/Titelliste_2021.xlsx").mock(
            return_value=httpx.Response(200, content=data, headers={"content-type": XLSX_TYPE})
        )

    async def test_preview_downloads_whole_workbook_despite_small_max_kb(self, workbook):
        out = await server.preview_resource(XLSX_RESOURCE["id"], max_rows=1, max_kb=16)
        assert out["kind"] == "spreadsheet"
        assert out["resource"]["format"] == "XLSX"
        assert out["rows"] == [{"EP": "01", "Kapitel": "0101", "Zweckbestimmung": "Kostenbeiträge", "Ist 2019 T€": 0, "Ansatz 2021 T€": 0}]
        assert out["sample_note"] == "first 1 of 3 rows"
        assert out["source"]["resource_url"] == XLSX_RESOURCE["url"]

    async def test_analyze_with_sheet_and_cache_per_sheet(self, workbook):
        server._RECORDS_CACHE.clear()
        first = await server.analyze_resource(XLSX_RESOURCE["id"], metrics=[Metric(agg="sum", column="Ist 2019 T€")])
        assert first["result"] == {"sum(Ist 2019 T€)": 20.7}
        assert first["sheet"] == "Alle"
        second = await server.analyze_resource(XLSX_RESOURCE["id"], sheet="Zweite", metrics=[Metric(agg="sum", column="v")])
        assert second["result"] == {"sum(v)": 3}
        assert second["sheet"] == "Zweite"
        server._RECORDS_CACHE.clear()

    async def test_unknown_sheet_is_a_tool_error(self, workbook):
        with pytest.raises(PortalError, match="no sheet named 'Nope'"):
            await server.preview_resource(XLSX_RESOURCE["id"], sheet="Nope")
