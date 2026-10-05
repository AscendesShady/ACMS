"""Accessibility: what people can reach on the network, not how fast they drive (no Qt).

Levine, From Mobility to Accessibility (2019, Eq 3.1 and Appendix A) and Rodrigue, The Geography of Transport Systems
(accessibility measures): from each junction, by the shortest travel time on the roads (free flow at the speed limits,
or a run's measured link times),
- cumulative opportunities: the jobs (every building's employees), school places (schools' students), hospitals and
  shops within each time budget;
- potential accessibility (Hansen 1959): A_i = Σ_j W_j · exp(−β · t_ij), β per minute;
and for the residents (the Residential buildings, at their door junction): the people-weighted means, the minutes to
the nearest hospital and school, and how evenly jobs access is shared (Gini, weighted by residents). With a road
closed, the same again and each measure's loss (Taylor, Vulnerability Analysis for Transportation: accessibility
loss).
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from math import exp

from .institutions import door
from .models import Network

KINDS = (("jobs", None), ("school places", "School"), ("hospitals", "Hospital"), ("shops", "Shop"))


def link_minutes(network: Network, measured: dict | None = None, closed: str | None = None) -> dict:
    """node -> [(next node, minutes)] along each road's directions (one-way roads kept): the speed limit's time, or
    `measured` seconds a link ("road>" from its from-node, "road<" back) where given; `closed` road left out."""
    found: dict = defaultdict(list)
    for road in network.all_roads():
        if road.id == closed:
            continue
        free = road.length_m / max(road.speed_limit_kmh, 1) * 3.6 / 60
        found[road.from_node].append((road.to_node, (measured or {}).get(f"{road.id}>", free * 60) / 60))
        if road.direction == "two-way":
            found[road.to_node].append((road.from_node, (measured or {}).get(f"{road.id}<", free * 60) / 60))
    return found


def minutes_from(graph: dict, source: str) -> dict[str, float]:
    best, todo = {source: 0.0}, [(0.0, source)]
    while todo:
        spent, here = heapq.heappop(todo)
        if spent > best[here]:
            continue
        for there, step in graph.get(here, ()):
            if spent + step < best.get(there, float("inf")):
                best[there] = spent + step
                heapq.heappush(todo, (spent + step, there))
    return best


def weighted_gini(values: list[tuple[float, float]]) -> float | None:
    """Gini of (value, weight) pairs: Σ_i Σ_j w_i w_j |x_i − x_j| / (2 W² μ)."""
    total = sum(w for _, w in values)
    mean = sum(x * w for x, w in values) / total if total else 0.0
    if not total or not mean:
        return None
    return sum(wi * wj * abs(xi - xj) for xi, wi in values for xj, wj in values) / (2 * total * total * mean)


def measures(network: Network, measured: dict | None = None, budgets=(15, 30), beta: float = 0.1,
             closed: str | None = None) -> dict:
    """Every junction's accessibility and the residents' (see the module's docstring)."""
    graph = link_minutes(network, measured, closed)
    places: dict = defaultdict(lambda: defaultdict(float))  # door junction -> opportunity -> amount there
    residents: list[tuple[str, str, float]] = []  # (building, door, residents)
    for item in network.institutions.values():
        at = door(network, item)
        if at is None:
            continue
        places[at]["jobs"] += item.employees
        if item.kind == "School":
            places[at]["school places"] += item.people
        if item.kind == "Hospital":
            places[at]["hospitals"] += 1
        if item.kind == "Shop":
            places[at]["shops"] += 1
        if item.kind == "Residential" and item.people:
            residents.append((item.id, at, float(item.people)))
    rows = {}
    for origin in network.nodes:
        times = minutes_from(graph, origin)
        row = {"potential_jobs": 0.0}
        for budget in budgets:
            for kind, _ in KINDS:
                row[f"{kind}_within_{budget}_min"] = sum(found[kind] for node, found in places.items()
                                                         if times.get(node, float("inf")) <= budget)
        for node, found in places.items():
            if node in times:
                row["potential_jobs"] += found["jobs"] * exp(-beta * times[node])
        for kind in ("hospitals", "school places"):
            reachable = [times[node] for node, found in places.items() if found[kind] and node in times]
            row[f"minutes_to_nearest_{'hospital' if kind == 'hospitals' else 'school'}"] = min(reachable) if reachable else None
        rows[origin] = row
    total = sum(people for *_, people in residents)
    summary = {}
    if total:
        for key in rows[next(iter(rows))]:
            known = [(rows[at][key], people) for _, at, people in residents if rows[at][key] is not None]
            if known:
                summary[key] = round(sum(v * w for v, w in known) / sum(w for _, w in known), 2)
        gini = weighted_gini([(rows[at]["potential_jobs"], people) for _, at, people in residents])
        summary["jobs_access_gini"] = None if gini is None else round(gini, 3)
    worst = sorted(residents, key=lambda item: rows[item[1]]["potential_jobs"])[:5]
    return {"by_junction": rows, "residents": total, "residents_mean": summary,
            "least_served": [{"building": name, "door": at, "residents": people,
                              "potential_jobs": round(rows[at]["potential_jobs"], 1),
                              f"jobs_within_{budgets[0]}_min": rows[at][f"jobs_within_{budgets[0]}_min"]}
                             for name, at, people in worst]}


def report(network: Network, measured: dict | None = None, budgets=(15, 30), beta: float = 0.1,
           close_road: str | None = None) -> dict:
    """measures(), shortened for the assistant: the residents' means, the least served, the junctions with the most
    and least potential access to jobs; with `close_road`, each residents' measure before and after and its loss."""
    if not network.institutions:
        raise ValueError("No buildings placed: accessibility counts what people can reach (place_institution or "
                         "deploy_institutions first).")
    if close_road is not None and close_road not in network.roads and close_road not in network.approach_roads:
        raise ValueError(f"No road called {close_road!r}.")
    if not 1 <= len(budgets) <= 3 or any(not 1 <= b <= 120 for b in budgets) or not 0.01 <= beta <= 1:
        raise ValueError("budgets_min: 1 to 3 budgets of 1-120 minutes; beta_per_min 0.01 to 1 (0.1 by default).")
    found = measures(network, measured, budgets, beta)
    ranked = sorted(found["by_junction"].items(), key=lambda item: -item[1]["potential_jobs"])
    answer = {"times": "measured in the run" if measured else "free flow at the speed limits", "beta_per_min": beta,
              "residents": found["residents"], "residents_mean": found["residents_mean"],
              "least_served_residents": found["least_served"],
              "most_accessible_junctions": [{"junction": j, "potential_jobs": round(r["potential_jobs"], 1)} for j, r in ranked[:5]],
              "least_accessible_junctions": [{"junction": j, "potential_jobs": round(r["potential_jobs"], 1)}
                                             for j, r in ranked[-5:][::-1]],
              "notes": ["Opportunities sit at each building's door junction: jobs are every building's employees; school "
                        "places a school's students.", "potential_jobs: Hansen's measure, jobs discounted by "
                        "exp(-beta x minutes)."]}
    if not found["residents"]:
        answer["notes"].append("No Residential buildings with residents: the residents' measures need some.")
    if close_road is not None:
        after = measures(network, measured, budgets, beta, closed=close_road)
        loss = {}
        for key, before in found["residents_mean"].items():
            now = after["residents_mean"].get(key)
            if isinstance(before, (int, float)) and isinstance(now, (int, float)) and before and "minutes" not in key \
                    and key != "jobs_access_gini":
                loss[key] = round(100 * (before - now) / before, 1)
            elif "minutes" in key:
                loss[key] = {"before": before, "after": now}
        answer["closing"] = {"road": close_road, "loss_percent": loss,
                             "note": "Percent of the residents' access lost with the road closed (free flow: the "
                                     "detour's longer times); minutes to the nearest school and hospital before and after."}
    return answer
