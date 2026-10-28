"""Linking 28Hse ids (h28-) to master ids (cd-): estate names, building names, shared units."""

from datetime import date

import pytest

from hkestate import db, link


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_db(c)
    link.ensure_tables(c)
    yield c
    c.close()


def add_master(con, code, name, *, parent=None, units=100, excluded=False, blocks=()):
    con.execute(
        "INSERT INTO master_estate (code, parent_code, name_zh, units, excluded, pilot) "
        "VALUES (?, ?, ?, ?, ?, false)", [code, parent, name, units, excluded])
    for bcode, phase, block in blocks:
        con.execute(
            "INSERT INTO master_building (building_id, estate_code, block_code, estate_name, "
            "phase_name, block_name) VALUES (?, ?, ?, ?, ?, ?)",
            [f"cd-{bcode}", code, bcode, name, phase, block])


def add_hse(con, hid, name, blocks=()):
    """A 28Hse estate in the index plus its grid (buildings with one flat each)."""
    con.execute("INSERT INTO hse28_estate (id, name, found_by) VALUES (?, ?, 'csv')", [hid, name])
    if not blocks:
        return
    eid = f"h28-{hid}"
    con.execute("INSERT INTO estate VALUES (?, ?, NULL, NULL, NULL, 'hse28', ?, now())",
                [eid, name.replace("-", " "), str(hid)])
    for n, (phase, block) in enumerate(blocks, 1):
        pid = f"{eid}-s{n}" if phase else None
        if phase:
            con.execute("INSERT INTO phase VALUES (?, ?, ?, ?)", [pid, eid, phase, str(n)])
        bid = f"{eid}-b{n}"
        con.execute(
            "INSERT INTO building (building_id, estate_id, phase_id, name_zh, source_key) "
            "VALUES (?, ?, ?, ?, ?)", [bid, eid, pid, block, str(n)])
        con.execute(
            "INSERT INTO unit (unit_id, building_id, floor_label, floor_num, floor_kind, flat, "
            "saleable_sqft) VALUES (?, ?, '12', 12, 'number', 'A', 500)", [f"{bid}|12|A", bid])


# ---- estates -------------------------------------------------------------------------------------


def test_exact_name_matches_across_spellings(con):
    add_master(con, "2-AAA", "太古城")
    add_master(con, "2-BBB", "海怡半島")
    add_hse(con, 1, "太古城")
    add_hse(con, 2, "海怡 半島")  # spacing differs
    counts = link.map_estates(con)
    assert counts == {"exact": 2}
    row = con.execute("SELECT master_codes, match_status FROM hse28_estate WHERE id = 2").fetchone()
    assert row == ("2-BBB", "exact")
    assert con.execute("SELECT master_id FROM source_map WHERE source_key = 'h28-1' "
                       "AND kind = 'estate'").fetchone() == ("cd-2-AAA",)


def test_phases_of_one_estate_all_match_a_whole_estate_name(con):
    add_master(con, "2-P1", "美孚新邨第一期", parent="3-MF", units=500)
    add_master(con, "2-P2", "美孚新邨第二期", parent="3-MF", units=700)
    add_master(con, "2-XX", "美孚新邨大廈", parent="3-OTHER")  # not a phase tail
    add_hse(con, 7, "美孚新邨")
    assert link.map_estates(con) == {"prefix": 1}
    row = con.execute("SELECT master_codes, size_flats FROM hse28_estate").fetchone()
    assert row == ("2-P1,2-P2", 1200)


def test_same_name_in_two_estates_is_ambiguous_and_not_mapped(con):
    add_master(con, "2-A", "海景花園", parent="3-A")
    add_master(con, "2-B", "海景花園", parent="3-B")
    add_hse(con, 9, "海景花園")
    assert link.map_estates(con) == {"ambiguous": 1}
    assert con.execute("SELECT count(*) FROM source_map").fetchone()[0] == 0


def test_unknown_name_is_none_and_excluded_estate_is_flagged(con):
    add_master(con, "2-H", "某某邨", excluded=True)
    add_hse(con, 1, "某某邨")
    add_hse(con, 2, "不存在苑")
    counts = link.map_estates(con)
    assert counts == {"exact": 1, "none": 1}
    assert con.execute("SELECT excluded FROM hse28_estate WHERE id = 1").fetchone() == (True,)
    assert con.execute("SELECT excluded FROM hse28_estate WHERE id = 2").fetchone() == (False,)


