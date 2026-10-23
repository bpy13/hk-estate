"""Normalising floor labels, names, prices and areas from Chinese page text."""

import pytest

from hkestate import normalise as n


@pytest.mark.parametrize(
    ("label", "num", "kind"),
    [
        ("19", 19, "number"),
        ("19樓", 19, "number"),
        ("19/F", 19, "number"),
        ("G", 0, "ground"),
        ("G/F", 0, "ground"),
        ("地下", 0, "ground"),
        ("UG", 0, "ground"),
        ("LG", -1, "basement"),
        ("B1", -1, "basement"),
        ("平台", 0, "podium"),
        ("PODIUM", 0, "podium"),
        ("頂層", None, "top"),
        ("頂樓", None, "top"),
        ("PH", None, "top"),
        ("高層", None, "band"),
        ("中層", None, "band"),
        ("低層", None, "band"),
        ("", None, "unknown"),
        ("天台特色戶?", None, "unknown"),
        # Chinese numerals (Centadata's older buildings: 五樓, 十二樓, 二十三樓)
        ("一", 1, "number"),
        ("五", 5, "number"),
        ("十", 10, "number"),
        ("十二", 12, "number"),
        ("二十", 20, "number"),
        ("二十三", 23, "number"),
        ("三十", 30, "number"),
        ("三十七", 37, "number"),
        ("廿三", 23, "number"),
        ("十二樓", 12, "number"),
        # forms that are not one numbered floor stay as they were
        ("閣", None, "unknown"),
        ("13號", None, "unknown"),
        ("三五", None, "unknown"),
    ],
)
def test_parse_floor(label, num, kind):
    info = n.parse_floor(label)
    assert (info.num, info.kind) == (num, kind)


@pytest.mark.parametrize(
    "label, num, kind",
    [
        # the same floor written twice (SRPE registers)
        ("3樓 3", 3, "number"), ("18 樓 18", 18, "number"), ("地下 G", 0, "ground"),
        ("地面層 G", 0, "ground"),
        # two-storey flats: the lower floor
        ("2-3", 2, "number"), ("15 -16", 15, "number"), ("5F - 6", 5, "number"),
        ("G-1", 0, "ground"), ("地下-1", 0, "ground"), ("地下至1", 0, "ground"),
        ("地下及1", 0, "ground"), ("G&1", 0, "ground"), ("5&6", 5, "number"),
        ("3F&5", 3, "number"), ("20F & R", 20, "number"), ("二-三", 2, "number"),
        ("二十六樓-二十七", 26, "number"), ("地下-二", 0, "ground"),
        # ground and basement written another way
        ("高層地下", 0, "ground"), ("低層地下", 0, "ground"), ("地下2", 0, "ground"),
        ("地庫", -1, "basement"),
        # left unread: houses, blocks, mezzanine, ambiguous or mismatched forms
        ("13號", None, "unknown"), ("3號屋", None, "unknown"), ("3座", None, "unknown"),
        ("閣", None, "unknown"), ("G2", None, "unknown"), ("3樓 5", None, "unknown"),
        ("--", None, "unknown"), ("B-1", None, "unknown"),
    ],
)
def test_compound_floor_labels(label, num, kind):
    info = n.parse_floor(label)
    assert (info.num, info.kind) == (num, kind)
    # the stored label is the source's text, so existing unit keys don't change
    assert info.label == label.upper().strip()


def test_floor_label_is_cleaned_but_kept_as_text():
    assert n.parse_floor(" 19樓 ").label == "19"
    assert n.parse_floor("g/f").label == "G"
    assert n.parse_floor("地下").label == "G"
    assert n.parse_floor("高層").label == "高層"


def test_chinese_numeral_label_is_kept_as_text():
    # the unit key is building|floor label|flat: stored units are keyed '五', not '5', and a
    # re-scrape must land on the same unit
    assert n.parse_floor("五樓").label == "五"
    assert n.parse_floor("十二").label == "十二"
    assert n.infer_skipped(["一", "二", "三", "五", "六"]) == {4}


def test_infer_skipped_floors_from_labels_seen():
    labels = [str(i) for i in range(1, 31) if i not in (4, 13, 14, 24)]
    assert n.infer_skipped(labels) == {4, 13, 14, 24}


def test_infer_skipped_ignores_floors_above_the_top():
    labels = [str(i) for i in range(1, 12) if i != 4]
    assert n.infer_skipped(labels) == {4}  # 13/14/24 are above the top floor, not skipped


def test_physical_floor_counts_skipped_labels_below():
    skipped = {4, 13, 14, 24}
    assert n.physical_floor(3, skipped) == 3
    assert n.physical_floor(5, skipped) == 4
    assert n.physical_floor(15, skipped) == 12
    assert n.physical_floor(25, skipped) == 21
    assert n.physical_floor(0, skipped) == 0
    assert n.physical_floor(None, skipped) is None


def test_top_floor_resolves_above_highest_numbered_floor():
    assert n.resolve_top_floor([1, 2, 30, 31]) == 32
    assert n.resolve_top_floor([]) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("太古城", "太古城"),
        ("太古城 ", "太古城"),
        ("沙田第一城", "沙田第一城"),
        ("美孚新邨", "美孚新邨"),
        ("日出康城", "日出康城"),
        ("太古城  安盛台", "太古城 安盛台"),
        ("ＡＢＣ　大厦", "ABC 大廈"),  # full-width to half-width, simplified to traditional
        ("沙田第一城", "沙田第一城"),
    ],
)
def test_to_traditional_normalises_names(text, expected):
    assert n.norm_name(text) == expected


def test_simplified_and_traditional_names_compare_equal():
    assert n.norm_name("太古城 星辉台") == n.norm_name("太古城 星輝台")


@pytest.mark.parametrize(
    ("text", "hkd"),
    [
        ("$875 萬元", 8_750_000),
        ("$1,043 萬元", 10_430_000),
        ("$1,380萬", 13_800_000),
        ("$875.5萬", 8_755_000),
        ("$1.2億", 120_000_000),
        ("$1億2,300萬", 123_000_000),
        ("$8,750,000", 8_750_000),
        ("HK$ 520 萬", 5_200_000),
        ("-", None),
        ("", None),
    ],
)
def test_parse_price(text, hkd):
    assert n.parse_price_hkd(text) == hkd


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("實530呎", ("saleable", 530.0)),
        ("實用 530 呎", ("saleable", 530.0)),
        ("建628呎", ("gross", 628.0)),
        ("建築 1,028呎", ("gross", 1028.0)),
        ("580呎", (None, 580.0)),
        ("--", (None, None)),
    ],
)
def test_parse_area(text, expected):
    assert n.parse_area(text) == expected


def test_resolve_area_bases():
    assert n.resolve_area(500.0, 640.0, 0.8) == (500.0, "saleable")
    assert n.resolve_area(None, 600.0, 0.8) == (pytest.approx(480.0), "converted")
    assert n.resolve_area(None, 600.0, None) == (600.0, "gross_only")
    assert n.resolve_area(None, None, 0.8) == (None, None)


def test_saleable_to_gross_ratio_is_median_of_units_with_both():
    pairs = [(530, 628), (580, 690), (703, 840), (None, 700), (500, None)]
    assert n.saleable_to_gross(pairs) == pytest.approx(580 / 690)
    assert n.saleable_to_gross([(None, 700)]) is None


@pytest.mark.parametrize(
    ("text", "rooms"), [("2房", 2), ("3 房", 3), ("開放式", 0), ("", None), ("車位", None)]
)
def test_parse_rooms(text, rooms):
    assert n.parse_rooms(text) == rooms
