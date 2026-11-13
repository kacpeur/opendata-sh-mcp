"""Layouts copied from real portal files: what the table detection must cope with."""

from __future__ import annotations

import io
import zipfile

import pytest

from opendata_sh_mcp.analyze import Metric, RowFilter, analyze, load_records
from opendata_sh_mcp.ckan import PortalError
from opendata_sh_mcp.preview import build_preview, parse_csv_table, sniff_delimiter, unwrap_zip
from opendata_sh_mcp.table import detect_table

# Statistikamt Nord "Zeitreihe" export: BOM, title lines, group row over sub-headers, notes below.
STATISTIK_CSV = (
    "﻿Zeitreihe für Zarpen\n"
    '""\n'
    "Bevölkerungsstand nach Altersgruppen in Zarpen am 31.12.\n"
    '""\n'
    '"";0-17 Jahre;;18-24 Jahre;;Insgesamt\n'
    "Zeit;Anzahl;%;Anzahl;%;Anzahl;%\n"
    "2000;355 ;22,8 ;134 ;8,6 ;1557 ;100,0 \n"
    "2001;362 ;23,0 ;120 ;7,6 ;1574 ;100,0 \n"
    "2002;363 ;22,7 ;127 ;8,0 ;1597 ;100,0 \n"
    '""\n'
    "Informationen zur Bevölkerungsstatistik\n"
    "Grundlage sind die Fortschreibungsergebnisse auf Basis der Volkszählung 1987.\n"
).encode()


class TestStatistikamtLayout:
    def test_title_two_row_header_and_notes(self):
        info: dict = {}
        rows = load_records(STATISTIK_CSV, "csv", "text/csv; charset=iso-8859-1", info)
        assert list(rows[0]) == [
            "Zeit", "0-17 Jahre Anzahl", "0-17 Jahre %", "18-24 Jahre Anzahl", "18-24 Jahre %", "Insgesamt Anzahl", "Insgesamt %",
        ]  # fmt: skip
        assert [r["Zeit"] for r in rows] == ["2000", "2001", "2002"]
        assert rows[0]["Insgesamt Anzahl"] == "1557"  # trailing blank stripped
        assert info["leading_text"] == "Zeitreihe für Zarpen\nBevölkerungsstand nach Altersgruppen in Zarpen am 31.12."
        assert info["trailing_text"].startswith("Informationen zur Bevölkerungsstatistik")
        assert "two header rows" in info["header_note"]

    def test_exact_sum_over_it(self):
        rows = load_records(STATISTIK_CSV, "csv", "")
        out = analyze(rows, metrics=[Metric(agg="sum", column="Insgesamt Anzahl"), Metric(agg="mean", column="0-17 Jahre %")])
        assert out["rows_total"] == 3
        assert out["result"] == {"sum(Insgesamt Anzahl)": 4728, "mean(0-17 Jahre %)": 22.8333333333}

    def test_single_value_column_keeps_plain_header(self):
        text = "Zeitreihe für Flensburg\n\"\"\nAmbulante Pflegedienste\n\"\"\nZeit;Ambulante Pflegedienste\n2009;11\n2011;12\n"
        table = parse_csv_table(text)
        assert table["header"] == ["Zeit", "Ambulante Pflegedienste"]
        assert table["body"] == [["2009", "11"], ["2011", "12"]]
        assert table["leading_text"] == "Zeitreihe für Flensburg\nAmbulante Pflegedienste"

    def test_missing_value_markers_are_data_not_a_units_row(self):
        table = parse_csv_table("Zeit;Wohnungen;Anteil\n2000;-;.\n2001;11;2,5\n2002;12;2,6\n2003;9;1,9\n")
        assert table["units"] is None
        assert len(table["body"]) == 4


class TestHeaderDetection:
    def test_headerless_file_gets_generated_names(self):
        text = "DESH_PR_0001|14|Spielplatz\nDESH_PR_0001|22|Hundestrand\nDESH_PR_0002|14|Spielplatz\nDESH_PR_0003|7|Dusche\n"
        table = parse_csv_table(text)
        assert table["delimiter"] == "|"
        assert table["header"] == ["col_1", "col_2", "col_3"]
        assert len(table["body"]) == 4
        assert "no header row" in table["header_note"]

    def test_year_column_names_are_still_a_header(self):
        table = parse_csv_table("Förderung\tTitel\t1996\t2004\nLehrkräfte\t0708 68402\t281,2\t291,6\nKindergeld\t0708 68403\t35,8\t93,7\n")
        assert table["header"] == ["Förderung", "Titel", "1996", "2004"]
        assert table["header_note"] is None
        assert len(table["body"]) == 2

    def test_text_only_table_keeps_first_row_as_header(self):
        table = parse_csv_table("Name;Ort\nSchule A;Kiel\nSchule B;Kiel\n")
        assert table["header"] == ["Name", "Ort"]
        assert len(table["body"]) == 2

    def test_second_label_row_becomes_units(self):
        table = parse_csv_table("Datum;Wert A;Wert B\n;[µSv/h];[°C]\n01.01.2026;7,1e-02;4,5\n02.01.2026;7,3e-02;4,8\n")
        assert table["units"] == {"Wert A": "[µSv/h]", "Wert B": "[°C]"}
        assert len(table["body"]) == 2

    def test_duplicate_and_empty_column_names_are_made_distinct(self):
        table = detect_table([["Ort", "Wert", "Wert", ""], ["Kiel", "1", "2", "x"], ["Husum", "3", "4", "y"]])
        assert table.header == ["Ort", "Wert", "Wert (2)", "col_4"]
        assert table.body[0] == ["Kiel", "1", "2", "x"]

    def test_wider_data_rows_are_not_cut(self):
        table = detect_table([["a", "b"], ["1", "2"], ["3", "4"], ["5", "6", "extra"]])
        assert table.header == ["a", "b", "col_3"]
        assert table.body[-1] == ["5", "6", "extra"]

    def test_empty(self):
        assert detect_table([]).header == []
        assert detect_table([[""], ["", " "]]).body == []


