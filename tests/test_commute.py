"""Commute estimate: hand-worked journeys on a small piece of the real MTR file."""
import pytest

from hkestate import commute

HEADER = ('"Line Code","Direction","Station Code","Station ID","Chinese Name",'
          '"English Name","Sequence"\n')


def _rows(line, direction, stops):
    return "".join(f'"{line}","{direction}","{c}","0","x","{c}",{i:.2f}\n' for i, c in stops)


# East Rail: Kowloon Tong .. Admiralty (4 stops); Kwun Tong line: Shek Kip Mei - Kowloon Tong
# - Mong Kok; Tsuen Wan line: Mong Kok - Admiralty (4 stops, via TST). Airport Express is ignored.
CSV = HEADER + (
    _rows("EAL", "DT", [(10, "KOT"), (11, "MKK"), (12, "HUH"), (13, "EXC"), (14, "ADM")])
    + _rows("EAL", "UT", [(1, "ADM"), (2, "EXC"), (3, "HUH"), (4, "MKK"), (5, "KOT")])
    + _rows("KTL", "UT", [(4, "MOK"), (5, "PRE"), (6, "SKM"), (7, "KOT")])
    + _rows("TWL", "UT", [(2, "ADM"), (3, "TST"), (4, "JOR"), (5, "YMT"), (6, "MOK")])
    + _rows("AEL", "DT", [(1, "KOT"), (2, "ADM")])  # must be ignored
)
G = commute.build_graph(CSV)


def test_kowloon_tong_to_admiralty_direct():
    # East Rail KOT -> MKK -> HUH -> EXC -> ADM = 4 stops x 2 min, no change
    assert commute.train_minutes(G, "KOT", "ADM") == 8


def test_line_change_is_counted():
    # Shek Kip Mei -> Kowloon Tong (1 stop), change to East Rail (+5), 4 stops:
    # 2 + 5 + 8 = 15. Via Mong Kok and the Tsuen Wan line would be 2 stops + 5 + 4 stops = 17.
    assert commute.train_minutes(G, "SKM", "ADM") == 15


def test_airport_express_is_ignored():
    assert ("KOT", "AEL") not in G


def test_unknown_station_has_no_route():
    assert commute.train_minutes(G, "KOT", "ZZZ") is None


def test_walk_minutes_is_straight_line_times_1_3_at_80_m_a_minute():
    # 0.01 degrees of latitude is about 1112 m
    w = commute.walk_minutes(22.30, 114.17, ("KOT", 22.31, 114.17))
    assert w == pytest.approx(1111.9 * 1.3 / 80, rel=0.01)


def test_minutes_takes_best_of_two_nearest_stations():
    stations = [("KOT", 22.3369, 114.1762), ("SKM", 22.3315, 114.1688), ("ADM", 22.2793, 114.1646)]
    # a flat right beside Kowloon Tong: walk ~0 + 8 min train
    m = commute.minutes(22.3369, 114.1762, "ADM", stations, G)
    assert m == pytest.approx(8, abs=0.5)
    # a flat beside Shek Kip Mei: SKM route costs 15, the walk to KOT (~1.1 km) + 8 is similar;
    # the better of the two wins
    m2 = commute.minutes(22.3315, 114.1688, "ADM", stations, G)
    walk_kot = commute.walk_minutes(22.3315, 114.1688, stations[0])
    assert m2 == pytest.approx(min(15, walk_kot + 8), abs=0.5)


# ---- distance-based times (Task 14b) ------------------------------------------------------------

STEP_M = 1111.9  # 0.01 degrees of latitude
# the East Rail stations in a straight line, 0.01 degrees apart
LINE_COORDS = {c: (22.30 + 0.01 * i, 114.17)
               for i, c in enumerate(["KOT", "MKK", "HUH", "EXC", "ADM"])}


def test_with_coordinates_each_stop_is_half_a_minute_plus_distance_over_speed():
    g = commute.build_graph(CSV, LINE_COORDS)
    per_stop = commute.DWELL_MIN + STEP_M / commute.SPEED_M_PER_MIN
    assert commute.train_minutes(g, "KOT", "ADM") == pytest.approx(4 * per_stop, rel=0.01)
    assert commute.DWELL_MIN == 0.5


def test_missing_coordinates_fall_back_to_two_minutes_a_stop():
    partial = {k: v for k, v in LINE_COORDS.items() if k != "HUH"}  # HUH has no position
    g = commute.build_graph(CSV, partial)
    per_stop = commute.DWELL_MIN + STEP_M / commute.SPEED_M_PER_MIN
    # KOT-MKK has coordinates; MKK-HUH, HUH-EXC fall back to 2; EXC-ADM has coordinates
    assert commute.train_minutes(g, "KOT", "ADM") == pytest.approx(2 * per_stop + 4, rel=0.01)


def test_no_coordinates_is_the_stop_count_estimate():
    assert commute.train_minutes(commute.build_graph(CSV, None), "KOT", "ADM") == 8


def test_average_speed_is_a_plausible_train_speed():
    assert 600 <= commute.SPEED_M_PER_MIN <= 1200  # 36-72 km/h between stations


WALK_CSV = HEADER + (
    _rows("TCL", "DT", [(1, "TST"), (2, "HOK")])
    + _rows("TWL", "UT", [(1, "CEN"), (2, "ADM")])
    + _rows("TML", "UT", [(1, "ETS"), (2, "HUH")])
    + _rows("EAL", "UT", [(1, "TST"), (2, "MKK")])
)


def test_walking_link_hong_kong_to_central_is_five_minutes():
    g = commute.build_graph(WALK_CSV)
    # HOK -> CEN walk 5, then one stop CEN -> ADM at the 2-minute fallback, no change cost
    assert commute.train_minutes(g, "HOK", "ADM") == 5 + 2
    assert commute.train_minutes(g, "ADM", "HOK") == 5 + 2


def test_walking_link_tsim_sha_tsui_to_east_tsim_sha_tsui_is_four_minutes():
    g = commute.build_graph(WALK_CSV)
    assert commute.train_minutes(g, "TST", "HUH") == 4 + 2


def test_walking_link_needs_both_stations():
    g = commute.build_graph(HEADER + _rows("TCL", "DT", [(1, "HOK"), (2, "KOW")]))
    assert commute.train_minutes(g, "HOK", "CEN") is None