def test_size_counts_flats_and_centadata_deals(con):
    add_master(con, "2-A", "大苑", units=300)
    con.execute(
        "INSERT INTO txn_raw (source, source_id, fetched_at, row_hash, estate_id) "
        "VALUES ('centadata', 'x1', now(), 'h', 'cd-2-A'), "
        "('centadata', 'x2', now(), 'h', 'cd-2-A')")
    add_hse(con, 1, "大苑")
    link.map_estates(con)
    assert con.execute("SELECT size_flats, size_deals FROM hse28_estate").fetchone() == (300, 2)


# ---- buildings -----------------------------------------------------------------------------------


def test_buildings_map_by_block_name(con):
    add_master(con, "2-A", "海景苑", blocks=[("1-B1", "", "1座"), ("1-B2", "", "2座")])
    add_hse(con, 5, "海景苑", blocks=[("", "1座"), ("", "2座")])
    link.map_estates(con)
    stats = link.link_buildings(con, 5)
    assert stats["mapped"] == 2 and stats["ambiguous"] == 0
    got = dict(con.execute("SELECT source_key, master_id FROM source_map "
                           "WHERE kind = 'building'").fetchall())
    assert got == {"h28-5-b1": "cd-1-B1", "h28-5-b2": "cd-1-B2"}
    # the 28Hse flats now exist under the master building too
    assert con.execute("SELECT count(*) FROM unit WHERE unit_id = 'cd-1-B1|12|A'"
                       ).fetchone()[0] == 1


def test_single_building_estates_map_without_names(con):
    add_master(con, "2-S", "單棟閣", blocks=[("1-S1", "", "單棟閣")])
    add_hse(con, 3, "單棟閣", blocks=[("", "A座")])
    link.map_estates(con)
    assert link.link_buildings(con, 3)["mapped"] == 1


def test_manual_mapping_is_kept(con):
    add_master(con, "2-A", "海景苑", blocks=[("1-B1", "", "1座")])
    add_hse(con, 5, "海景苑", blocks=[("", "1座")])
    con.execute("INSERT INTO source_map VALUES ('hse28', 'building', 'h28-5-b1', 'cd-OTHER', "
                "'', 'manual', now())")
    link.map_estates(con)
    link.link_buildings(con, 5)
    assert con.execute("SELECT master_id FROM source_map WHERE source_key = 'h28-5-b1'"
                       ).fetchone() == ("cd-OTHER",)


def test_hse_building_claimed_by_two_master_buildings_is_ambiguous(con):
    # a block-less 28Hse estate (one building) against a master estate with two buildings
    add_master(con, "2-A", "雙塔苑", blocks=[("1-B1", "", "1座"), ("1-B2", "", "2座")])
    add_hse(con, 5, "雙塔苑", blocks=[("", "大廈")])
    link.map_estates(con)
    stats = link.link_buildings(con, 5)
    assert stats["mapped"] == 0 and stats["ambiguous"] == 1


# ---- deals land on one unit ----------------------------------------------------------------------


def deal(con, source, sid, bid, day, price, *, dtype="registry", floor="12", flat="A"):
    con.execute(
        "INSERT INTO txn_raw (source, source_id, fetched_at, row_hash, building_id, "
        "floor_label, flat, deal_date, deal_type, price_hkd, saleable_sqft) "
        "VALUES (?, ?, now(), ?, ?, ?, ?, ?, ?, ?, 500)",
        [source, sid, sid, bid, floor, flat, day, dtype, price])


def test_mapped_28hse_and_centadata_deals_share_a_unit_and_dedupe(con):
    add_master(con, "2-A", "海景苑", blocks=[("1-B1", "", "1座")])
    add_hse(con, 5, "海景苑", blocks=[("", "1座")])
    link.map_estates(con)
    link.link_buildings(con, 5)
    deal(con, "hse28", "h1", "h28-5-b1", date(2024, 3, 10), 6_000_000)
    deal(con, "centadata", "c1", "cd-1-B1", date(2024, 3, 1), 6_000_000)  # same sale
    deal(con, "centadata", "c0", "cd-1-B1", date(2015, 5, 1), 4_000_000)  # pre-2018, only here
    rows = con.execute("SELECT source_id, unit_id, building_id, excl_duplicate FROM txn_all "
                       "ORDER BY source_id").fetchall()
    units = {r[1] for r in rows}
    assert units == {"cd-1-B1|12|A"}
    dup = {r[0]: r[3] for r in rows}
    assert dup == {"c0": False, "c1": True, "h1": False}  # Land Registry copy wins
    assert con.execute("SELECT floor_num FROM txn_all WHERE source_id = 'h1'").fetchone() == (12,)


