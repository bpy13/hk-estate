"""Show sheet names and the last non-empty rows of a cached RVD .xls file. No fetching."""

import gzip
import hashlib
import sys

import xlrd

from hkestate.config import get_data_dir

url = sys.argv[1]
sha = hashlib.sha1(url.encode()).hexdigest()
path = sorted((get_data_dir() / "cache" / "www.rvd.gov.hk").glob(f"*/{sha}.html.gz"))[-1]
book = xlrd.open_workbook(file_contents=gzip.decompress(path.read_bytes()))
for sheet in book.sheets():
    rows = [sheet.row_values(i) for i in range(sheet.nrows)]
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    print(f"== sheet {sheet.name!r}: {sheet.nrows} rows x {sheet.ncols} cols")
    for r in rows[:6] + [["..."]] + rows[-6:]:
        print("   ", [str(c)[:18] for c in r[:9]])
