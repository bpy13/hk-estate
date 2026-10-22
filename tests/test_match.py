"""Matching listings to master buildings, floor bands and cross-site duplicates."""

from hkestate import match
from hkestate.match import Building

TKS = [
    Building("tks-48", "太古城", "星輝台", "海星閣", 26),
    Building("tks-27", "太古城", "安盛台", "寧安閣", 27),
    Building("tks-39", "太古城", "海景花園(東)", "美菊閣", 28),
    Building("tks-63", "太古城", "海天花園", "景天閣", 30),
]
CITY_ONE = [
    Building("c1-21", "沙田第一城", "2期", "21座", 34),
    Building("c1-2", "沙田第一城", "1期", "2座", 34),
]
LOHAS = [
    Building("lp-ld1", "領都", "", "第1座", 60),
    Building("lp-mb1", "MALIBU", "", "第1A座", 50),
    Building("lp-gm1", "Grand Montara", "", "第1座", 55),
]


def m(text, buildings, **kw):
    return match.match_building(text, buildings, **kw)


def test_exact_block_name():
    assert m("太古城 星輝台 海星閣 (48座)", TKS) == ("tks-48", "exact")
    assert m("安盛台 寧安閣", TKS) == ("tks-27", "exact")


def test_simplified_chinese_and_variants():
    assert m("太古城 安盛台 宁安阁", TKS) == ("tks-27", "exact")


def test_centanet_slug_style_and_missing_spaces():
    assert m("太古城-海景花園-東-美菊閣-39座", TKS)[0] == "tks-39"
    bid, method = m("太古城海天花園景天閣(63座)", TKS)
    assert bid == "tks-63" and method in ("contains", "fuzzy")


def test_name_fuzz_for_a_near_miss():
    # one character off (海天花園 景大閣): close enough to only one building
    bid, method = m("太古城 海天花園 景大閣", TKS)
    assert (bid, method) == ("tks-63", "fuzzy")


def test_unrelated_name_does_not_match():
    assert m("康怡花園 A座", TKS) == (None, "none")


def test_numbered_blocks_and_ordinal_prefix():
    assert m("沙田第一城 2期 21座", CITY_ONE) == ("c1-21", "exact")
    assert m("沙田第一城 第2座", CITY_ONE) == ("c1-2", "exact")


def test_same_block_name_in_several_estates_needs_the_estate():
    assert m("日出康城 領都 1座", LOHAS) == ("lp-ld1", "exact")
    assert m("GRAND MONTARA 第1座", LOHAS) == ("lp-gm1", "exact")
    assert m("日出康城 1座", LOHAS) == (None, "ambiguous")


def test_tower_with_wing_in_brackets():
    # Centanet: '7期A Montara 2座 (2A)'; 28Hse block: '第2A座'
    blds = [
        Building("m2a", "MONTARA", "", "第2A座", 50),
        Building("m2b", "MONTARA", "", "第2B座", 50),
        Building("x2a", "MALIBU", "", "第2A座", 50),
    ]
    assert m("日出康城 7期A Montara 2座 (2A)", blds) == ("m2a", "exact")


MEI_FOO = [
    Building("mf-19", "美孚新邨", "第一期", "百老匯街19號", 14),
    Building("mf-21", "美孚新邨", "第一期", "百老匯街21號", 14),
    Building("mf-23", "美孚新邨", "第一期", "百老匯街23號", 14),
    Building("mf-l1", "美孚新邨", "第五期", "蘭秀街1號", 20),
    Building("mf-l3", "美孚新邨", "第五期", "蘭秀街3號", 20),
]


def test_address_range_is_one_physical_block():
    # Centanet names Mei Foo blocks by address range; 28Hse has a building per number
    assert m("美孚新邨 1期 百老匯街19-21號", MEI_FOO) == ("mf-19", "range")
    assert m("美孚新邨 5期 蘭秀道1-3號", MEI_FOO) == ("mf-l1", "range")  # 道 = 街
    assert m("美孚新邨 1期 百老匯街23號", MEI_FOO) == ("mf-23", "exact")


def test_phase_only_is_not_enough():
    assert m("太古城 海天花園", TKS)[0] is None


def test_single_building_estate():
    assert m("高雲臺", [Building("gh", "高雲台", "", "高雲台", 41)]) == ("gh", "single")