def test_unmapped_28hse_building_keeps_its_own_id(con):
    deal(con, "hse28", "h1", "h28-9-b1", date(2024, 3, 10), 6_000_000)
    assert con.execute("SELECT building_id FROM txn_all").fetchone() == ("h28-9-b1",)


# ---- pilot ---------------------------------------------------------------------------------------


def test_pilot_link_and_shared_unit_report(con):
    bid = "h28-1140-s1-b18"
    con.execute("INSERT INTO estate VALUES ('h28-1140', '太古城', NULL, NULL, 'taikoo-shing', "
                "'hse28', '1140', now())")
    con.execute("INSERT INTO building (building_id, estate_id, name_zh) VALUES (?, 'h28-1140', "
                "'某閣')", [bid])
    deal(con, "hse28", "h1", bid, date(2024, 3, 10), 6_000_000)
    deal(con, "centadata", "c1", bid, date(2024, 3, 1), 6_050_000)
    deal(con, "centadata", "c2", bid, date(2012, 3, 1), 3_000_000, flat="B")
    con.execute("UPDATE txn_raw SET estate_id = 'h28-1140', "
                "url = 'https://x/centadata-details/n_1-ABCDEFGHIJ' WHERE source = 'centadata'")
    stats = link.link_pilot(con)
    assert stats == {"hse28_buildings": 1, "centadata_blocks": 1}
    assert con.execute("SELECT master_id FROM source_map WHERE source = 'centadata'"
                       ).fetchone() == (bid,)
    report = link.shared_unit_report(con)
    assert report == [("1140", 1, 2, 1)]  # one flat has both sources, one is Centadata-only


# ---- estate subsets (`--estates`, test rebuilds) -------------------------------------------------


def test_resolve_estates_names_codes_big_estates_and_pilot_groups(con):
    add_master(con, "2-P1", "美孚新邨第一期", parent="3-MF")
    add_master(con, "2-P2", "美孚新邨第二期", parent="3-MF")
    add_master(con, "2-YY", "爾巒")
    add_master(con, "2-ZZ", "爾巒軒")  # another estate whose name starts the same: not a phase
    assert link.resolve_estates(con, ["爾巒"]) == {"2-YY"}
    assert link.resolve_estates(con, ["美孚新邨第一期"]) == {"2-P1"}
    assert link.resolve_estates(con, ["3-MF"]) == {"2-P1", "2-P2"}
    assert link.resolve_estates(con, ["2-ZZ", " "]) == {"2-ZZ"}
    assert link.resolve_estates(con, ["太古城", "taikoo-shing"]) == {"taikoo-shing"}
    assert link.resolve_estates(con, ["不存在"]) == set()
    assert "lohas-park" in link.resolve_estates(con, ["pilot"])


def test_subset_building_ids_cover_master_28hse_and_pilot_buildings(con):
    add_master(con, "2-AAA", "測試苑", blocks=[("1-B1", "", "1座")])
    add_master(con, "2-BBB", "別處", blocks=[("1-C1", "", "1座")])
    add_hse(con, 5, "測試苑", blocks=[("", "1座")])
    link.map_estates(con)
    con.execute("INSERT INTO estate VALUES ('h28-1140', '太古城', NULL, NULL, 'taikoo-shing', "
                "'hse28', '1140', now())")
    con.execute("INSERT INTO building (building_id, estate_id, name_zh) "
                "VALUES ('h28-1140-b1', 'h28-1140', '某閣')")
    assert link.subset_building_ids(con, {"2-AAA"}) == {"cd-1-B1", "h28-5-b1"}
    assert link.subset_building_ids(con, {"taikoo-shing"}) == {"h28-1140-b1"}
    assert link.subset_building_ids(con, set()) == set()


def test_subset_names_match_phases_and_whole_estates(con):
    add_master(con, "2-P1", "日出康城第1期", parent="3-LP")
    keys = link.subset_name_keys(con, {"2-P1", "mei-foo"})
    assert link.names_match("日出康城", keys)  # the whole estate of a subset phase
    assert link.names_match("美孚新邨", keys)
    assert not link.names_match("太古城", keys)
