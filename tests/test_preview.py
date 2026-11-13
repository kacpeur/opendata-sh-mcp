"""Unit tests for preview.py (no HTTP)."""

from __future__ import annotations

import json

import pytest

from opendata_sh_mcp import preview
from opendata_sh_mcp.preview import build_preview, decode, preview_csv, preview_json

from .conftest import CSV_BYTES, CSV_TEXT, GEOJSON, GEOJSON_BYTES


class TestDecode:
    def test_utf8(self):
        assert decode("Kiel – Düsternbrook".encode()) == "Kiel – Düsternbrook"

    def test_utf8_bom_is_stripped(self):
        assert decode("﻿Stadtteil;Jahr".encode()) == "Stadtteil;Jahr"

    def test_cp1252_umlauts_and_euro(self):
        raw = "Größe;Straße;Preis €;„Zitat“".encode("cp1252")
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
        assert decode(raw) == "Größe;Straße;Preis €;„Zitat“"

    def test_latin1_fallback_for_bytes_undefined_in_cp1252(self):
        # 0x81 is undefined in cp1252 but valid in latin-1.
        assert decode(b"a\x81\xe4") == "a\x81ä"

    def test_encoding_order(self):
        assert preview.TEXT_ENCODINGS[0] == "utf-8-sig"


class TestCsv:
    def test_semicolon_cp1252_with_decimal_commas(self):
        out = build_preview(CSV_BYTES, "CSV", "text/csv; charset=windows-1252", max_rows=3, truncated=False)
        assert out["kind"] == "csv"
        assert out["delimiter"] == ";"
        assert out["columns"] == ["Stadtteil", "Jahr", "Einwohner", "Anteil in %", "Fläche km²"]
        assert out["rows_in_sample"] == 5
        assert len(out["rows"]) == 3
        assert out["rows"][1] == {
            "Stadtteil": "Düsternbrook",
            "Jahr": "2023",
            "Einwohner": "2.345",
            "Anteil in %": "0,9",
            "Fläche km²": "2,41",
        }
        assert out["sample_truncated"] is False

    def test_truncated_drops_partial_last_line(self):
        cut = CSV_TEXT[: CSV_TEXT.index("Wik;2023;19") + 8]  # "Wik;2023" -- a partial row
        out = preview_csv(cut, max_rows=100, truncated=True)
        assert [r["Stadtteil"] for r in out["rows"]] == ["Altstadt", "Düsternbrook", "Gaarden-Ost", "Schreventeich"]
        assert out["rows_in_sample"] == 4
        assert out["sample_truncated"] is True

    def test_not_truncated_keeps_last_line_without_newline(self):
        text = "a;b\n1;2\n3;4"
        out = preview_csv(text, max_rows=10, truncated=False)
        assert out["rows"] == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]

    def test_truncated_single_line_is_kept(self):
        out = preview_csv("Stadtteil;Jahr", max_rows=10, truncated=True)
        assert out["columns"] == ["Stadtteil", "Jahr"]
        assert out["rows"] == []

    def test_comma_delimited(self):
        out = preview_csv("name,wert\nKiel,1\nLübeck,2\n", max_rows=10, truncated=False)
        assert out["delimiter"] == ","
        assert out["rows"][1] == {"name": "Lübeck", "wert": "2"}

    def test_tab_delimited_via_tsv_format(self):
        out = build_preview(b"Ort\tAnzahl\nHusum\t3\nHeide\t4\n", "tsv", "", 10, False)
        assert out["delimiter"] == "\t"
        assert out["rows"][0] == {"Ort": "Husum", "Anzahl": "3"}

    def test_sniffer_failure_falls_back_to_counting(self):
        # A single column gives the sniffer nothing to work with.
        out = preview_csv("Stadtteil\nAltstadt\nWik\n", max_rows=10, truncated=False)
        assert out["columns"] == ["Stadtteil"]
        assert out["rows"] == [{"Stadtteil": "Altstadt"}, {"Stadtteil": "Wik"}]

    def test_blank_lines_skipped(self):
        out = preview_csv("a;b\n\n1;2\n;\n3;4\n", max_rows=10, truncated=False)
        assert out["rows_in_sample"] == 2

    def test_empty(self):
        out = preview_csv("", max_rows=10, truncated=False)
        assert out == {"kind": "csv", "columns": [], "rows": [], "note": "empty file"}

    def test_content_type_triggers_csv(self):
        out = build_preview(b"a;b\n1;2\n", "", "text/csv", 5, False)
        assert out["kind"] == "csv"


