"""Unit tests for analyze.py (no HTTP)."""

from __future__ import annotations

import json

import pytest

from opendata_sh_mcp.analyze import Metric, RowFilter, analyze, load_records, to_date, to_number
from opendata_sh_mcp.ckan import PortalError

from .conftest import CSV_BYTES, GEOJSON_BYTES


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1.234", 1234.0),
        ("1.234,5", 1234.5),
        ("0,5", 0.5),
        ("1,234.5", 1234.5),
        ("12.5", 12.5),
        (" 19.012 ", 19012.0),
        ("-3", -3.0),
        ("1 000", 1000.0),
        (7, 7.0),
        ("", None),
        ("k.A.", None),
        ("-", None),
        (None, None),
        (True, None),
        (float("nan"), None),
    ],
)
def test_to_number(raw, expected):
    assert to_number(raw) == expected


def test_to_number_dot_decimal():
    assert to_number("1.234", dot_decimal=True) == 1.234


def test_load_csv_cp1252_semicolon():
    rows = load_records(CSV_BYTES, "csv", "")
    assert len(rows) == 5
    assert rows[1]["Stadtteil"] == "Düsternbrook"
    assert rows[1]["Fläche km²"] == "2,41"


def test_load_geojson_properties():
    rows = load_records(GEOJSON_BYTES, "geojson", "")
    assert [r["name"] for r in rows] == ["Altstadt", "Düsternbrook", "Friedrichsort"]


def test_load_json_wrapped_list():
    doc = {"meta": {"x": 1}, "data": [{"a": 1}, {"a": 2}]}
    assert load_records(json.dumps(doc).encode(), "json", "") == [{"a": 1}, {"a": 2}]


def test_load_json_not_records():
    with pytest.raises(PortalError, match="not a list of records"):
        load_records(b'{"a": 1}', "json", "")


def test_load_unsupported_format():
    with pytest.raises(PortalError, match="cannot be analysed"):
        load_records(b"PK\x03\x04", "xlsx", "application/vnd.ms-excel")


ROWS = load_records(CSV_BYTES, "csv", "")


def test_default_is_row_count():
    out = analyze(ROWS)
    assert out["rows_total"] == out["rows_matched"] == 5
    assert out["result"] == {"count(*)": 5}
    assert "Einwohner" in out["columns"]


def test_exact_sum_mean_median_min_max_german_numbers():
    metrics = [Metric(agg=a, column=c) for a, c in [("sum", "Einwohner"), ("mean", "Anteil in %"), ("median", "Einwohner"), ("min", "Fläche km²"), ("max", "Einwohner")]]
    out = analyze(ROWS, metrics=metrics)["result"]
    assert out == {
        "sum(Einwohner)": 51232,
        "mean(Anteil in %)": 4.04,
        "median(Einwohner)": 9876,
        "min(Fläche km²)": 0.35,
        "max(Einwohner)": 19012,
    }


def test_filters_numeric_text_and_in():
    f = [RowFilter(column="Einwohner", op="gt", value=5000), RowFilter(column="Stadtteil", op="ne", value="wik")]
    out = analyze(ROWS, filters=f, metrics=[Metric(agg="sum", column="Einwohner")])
    assert out["rows_matched"] == 2
    assert out["result"]["sum(Einwohner)"] == 18765 + 9876
    out = analyze(ROWS, filters=[RowFilter(column="Stadtteil", op="in", value=["Wik", "Altstadt"])])
    assert out["result"]["count(*)"] == 2
    out = analyze(ROWS, filters=[RowFilter(column="Stadtteil", op="contains", value="GAARDEN")])
    assert out["result"]["count(*)"] == 1


def test_group_by_sorting_and_cap():
    rows = [{"Kreis": k, "Wert": v} for k, v in [("A", "1"), ("B", "5"), ("A", "2"), ("C", "x"), ("B", "1")]]
    out = analyze(rows, group_by=["Kreis"], metrics=[Metric(agg="sum", column="Wert"), Metric(agg="count")])
    assert out["group_count"] == 3
    assert out["groups"] == [
        {"key": {"Kreis": "B"}, "sum(Wert)": 6, "count(*)": 2},
        {"key": {"Kreis": "A"}, "sum(Wert)": 3, "count(*)": 2},
        {"key": {"Kreis": "C"}, "sum(Wert)": None, "count(*)": 1},
    ]
    assert out["values_not_numeric"] == {"sum(Wert)": 1}
    out = analyze(rows, group_by=["Kreis"], sort="key", max_groups=2)
    assert [g["key"]["Kreis"] for g in out["groups"]] == ["A", "B"]
    assert "showing 2 of 3 groups" in out["note"]


