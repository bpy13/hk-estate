"""Rough commute time from a building to an MTR station (e.g. Central or Admiralty).

An estimate, not a timetable: MTR open data has no station-to-station journey times, so
train time = stops x 2 min + 5 min for each change of line, over a graph built from the open
data file `mtr_lines_and_stations.csv` (the same file geo.py loads). Walk time = straight
line x 1.3 at 80 m a minute, from the 2 nearest stations to the building.

Left out on purpose: Airport Express (not a commuter line), and Light Rail (not in the file).
"""
from __future__ import annotations

import csv
import heapq
import io
from collections import defaultdict

from hkestate.geo import haversine_m

MIN_PER_STOP = 2.0       # fallback when a station has no coordinates
# Minutes per stop = DWELL_MIN + straight-line distance / SPEED_M_PER_MIN, the same two
# numbers for every line. They are not physical (1700 m/min is 100 km/h): the dwell stands
# in for slow-downs, doors and acceleration, and the pair was fitted together to rough
# target journeys: Kowloon Tong-Admiralty 9-11, Sham Shui Po-Admiralty 12-15,
# Tsim Sha Tsui-Central 5-7, Tuen Mun-Admiralty 42-50, Tseung Kwan O-Central 22-28. At
# 1.6 / 1700 they come out 10.9, 13.2, 5.0 (4.97, the one miss, by 0.03), 42.0, 27.9; no
# pair on a fine grid meets all five.
DWELL_MIN = 1.6
SPEED_M_PER_MIN = 1700.0
MIN_PER_CHANGE = 5.0
# Footpaths between stations that the open data treats as separate: (a, b, walk minutes).
# Hong Kong and Central are linked by a long walkway; TST and East TST by a subway.
WALK_LINKS = [("HOK", "CEN", 5.0), ("TST", "ETS", 4.0)]
WALK_DETOUR = 1.3        # streets are longer than the straight line
WALK_M_PER_MIN = 80.0
EXCLUDED_LINES = {"AEL"}
NEAREST_STATIONS = 2

# graph: node (station code, line code) -> list of neighbour nodes (one stop apart)
Graph = dict[tuple[str, str], dict[tuple[str, str], float]]


def build_graph(csv_text: str, coords: dict[str, tuple[float, float]] | None = None) -> Graph:
    """Line graph from the open-data CSV text. Each (station, line) is a node; stops that are
    next to each other on a line (in any direction/branch sequence) are joined, with the
    minutes between them: dwell + straight-line distance / speed when `coords` ({station code:
    (lat, lon)}) has both stations, else MIN_PER_STOP. Changing line at a station is handled
    in the search, because it costs minutes rather than stops. Walking links join stations
    the file does not connect (no line-change cost)."""
    rows: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
    for r in csv.DictReader(io.StringIO(csv_text.lstrip("﻿"))):
        line, code = r["Line Code"], r["Station Code"]
        if line in EXCLUDED_LINES or not code:
            continue
        rows[(line, r["Direction"])].append((float(r["Sequence"]), code))
    coords = coords or {}
    graph: Graph = defaultdict(dict)
    for (line, _direction), stops in rows.items():
        stops.sort()
        for (_, a), (_, b) in zip(stops, stops[1:], strict=False):
            if a != b:
                if a in coords and b in coords:
                    d = haversine_m(*coords[a], *coords[b])
                    mins = DWELL_MIN + d / SPEED_M_PER_MIN
                else:
                    mins = MIN_PER_STOP
                graph[(a, line)][(b, line)] = mins
                graph[(b, line)][(a, line)] = mins
    by_station: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for node in list(graph):
        by_station[node[0]].append(node)
    for a, b, walk in WALK_LINKS:
        for na in by_station.get(a, []):
            for nb in by_station.get(b, []):
                graph[na][nb] = walk
                graph[nb][na] = walk
    return dict(graph)


def train_minutes(graph: Graph, origin: str, dest: str) -> float | None:
    """Cheapest train time between two station codes, or None if no path (or unknown)."""
    if origin == dest:
        return 0.0
    by_station: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for node in graph:
        by_station[node[0]].append(node)
    if origin not in by_station or dest not in by_station:
        return None
    best: dict[tuple[str, str], float] = {}
    heap = [(0.0, node) for node in by_station[origin]]  # start on any line, no change cost
    heapq.heapify(heap)
    while heap:
        cost, node = heapq.heappop(heap)
        if node in best:
            continue
        best[node] = cost
        if node[0] == dest:
            return cost
        for nxt, step in graph[node].items():
            if nxt not in best:
                heapq.heappush(heap, (cost + step, nxt))
        for other in by_station[node[0]]:  # change line at this station
            if other[1] != node[1] and other not in best:
                heapq.heappush(heap, (cost + MIN_PER_CHANGE, other))
    return None


def walk_minutes(lat: float, lon: float, station: tuple[str, float, float]) -> float:
    """Walk to a station given as (code, lat, lon)."""
    _, s_lat, s_lon = station
    return haversine_m(lat, lon, s_lat, s_lon) * WALK_DETOUR / WALK_M_PER_MIN


def minutes(lat: float, lon: float, dest_station: str,
            stations: list[tuple[str, float, float]], graph: Graph) -> float | None:
    """Door-to-station minutes: for the 2 nearest stations, walk + train to `dest_station`
    (a station code); the best of them. None if no route."""
    nearest = sorted(stations, key=lambda s: haversine_m(lat, lon, s[1], s[2]))
    best = None
    for st in nearest[:NEAREST_STATIONS]:
        train = train_minutes(graph, st[0], dest_station)
        if train is None:
            continue
        total = walk_minutes(lat, lon, st) + train
        if best is None or total < best:
            best = total
    return best