class TestJson:
    def test_list(self):
        data = [{"Stadtteil": "Altstadt", "Einwohner": 1234}, {"Stadtteil": "Wik"}, {"Stadtteil": "Pries"}]
        out = preview_json(json.dumps(data), max_rows=2, truncated=False)
        assert out == {"kind": "json", "length": 3, "items": data[:2]}

    def test_dict_shape(self):
        data = {"stand": "2024-01-01", "werte": list(range(10)), "quelle": "Kiel"}
        out = preview_json(json.dumps(data), max_rows=3, truncated=False)
        assert out["kind"] == "json"
        assert out["keys"] == ["stand", "werte", "quelle"]
        assert out["sample"] == {"stand": "2024-01-01", "werte": [0, 1, 2], "quelle": "Kiel"}

    def test_scalar(self):
        assert preview_json("42", 5, False) == {"kind": "json", "value": 42}

    def test_geojson(self):
        out = build_preview(GEOJSON_BYTES, "GeoJSON", "application/geo+json", max_rows=2, truncated=False)
        assert out["kind"] == "geojson"
        assert out["feature_count"] == 3
        assert out["property_keys"] == ["flaeche_km2", "name", "nummer"]
        assert out["geometry_types"] == ["MultiPolygon", "None", "Polygon"]
        assert out["features"] == [
            {"properties": GEOJSON["features"][0]["properties"], "geometry_type": "Polygon"},
            {"properties": {"name": "Düsternbrook", "flaeche_km2": 2.41}, "geometry_type": "MultiPolygon"},
        ]

    def test_truncated_json_shows_head(self):
        out = preview_json('{"type": "FeatureCollection", "features": [', 5, truncated=True)
        assert out["kind"] == "json"
        assert "larger than the preview limit" in out["note"]
        assert out["head"].startswith('{"type"')

    def test_invalid_json(self):
        out = preview_json("{nicht json", 5, False)
        assert out["note"].startswith("invalid JSON")
        assert out["head"] == "{nicht json"

    def test_json_detected_by_content_type(self):
        out = build_preview(b"[1,2,3]", "", "application/json; charset=utf-8", 5, False)
        assert out == {"kind": "json", "length": 3, "items": [1, 2, 3]}


class TestOtherFormats:
    def test_xml_head(self):
        xml = b'<?xml version="1.0" encoding="UTF-8"?><wfs:FeatureCollection/>'
        out = build_preview(xml, "WFS", "text/xml", 5, True)
        assert out == {"kind": "wfs", "head": xml.decode(), "sample_truncated": True}

    def test_xml_by_content_type_without_format(self):
        out = build_preview(b"<a/>", "", "application/xml", 5, False)
        assert out["kind"] == "text"

    def test_binary_fallback_by_format(self):
        out = build_preview(b"PK\x03\x04" + b"\x00" * 100, "XLSX", "application/octet-stream", 5, False)
        assert out["kind"] == "binary"
        assert out["bytes_read"] == 104
        assert "'xlsx'" in out["note"]

    def test_xlsx_real_content_type_is_binary(self):
        ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        out = build_preview(b"PK\x03\x04" + b"\x00" * 100, "XLSX", ctype, 5, False)
        assert out["kind"] == "binary"

    def test_binary_fallback_by_content_type(self):
        out = build_preview(b"%PDF-1.7", "", "application/pdf", 5, False)
        assert out["kind"] == "binary"
        assert "'application/pdf'" in out["note"]


def test_preview_csv_separates_units_and_trailing_text():
    from .test_analyze import KFUE_CSV

    out = preview_csv(decode(KFUE_CSV), max_rows=10, truncated=False)
    assert out["rows_in_sample"] == 3
    assert out["rows"][0]["Datum/Uhrzeit"] == "28.09.2026 23:00:00"
    assert out["units"]["KBR.1011.IMK"] == "[µSv/h]"
    assert out["trailing_text"].startswith("Alle Genitronstationen")


def test_preview_csv_without_units_row_keeps_first_row():
    out = preview_csv("a;b\nx;1\ny;2\n", max_rows=10, truncated=False)
    assert "units" not in out and out["rows_in_sample"] == 2
