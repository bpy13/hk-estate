"""RVD (Rating and Valuation Department) property market statistics.

RVD publishes legacy .xls files with bilingual headers. Each file used here has a fixed
column layout: one column per class (A-E by saleable area, plus combined groups) and, in
some files, per region. Each spec below names the value columns and the header text expected
next to them; if RVD moves a column the loader stops with `LayoutChanged` instead of loading
numbers under the wrong label.

Monthly sheets: year in column 1 (only on January rows), month in column 5, `*` in column 6
for provisional figures, a value's cell flanked by `(` `)` when based on fewer than 20 deals.
Annual sheets (completions, stock, vacancy): year in column 2, `#` in column 3 = provisional.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import duckdb

BASE = "https://www.rvd.gov.hk/doc/en/statistics/"


class LayoutChanged(Exception):
    """The spreadsheet's headers are not where the spec expects them."""


@dataclass(frozen=True)
class RvdFile:
    filename: str
    sheet: int
    table: str  # price_index, rent_index, market_stat
    series: str
    freq: str  # month, year
    columns: dict  # value column -> (class, region)
    checks: tuple  # (column, expected header text) pairs

    @property
    def url(self) -> str:
        return BASE + self.filename


_CLASS_COLS = {8: "A", 11: "B", 14: "C", 17: "D", 20: "E", 23: "ABC", 26: "DE", 29: "ALL"}
_CLASS_CHECKS = (
    (7, "A"), (10, "B"), (13, "C"), (16, "D"), (19, "E"),
    (22, "A, B & C"), (25, "D & E"), (28, "All Classes"),
)
_REGIONS3 = ("HK", "KLN", "NT")
_AVG_COLS = {8 + 3 * k: ("ABCDE"[k // 3], _REGIONS3[k % 3]) for k in range(15)}
_AVG_CHECKS = tuple((7 + 9 * i, "ABCDE"[i]) for i in range(5)) + tuple(
    (7 + 3 * k, ("港島", "九龍", "新界")[k % 3]) for k in range(15)
)
_POP_COLS = {
    8 + 3 * k: (("ABC", "DE", "ALL")[k // 3], ("URBAN", "NT", "ALL")[k % 3]) for k in range(9)
}
_POP_CHECKS = ((7, "A, B & C"), (16, "D & E"), (25, "所有類別")) + tuple(
    (7 + 3 * k, ("Urban", "N.T.", "All")[k % 3]) for k in range(9)
)
_STOCK_COLS = {4: ("A", "ALL"), 6: ("B", "ALL"), 8: ("C", "ALL"), 10: ("D", "ALL"),
               12: ("E", "ALL"), 14: ("ALL", "ALL")}
_STOCK_CHECKS = ((4, "A"), (6, "B"), (8, "C"), (10, "D"), (12, "E"), (14, "總 數 Total"))


def _cls(cols):
    return {c: (v, "ALL") for c, v in cols.items()}


FILES: dict[str, RvdFile] = {
    "avg_rent": RvdFile("his_data_1.xls", 0, "rent_index", "rvd_avg_rent_psm", "month",
                        _AVG_COLS, _AVG_CHECKS),
    "avg_price": RvdFile("his_data_2.xls", 0, "price_index", "rvd_avg_price_psm", "month",
                         _AVG_COLS, _AVG_CHECKS),
    "rent_index": RvdFile("his_data_3.xls", 0, "rent_index", "rvd_rent_index", "month",
                          _cls(_CLASS_COLS), _CLASS_CHECKS),
    "price_index": RvdFile("his_data_4.xls", 0, "price_index", "rvd_price_index", "month",
                           _cls(_CLASS_COLS), _CLASS_CHECKS),
    "popular": RvdFile("his_data_5.xls", 0, "price_index", "rvd_popular_dev_index", "month",
                       _POP_COLS, _POP_CHECKS),
    "completions": RvdFile("private_domestic.xls", 0, "market_stat", "rvd_completions", "year",
                           _STOCK_COLS, _STOCK_CHECKS),
    "stock": RvdFile("private_domestic.xls", 1, "market_stat", "rvd_stock", "year",
                     _STOCK_COLS, _STOCK_CHECKS),
    "vacancy": RvdFile("private_domestic.xls", 2, "market_stat", "rvd_vacancy", "year",
                       _STOCK_COLS, _STOCK_CHECKS),
}


@dataclass(frozen=True)
class Point:
    series: str
    freq: str
    period: date
    cls: str
    region: str
    value: float
    provisional: bool
    few_deals: bool


def _cell(row, i) -> str:
    return str(row[i]).strip() if i < len(row) and row[i] is not None else ""


def _number(text: str) -> float | None:
    t = text.replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _check_headers(header_rows, spec: RvdFile) -> None:
    for col, expected in spec.checks:
        want = _squash(expected).replace(" ", "")
        found = any(_squash(_cell(r, col)).replace(" ", "") == want for r in header_rows)
        if not found:
            raise LayoutChanged(f"{spec.filename}: expected {expected!r} in column {col}")


def parse_sheet(rows: list[list], spec: RvdFile) -> list[Point]:
    """Parse one sheet (a list of rows of cell values) according to `spec`."""
    points: list[Point] = []
    header_end = None
    year = None
    for i, row in enumerate(rows):
        if spec.freq == "month":
            y = _number(_cell(row, 1))
            if y and 1900 < y < 2100:
                year = int(y)
            m = _number(_cell(row, 5))
            if year is None or m is None or not (1 <= m <= 12) or m != int(m):
                continue
            period = date(year, int(m), 1)
            provisional = "*" in _cell(row, 6)
        else:
            y = _number(_cell(row, 2))
            if y is None or not (1900 < y < 2100):
                continue
            period = date(int(y), 1, 1)
            provisional = "#" in _cell(row, 3)
        if header_end is None:
            header_end = i
            _check_headers(rows[:i], spec)
        for col, (cls, region) in spec.columns.items():
            value = _number(_cell(row, col))
            if value is None:
                continue
            points.append(
                Point(spec.series, spec.freq, period, cls, region, value, provisional,
                      few_deals=_cell(row, col - 1) == "(")
            )
    if header_end is None:
        raise LayoutChanged(f"{spec.filename}: no data rows found")
    return points


def read_xls(content: bytes, sheet: int) -> list[list]:
    import xlrd

    book = xlrd.open_workbook(file_contents=content)
    sh = book.sheet_by_index(sheet)
    return [sh.row_values(r) for r in range(sh.nrows)]


def store(con: duckdb.DuckDBPyConnection, spec: RvdFile, points: list[Point], url: str) -> int:
    """Replace the series with the freshly parsed points (RVD revises provisional figures)."""
    now = datetime.now()
    con.execute("BEGIN")
    try:
        con.execute(f"DELETE FROM {spec.table} WHERE series = ?", [spec.series])
        con.executemany(
            f"INSERT INTO {spec.table} (series, freq, period, class, region, value, provisional,"
            " few_deals, source_url, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (p.series, p.freq, p.period, p.cls, p.region, p.value, p.provisional,
                 p.few_deals, url, now)
                for p in points
            ],
        )
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return len(points)


def load_all(fetcher, db_path=None, log=print) -> dict[str, int]:
    """Download every RVD file in FILES and store its series. Returns rows per series."""
    from hkestate import db

    loaded = {}
    for spec in FILES.values():
        content = fetcher.get_bytes(spec.url, "static")
        points = parse_sheet(read_xls(content, spec.sheet), spec)
        with db.session(db_path) as con:
            db.init_db(con)
            loaded[spec.series] = store(con, spec, points, spec.url)
        latest = max(p.period for p in points)
        log(f"{spec.series}: {loaded[spec.series]} values, latest {latest:%Y-%m}")
    return loaded