def test_dot_decimal_column_detected():
    rows = [{"v": "1.5"}, {"v": "1.234"}]
    assert analyze(rows, metrics=[Metric(agg="sum", column="v")])["result"]["sum(v)"] == 2.734


def test_count_distinct_and_empty_filters():
    rows = [{"a": "x"}, {"a": "x"}, {"a": ""}, {"a": "y"}]
    out = analyze(rows, filters=[RowFilter(column="a", op="not_empty")], metrics=[Metric(agg="count_distinct", column="a")])
    assert out["rows_matched"] == 3 and out["result"] == {"count_distinct(a)": 2}
    assert analyze(rows, filters=[RowFilter(column="a", op="empty")])["result"] == {"count(*)": 1}


def test_unknown_column_lists_available():
    with pytest.raises(PortalError, match=r"unknown column\(s\) \['Jahrgang'\].*Stadtteil"):
        analyze(ROWS, group_by=["Jahrgang"])


def test_sum_needs_column():
    with pytest.raises(PortalError, match="sum needs a column"):
        analyze(ROWS, metrics=[Metric(agg="sum")])


# Shaped like KFÜ-Messwerte: units row, hourly values in scientific notation with decimal
# commas, then a station list and comments after a blank line.
KFUE_CSV = (
    "Datum/Uhrzeit;KBR.1011.IMK;KBR.TBAS.MET;\r\n"
    ";[µSv/h];[°C];\r\n"
    "28.09.2026 23:00:00;7,787e-02;12,5;\r\n"
    "29.09.2026 00:00:00;7,425e-02;11,9;\r\n"
    "22.09.2026 01:00:00;7,335e-02;14,1;\r\n"
    "\r\n"
    "Alle Genitronstationen für opendata-Portal (7 Tage)\r\n"
    '"KBR.1011.IMK                    KFÜ-Sonde KBR N          µSv/h     "\r\n'
    "\r\n"
    "Kommentare\r\n"
    '"KBR.*   23.09.2026 20:00  24.09.2026 00:00  leicht erhöhte Werte bei Regen (Washout)"\r\n'
).encode("cp1252")


def test_kfue_layout_units_and_trailing_text_are_not_data():
    info: dict = {}
    rows = load_records(KFUE_CSV, "csv", "", info)
    assert len(rows) == 3
    assert info["units"] == {"KBR.1011.IMK": "[µSv/h]", "KBR.TBAS.MET": "[°C]"}
    assert info["trailing_text"].startswith("Alle Genitronstationen")
    assert "Washout" in info["trailing_text"]


def test_kfue_newest_entry_and_scientific_notation():
    rows = load_records(KFUE_CSV, "csv", "")
    out = analyze(
        rows,
        metrics=[Metric(agg="max", column="Datum/Uhrzeit"), Metric(agg="min", column="Datum/Uhrzeit"), Metric(agg="max", column="KBR.1011.IMK")],
    )
    assert out["result"] == {
        "max(Datum/Uhrzeit)": "2026-09-29T00:00:00",
        "min(Datum/Uhrzeit)": "2026-09-22T01:00:00",
        "max(KBR.1011.IMK)": 0.07787,
    }
    assert "values_not_numeric" not in out


def test_german_date_filter_compares_chronologically():
    rows = load_records(KFUE_CSV, "csv", "")
    out = analyze(rows, filters=[RowFilter(column="Datum/Uhrzeit", op="ge", value="28.09.2026")])
    assert out["rows_matched"] == 2
    # as text, "28.09.2026" >= "01.10.2026" would be true for every row
    out = analyze(rows, filters=[RowFilter(column="Datum/Uhrzeit", op="ge", value="01.10.2026")])
    assert out["rows_matched"] == 0
    out = analyze(rows, filters=[RowFilter(column="Datum/Uhrzeit", op="lt", value="2026-09-23")])
    assert out["rows_matched"] == 1


@pytest.mark.parametrize(
    "raw, expected",
    [("29.09.2026", "2026-09-29T00:00:00"), ("1.2.2024 7:05", "2024-02-01T07:05:00"), ("2026-09-30T05:14:47", "2026-09-30T05:14:47"), ("31.02.2024", None), ("Kiel", None)],
)
def test_to_date(raw, expected):
    d = to_date(raw)
    assert (d.isoformat() if d else None) == expected