class TestDelimiter:
    def test_pipe_file_with_semicolons_inside_cells(self):
        text = (
            "DESH_PR_0023|RATZEBURGER SEE;POGEEZ;BADEANSTALT|See|40|9610\n"
            "DESH_PR_0024|RATZEBURGER SEE;BUCHHOLZ;LIEGEWIESE|See|10|9610\n"
            "DESH_PR_0025|RATZEBURGER SEE;RATZEBURG;AQUA SIWA|See|25|9610\n"
        )
        assert sniff_delimiter(text) == "|"

    def test_semicolon_file_with_decimal_commas_in_every_row(self):
        assert sniff_delimiter("Jahr;Anteil;Quote\n2000;1,5;2,5\n2001;1,6;2,6\n2002;1,7;2,7\n") == ";"

    def test_comma_file_with_quoted_commas(self):
        assert sniff_delimiter('name,adresse,plz\n"Meier, A.","Weg 1, Kiel",24103\n"Kuhn, B.","Allee 2, Kiel",24105\n') == ","

    def test_bare_carriage_returns_do_not_crash(self):
        table = parse_csv_table("a;b\r1;2\r3;4\r")
        assert table["header"] == ["a", "b"]
        assert table["body"] == [["1", "2"], ["3", "4"]]


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestZipLabelledAsCsv:
    CSV = b"TKNR;Zst;Stunde;KFZ_R1\n2225;1144;01;21\n2225;1144;02;17\n" + b"2225;1144;03;9\n" * 400

    def test_analyze_reads_the_csv_inside(self):
        info: dict = {}
        rows = load_records(_zip({"zst1144_2010.csv": self.CSV}), "csv", "application/zip", info)
        assert len(rows) == 402
        assert info["archive_member"] == "zst1144_2010.csv"
        assert analyze(rows, metrics=[Metric(agg="sum", column="KFZ_R1")])["result"] == {"sum(KFZ_R1)": 21 + 17 + 9 * 400}

    def test_preview_of_complete_archive(self):
        out = build_preview(_zip({"readme.pdf": b"%PDF", "zst.csv": self.CSV}), "CSV", "application/zip", 2, False)
        assert out["kind"] == "csv"
        assert out["columns"] == ["TKNR", "Zst", "Stunde", "KFZ_R1"]
        assert out["archive_member"] == "zst.csv"
        assert out["archive_other_members"] == ["readme.pdf"]

    def test_preview_of_partial_download_reads_first_member(self):
        archive = _zip({"zst.csv": self.CSV})
        out = build_preview(archive[: len(archive) // 2], "csv", "application/zip", 2, True)
        assert out["kind"] == "csv"
        assert out["rows"][0] == {"TKNR": "2225", "Zst": "1144", "Stunde": "01", "KFZ_R1": "21"}
        assert out["sample_truncated"] is True

    def test_analyze_refuses_member_over_the_limit(self):
        with pytest.raises(PortalError, match="over the limit"):
            load_records(_zip({"big.csv": self.CSV}), "csv", "", max_unpacked=1000)

    def test_broken_archive_is_reported(self):
        with pytest.raises(PortalError, match="ZIP"):
            load_records(b"PK\x03\x04" + b"\x00" * 40, "csv", "")
        assert build_preview(b"PK\x03\x04" + b"\x00" * 40, "csv", "", 5, False)["kind"] == "binary"

    def test_spreadsheets_are_left_alone(self):
        data = _zip({"xl/workbook.xml": b"<x/>"})
        assert unwrap_zip(data, "xlsx", False, 10**6) == (data, False, {})

    def test_binary_content_labelled_csv(self):
        with pytest.raises(PortalError, match="binary"):
            load_records(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "csv", "")


class TestNumberStylePerColumn:
    # English decimals: "1.234" here is one point two, never 1234.
    ROWS = [
        {"ort": "A", "wert": "1.234"},
        {"ort": "A", "wert": "2.000"},
        {"ort": "B", "wert": "1.5"},
        {"ort": "B", "wert": "12.34"},
    ]

    def test_grouped_sums_agree_with_the_total(self):
        total = analyze(self.ROWS, metrics=[Metric(agg="sum", column="wert")])["result"]["sum(wert)"]
        groups = analyze(self.ROWS, group_by=["ort"], metrics=[Metric(agg="sum", column="wert")], sort="key")["groups"]
        assert total == 17.074
        assert [g["sum(wert)"] for g in groups] == [3.234, 13.84]

    def test_filters_use_the_same_reading(self):
        out = analyze(self.ROWS, filters=[RowFilter(column="wert", op="lt", value=3)])
        assert out["rows_matched"] == 3
        assert analyze(self.ROWS, filters=[RowFilter(column="wert", op="eq", value="1.234")])["rows_matched"] == 1

    def test_german_thousands_column_is_unchanged(self):
        rows = [{"wert": "1.234"}, {"wert": "2.000"}, {"wert": "12,5"}]
        assert analyze(rows, metrics=[Metric(agg="sum", column="wert")])["result"] == {"sum(wert)": 3246.5}

    def test_percent_and_unicode_minus(self):
        rows = [{"anteil": "77,80%"}, {"anteil": "22,20 %"}, {"anteil": "−5"}]
        assert analyze(rows, metrics=[Metric(agg="sum", column="anteil")])["result"] == {"sum(anteil)": 95}