YIULUNG = [Building(f"yl-{n}", "逸瓏", "", f"{n}座", 10) for n in (1, 2, 3, 5, 6, 7)]


def test_text_continuing_the_estate_name_is_no_match():
    # 逸瓏灣 (Tai Po) is not 逸瓏 (Ho Man Tin); the block '5座' also sits inside '15座'
    assert m("逸瓏灣 II 大廈15座", YIULUNG)[0] is None
    assert m("逸瓏灣 I 大廈17座", YIULUNG)[0] is None
    assert m("逸瓏5座", YIULUNG)[0] == "yl-5"
    assert m("逸瓏 5座", YIULUNG)[0] == "yl-5"
    # an estate name followed by its phase is still fine (no spaces, Centanet slug style)
    assert m("太古城海天花園景天閣", TKS)[0] == "tks-63"


def test_block_the_estate_lacks_needs_a_check():
    one = [Building("fs", "豐盛苑", "", "豐盛苑", 30)]
    assert m("豐盛苑 榮盛閣 (A座)", one) == (None, "check_match")
    assert m("豐盛苑", one) == ("fs", "single")
    ladder = [Building("hy-a", "慧雅苑", "", "慧雅閣", 20),
              Building("hy-b", "慧雅苑", "", "慧安閣", 20)]
    assert m("慧雅苑慧雅閣B座", ladder) == ("hy-a", "contains")  # B座 is the wing of the block
    assert m("慧雅苑慧雅閣 富盛閣", ladder) == (None, "check_match")


def test_manual_map_wins():
    manual = {("centanet", match.norm_key("太古城 怪名")): "tks-48"}
    assert m("太古城 怪名", TKS, source="centanet", manual=manual) == ("tks-48", "manual")


def test_floor_bands():
    fb = match.floor_band
    assert fb("高層", None) == "high"
    assert fb("中層", 30) == "mid"
    assert fb("低層", 30) == "low"
    assert fb("25樓", 30) == "high"
    assert fb("15", 30) == "mid"
    assert fb("3樓", 30) == "low"
    assert fb("G", 30) == "low"
    assert fb("25樓", None) == "unknown"  # no building height: can't place a number
    assert fb(None, 30) == "unknown"
    assert fb("高層", 30, floor_range="20-30/30") == "high"
    assert fb(None, 30, floor_range="1-10/30") == "low"


def test_cross_site_dedupe():
    rows = [
        {"source": "centanet", "listing_id": "a", "kind": "sale", "building_id": "b1",
         "floor_band": "high", "saleable_sqft": 580},
        {"source": "hse28", "listing_id": "b", "kind": "sale", "building_id": "b1",
         "floor_band": "high", "saleable_sqft": 590},  # +1.7%: same flat
        {"source": "hse28", "listing_id": "c", "kind": "sale", "building_id": "b1",
         "floor_band": "high", "saleable_sqft": 650},  # different size
        {"source": "spacious", "listing_id": "d", "kind": "sale", "building_id": "b1",
         "floor_band": "unknown", "saleable_sqft": 581},  # no floor: joins the only fit
        {"source": "centanet", "listing_id": "e", "kind": "sale", "building_id": "b1",
         "floor_band": "high", "saleable_sqft": 580},  # same site twice: kept apart
        {"source": "hse28", "listing_id": "f", "kind": "rent", "building_id": "b1",
         "floor_band": "high", "saleable_sqft": 580},  # a rental is not a sale
        {"source": "hse28", "listing_id": "g", "kind": "sale", "building_id": "b1",
         "floor_band": "low", "saleable_sqft": 580},  # other band
    ]
    groups = match.dedupe(rows)
    g = {k[1]: v for k, v in groups.items()}
    assert g["a"] == g["b"]
    assert len({g["a"], g["c"], g["e"], g["f"], g["g"]}) == 5
    # d fits both a's group and e's group (same building, area): ambiguous, left alone
    assert g["d"] not in (g["a"], g["e"])


def test_spacious_joins_a_unique_group():
    rows = [
        {"source": "centanet", "listing_id": "a", "kind": "sale", "building_id": "b1",
         "floor_band": "mid", "saleable_sqft": 700},
        {"source": "spacious", "listing_id": "d", "kind": "sale", "building_id": "b1",
         "floor_band": "unknown", "saleable_sqft": 705},
    ]
    g = match.dedupe(rows)
    assert g[("centanet", "a")] == g[("spacious", "d")]
