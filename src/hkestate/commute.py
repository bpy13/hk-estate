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

MIN_PER_STOP = 2.0
MIN_PER_CHANGE = 5.0
WALK_DETOUR = 1.3        # streets are longer than the straight line
WALK_M_PER_MIN = 80.0
EXCLUDED_LINES = {"AEL"}
NEAREST_STATIONS = 2

# graph: node (station code, line code) -> list of neighbour nodes (one stop apart)
Graph = dict[tuple[str, str], set[tuple[str, str]]]


def build_graph(csv_text: str) -> Graph:
    """Line graph from the open-data CSV text. Each (station, line) is a node; stops that are
    next to each other on a line (in any direction/branch sequence) are joined. Changing line
    at a station is handled in the search, because it costs minutes rather than stops."""
    rows: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
    for r in csv.DictReader(io.StringIO(csv_text.lstrip("﻿"))):
        line, code = r["Line Code"], r["Station Code"]
        if line in EXCLUDED_LINES or not code:
            continue
        rows[(line, r["Direction"])].append((float(r["Sequence"]), code))
    graph: Graph = defaultdict(set)
    for (line, _direction), stops in rows.items():
        stops.sort()
        for (_, a), (_, b) in zip(stops, stops[1:], strict=False):
            if a != b:
                graph[(a, line)].add((b, line))
                graph[(b, line)].add((a, line))
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
        for nxt in graph[node]:
            if nxt not in best:
                heapq.heappush(heap, (cost + MIN_PER_STOP, nxt))
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
