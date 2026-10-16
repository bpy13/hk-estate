"""Keyword risk flags from listing text, including negations."""

from datetime import datetime

from hkestate import db, flags
from hkestate.listing_types import Listing


def f(text):
    return set(flags.find_flags(text))


def test_seed_terms():
    assert f("凶宅 平賣") == {"haunted"}
    assert f("Rare haunted flat") == {"haunted"}
    assert f("連租約 回報4厘") == {"tenanted"}
    assert f("連租出售") == {"tenanted"}
    assert f("Sold tenanted") == {"tenanted"}
    assert f("高層海景三房") == {"sea_view"}
    assert f("Full SEA VIEW") == {"sea_view"}
    assert f("全新裝修 即買即住") == {"renovated"}
    assert f("newly renovated") == {"renovated"}
    assert f("單位有僭建") == {"illegal_structure"}
    assert f("illegal structure removed?") == {"illegal_structure"}
    assert f("開放式 單位") == {"studio"}
    assert f("cosy studio") == {"studio"}
    assert f("三房 兩廳") == set()


def test_negations():
    assert f("非凶宅 海景三房") == {"sea_view"}
    assert f("保證不是凶宅") == set()
    assert f("並非兇宅") == set()
    assert f("Not haunted") == set()
    assert f("交吉 不連租約") == set()
    assert f("無僭建") == set()
    assert f("沒有海景") == set()
    assert f("開放式廚房 兩房") == set()  # an open kitchen is not a studio flat


def test_a_negated_mention_does_not_hide_a_real_one():
    assert f("本單位非凶宅，但對面座曾有凶宅") == {"haunted"}


def test_evidence_is_the_matched_term_only():
    found = flags.find_flags("聯絡 PHONE_PLACEHOLDER 凶宅筍盤")
    assert found == {"haunted": "凶宅"}


def test_apply_flags_writes_risk_flag_rows(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_db(con)
    lst = Listing("hse28", "1", "sale", "u", features=["望海景"], text_for_flags="連租約 PHONE_X")
    lst2 = Listing("hse28", "2", "sale", "u", rooms=0, text_for_flags="")
    n = flags.apply_flags(con, [lst, lst2], datetime(2026, 10, 16))
    rows = con.execute(
        "SELECT listing_id, flag, evidence FROM risk_flag ORDER BY 1, 2").fetchall()
    assert rows == [("1", "sea_view", "海景"), ("1", "tenanted", "連租約"),
                    ("2", "studio", "rooms=0")]
    assert n == 3
    # re-reading the listing replaces its flags rather than adding copies
    flags.apply_flags(con, [lst], datetime(2026, 10, 17))
    assert con.execute("SELECT count(*) FROM risk_flag").fetchone()[0] == 3
