"""Demand numbers worked out without changing anything, which the OD panel and the window apply (no Qt): gravity
and Furness generation fitted to a target saturation, scaling, ME2 count calibration, and multinomial-logit mode
choice for OD pairs and institutions."""

from __future__ import annotations

import heapq
from collections import defaultdict
from math import exp, hypot, log

from . import stats
from .design_checks import route_lengths
from .institutions import WALK, catchment, door, hourly_people
from .lane_graph import CYCLE_LANE_TIME_FACTOR, Graph
from .metro import METRO, METRO_WALK_M, WALK_KMH, door as metro_door, places as metro_places, ride as metro_ride
from .models import Network
from .stats import geh
from .tool_args import SATURATION_RANGE, _int, _parsed_json


# Demand sizing stops before a link's 95th-percentile queue fills this share of it (HCM's queue storage ratio: 1 is
# failure; queues on neighbouring short links interact well before it)
STORAGE_LIMIT = 0.5


def _design_hour(trips: dict, shares: dict | None) -> int | None:
    """The busiest hour of the day for daily trips spread by each pair's 24 `shares` (a pair without
    any: evenly), as a Run picks it to time its signals; None for hourly demand (shares None)."""
    if shares is None:
        return None
    return max(range(24), key=lambda hour: sum(value * _share(shares, pair, hour) for pair, value in trips.items()))


def _share(shares: dict | None, pair, hour: int | None) -> float:
    if hour is None:
        return 1.0
    return shares[pair][hour] if shares.get(pair) else 1 / 24


def _load(graph: Graph, trips: dict, shares: dict | None, hour: int | None) -> tuple[dict, dict]:
    """graph.assign of the veh/h the trips make in that hour (hourly demand: as they are)."""
    return graph.assign({pair: value * _share(shares, pair, hour) for pair, value in trips.items()})


def _saturations(graph: Graph, flows: dict, moves: dict) -> dict[str, float]:
    """Each junction's degree of saturation where someone gives way or waits (Graph.saturation)."""
    found = {node: graph.saturation(node, flows, moves) for node in graph.network.nodes}
    return {node: x for node, x in found.items() if x is not None}


def _fit_saturation(graph: Graph, fixed: dict, scaled: dict, target: float,
                    shares: dict | None) -> tuple[float, str]:
    """The factor on `scaled` ({pair: veh/h, or trips/day with shares}) that brings the most saturated
    junction to `target`, the `fixed` pairs staying as they are, or first fills a link with its queue (its queue
    storage ratio reaching 1, Graph.storage_ratio: a short link spills back into the junction behind long before
    either junction reaches its X; an 80 m signal grid sized to X 0.6 jammed). Both rise with the factor: bisection
    on whichever binds first. (factor, "saturation" or "storage": which bound it)."""
    if not SATURATION_RANGE[0] <= target <= SATURATION_RANGE[1]:
        raise ValueError("degree_of_saturation must be 0.1 to 1.2 (0.8 = 80 % of capacity).")
    hour = _design_hour({**fixed, **scaled}, shares)
    base, extra = _load(graph, fixed, shares, hour), _load(graph, scaled, shares, hour)

    def loaded(factor: float):
        return ({key: a.get(key, 0.0) + factor * b.get(key, 0.0) for key in {*a, *b}} for a, b in zip(base, extra))

    def worst(factor: float) -> float:  # the binding share of its limit: X over target, or a queue over its storage
        flows, moves = loaded(factor)
        x = max(_saturations(graph, flows, moves).values(), default=0.0)
        spill = max((graph.storage_ratio(node, moves) or 0.0 for node in graph.network.nodes), default=0.0)
        return max(x, spill / STORAGE_LIMIT * target)

    def bound(factor: float) -> str:
        flows, moves = loaded(factor)
        x = max(_saturations(graph, flows, moves).values(), default=0.0)
        spill = max((graph.storage_ratio(node, moves) or 0.0 for node in graph.network.nodes), default=0.0)
        return "storage" if spill / STORAGE_LIMIT * target > x else "saturation"

    floor_x = worst(0.0)
    if worst(1.0) <= floor_x:
        raise ValueError("No route of these pairs crosses a junction where traffic waits (signalised, roundabout "
                         "or a priority junction's minor arm): there is nothing to size by. Give total or factor.")
    if floor_x >= target:
        raise ValueError(f"The other pairs alone already load a junction to {floor_x:.2f}: lower them, or aim higher.")
    low, high = 0.0, 1.0
    for _ in range(60):
        if worst(high) >= target:
            break
        high *= 2
    for _ in range(40):
        middle = (low + high) / 2
        low, high = (middle, high) if worst(middle) < target else (low, middle)
    return high, bound(high)


def _saturation_report(graph: Graph, trips: dict, shares: dict | None) -> dict:
    """What the demand does to the junctions: the five most saturated (flow/capacity) with their
    control, and for daily demand the hour that is."""
    hour = _design_hour(trips, shares)
    busiest = sorted(_saturations(graph, *_load(graph, trips, shares, hour)).items(), key=lambda item: -item[1])[:5]
    report = {}
    if busiest:
        report["degree_of_saturation"] = {node: round(x, 2) for node, x in busiest}
        report["junction_controls"] = {node: graph.network.nodes[node].control for node, _ in busiest}
    if hour is not None:
        report["design_hour"] = f"{hour:02d}:00-{hour + 1:02d}:00"
    return report


def _no_demand_reason(trips: dict, by_o: dict, by_d: dict, total: float, unit: str) -> str:
    """Why a gravity model gave no pair any demand, and what to change."""
    if not any(trips.values()):
        sources = ", ".join(sorted(end for end, weight in by_o.items() if weight)) or "none"
        sinks = ", ".join(sorted(end for end, weight in by_d.items() if weight)) or "none"
        return (f"No pair gets demand: trips leave only {sources} (supply) and arrive only at {sinks} (demand), and "
                "no route joins two different ends of these. Set supply and demand at more ends (set_end_flows), or "
                "set them to 0 everywhere so the total is shared by every pair. Nothing was changed.")
    return (f"{total:g} {unit} over {len(trips)} pairs is under 1 a pair, so every pair rounds to 0: give a total of "
            f"at least {len(trips)}, or fewer pairs. Nothing was changed.")


def _furness(km: dict, leave: dict, arrive: dict, decay: float) -> dict:
    """T = O·D·exp(−decay·km) over the pairs with a route, balanced to the origins' and destinations'
    totals by Furness's iterations (100 at most, to 0.1 %)."""
    trips = {(o, d): leave[o] * arrive[d] * exp(-decay * distance) for (o, d), distance in km.items()}
    for _ in range(100):  # Furness: rows to the origins' totals, then columns to the destinations'
        rows, columns = defaultdict(float), defaultdict(float)
        for (o, _d), value in trips.items():
            rows[o] += value
        trips = {(o, d): value * leave[o] / rows[o] if rows[o] else 0.0 for (o, d), value in trips.items()}
        for (_o, d), value in trips.items():
            columns[d] += value
        trips = {(o, d): value * arrive[d] / columns[d] if columns[d] else 0.0 for (o, d), value in trips.items()}
        rows = defaultdict(float)
        for (o, _d), value in trips.items():
            rows[o] += value
        if all(abs(rows[o] - leave[o]) <= 0.001 * leave[o] for o in rows):
            break
    return trips


def _mean_km(trips: dict, km: dict) -> float:
    total = sum(trips.values())
    return sum(value * km[pair] for pair, value in trips.items()) / total if total else 0.0


def demand_plan(network: Network, pairs: list, args: dict, values: dict | None = None,
                shares: dict | None = None) -> tuple[dict, dict]:
    """generate_demand's numbers for these (origin, destination) pairs, nothing changed: a gravity model
    T = O·D·exp(-β·km) over the shortest routes, balanced to the ends' totals (Furness). `values`: the
    matrix's demand of that kind now ({pair: value}; pairs outside `pairs` keep theirs and load the
    junctions too); `shares`: daily demand's 24 hourly shares by pair (a pair without: evenly).
    ValueError says what is missing."""
    demand = args.get("demand") or "hourly"
    if demand not in ("hourly", "daily"):
        raise ValueError("demand must be 'hourly' or 'daily'.")
    decay = float(args.get("distance_decay_per_km", 0.5) if args.get("distance_decay_per_km") is not None else 0.5)
    if not 0 <= decay <= 10:
        raise ValueError("distance_decay_per_km must be 0 to 10.")
    origins, destinations = sorted({o for o, _ in pairs}), sorted({d for _, d in pairs})
    ends, days = network.approach_nodes, 24 if demand == "daily" else 1
    supply = {o: ends[o].supply_vph if o in ends else 0.0 for o in origins}
    wanted = {d: ends[d].demand_vph if d in ends else 0.0 for d in destinations}
    note = ""
    target = args.get("degree_of_saturation")
    if target is not None:  # sized below, once the routes are known: any total does
        target = float(target)
        if args.get("total") not in (None, "", 0):  # 0: a small model's filler, no total
            raise ValueError("Give total or degree_of_saturation, not both.")
        if not SATURATION_RANGE[0] <= target <= SATURATION_RANGE[1]:
            raise ValueError("degree_of_saturation must be 0.1 to 1.2 (0.8 = 80 % of capacity).")
        total = 10_000.0 * days
    elif args.get("total") not in (None, ""):
        total = float(args["total"])
        if not 1 <= total <= 2_000_000:
            raise ValueError("total must be 1 to 2,000,000.")
    else:
        total = (sum(supply.values()) or sum(wanted.values())) * days
        if not total:
            raise ValueError("Give degree_of_saturation for how busy (light 0.4, medium 0.65, busy or peak 0.85), "
                             "or total (veh/h hourly, trips per day daily), or set the ends' supply and demand first "
                             "(set_end_flows).")
        note = "Totals from the ends' flows" + (" (veh/h × 24 for a day)." if days > 1 else ".")
    by_o = supply if any(supply.values()) else dict.fromkeys(origins, 1.0)
    by_d = wanted if any(wanted.values()) else dict.fromkeys(destinations, 1.0)
    leave = {o: total * by_o[o] / sum(by_o.values()) for o in origins}
    arrive = {d: total * by_d[d] / sum(by_d.values()) for d in destinations}
    routes = route_lengths(network, origins)
    km = {(o, d): routes[o][d] / 1000 for o, d in pairs if d in routes[o]}
    fitted = args.get("mean_trip_km") or None  # 0 km is no trip: a small model's filler (llama3.2), not a length
    if fitted is not None:  # Hyman's method: the decay whose balanced matrix has this mean trip length
        if args.get("distance_decay_per_km") is not None:
            raise ValueError("Give mean_trip_km or distance_decay_per_km, not both.")
        fitted = float(fitted)
        shortest, longest = (_mean_km(_furness(km, leave, arrive, beta), km) for beta in (10.0, 0.0))
        if longest <= 0 or longest - shortest < 0.01:  # no trips to spread (flows at one end), or all one length
            raise ValueError("A mean trip length can't be fitted here: " + (
                "no trip can be made between these ends (flows set at only one end?). Set supply and demand at "
                "more ends (set_end_flows), or give total." if longest <= 0 else
                f"every route is about {longest:.2f} km, so the decay doesn't change it. Give distance_decay_per_km "
                "instead.") + " Nothing was changed.")
        if not shortest <= fitted <= longest:
            raise ValueError(f"mean_trip_km must be {shortest:.2f} to {longest:.2f} km on these ends' routes (the "
                             "shortest and the longest mean a gravity model can make here).")
        low, high = 0.0, 10.0
        for _ in range(40):  # the mean trip length falls as the decay rises
            middle = (low + high) / 2
            low, high = (middle, high) if _mean_km(_furness(km, leave, arrive, middle), km) > fitted else (low, middle)
        decay = round((low + high) / 2, 4)
    trips = _furness(km, leave, arrive, decay)
    shares = (shares or {}) if days > 1 else None
    others = {pair: float(value) for pair, value in (values or {}).items() if value and pair not in set(pairs)}
    graph = Graph(network)
    if target is not None:  # every pair scaled until the most saturated junction is at the target
        factor, sized_by = _fit_saturation(graph, others, trips, target, shares)
        trips = {pair: value * factor for pair, value in trips.items()}
        total = sum(trips.values())
    plan = {pair: round(value) for pair, value in trips.items() if round(value) > 0}
    unit = "trips/day" if days > 1 else "veh/h"
    if not plan:  # refused before anything is typed in: the matrix would only be cleared
        raise ValueError(_no_demand_reason(trips, by_o, by_d, total, unit))
    info = {"method": "gravity model, exp(-distance_decay_per_km x km) over the shortest routes, balanced to the "
                      "ends' totals (Furness)" + ("; the decay fitted to mean_trip_km (Hyman)" if fitted is not None else ""),
            "demand": demand, "unit": unit, "distance_decay_per_km": decay,
            "mean_trip_km": round(_mean_km(plan, km), 2),
            "total_asked": round(total), "total_made": sum(plan.values()), "pairs_with_demand": len(plan),
            "busiest_pairs": {f"{o}->{d}": value for (o, d), value in
                              sorted(plan.items(), key=lambda item: -item[1])[:5]}}
    if len(plan) < len(trips):
        info["pairs_rounded_to_0"] = len(trips) - len(plan)  # under 0.5 a pair: a larger total reaches them
    missing = [f"{o}->{d}" for o, d in pairs if d not in routes[o]]
    if missing:
        info["no_route"] = missing[:20]
    info.update(_saturation_report(graph, {**others, **plan}, shares))
    if target is not None:
        info["degree_of_saturation_target"] = target
        if sized_by == "storage":
            info["sized_by"] = "a link's queue fills its length before any junction reaches that degree of saturation (short links spill back into the junction behind): sized to what the links hold instead"
    if note:
        info["note"] = note
    return plan, info


def scale_plan(network: Network, values: dict, pairs: list, args: dict, shares: dict | None = None) -> tuple[dict, dict]:
    """scale_demand's numbers, nothing changed: the matrix's demand `values` ({pair: veh/h, or trips/day
    with `shares` as in demand_plan}) in `pairs` times `factor`, or times whatever brings the most
    saturated junction to `degree_of_saturation`; the other pairs stay and still load the junctions."""
    factor, target = args.get("factor"), args.get("degree_of_saturation")
    if (factor is None) == (target is None):
        raise ValueError("Give factor (e.g. 1.2 for 20 % more) or degree_of_saturation (e.g. 0.8), one of them.")
    daily = args.get("demand") == "daily"
    scope = set(pairs)
    scaled = {pair: float(value) for pair, value in values.items() if value and pair in scope}
    if not scaled:
        raise ValueError(f"There's no {'daily' if daily else 'hourly'} demand in those pairs to scale: enter some "
                         "first (set_od_demand or generate_demand).")
    others = {pair: float(value) for pair, value in values.items() if value and pair not in scope}
    shares = (shares or {}) if daily else None
    graph = Graph(network)
    if factor is not None:
        factor = float(factor)
        if not 0.01 <= factor <= 100:
            raise ValueError("factor must be 0.01 to 100.")
    else:
        factor, sized_by = _fit_saturation(graph, others, scaled, float(target), shares)
    plan = {pair: round(value * factor) for pair, value in scaled.items()}
    if not any(plan.values()):
        raise ValueError(f"A factor of {factor:.3g} rounds every pair's demand to 0: nothing would be left. "
                         "Nothing was changed.")
    info = {"demand": "daily" if daily else "hourly", "unit": "trips/day" if daily else "veh/h",
            "factor": round(factor, 3), "pairs_scaled": len(plan), "total_before": round(sum(scaled.values())),
            "total_after": sum(plan.values())}
    if any(not value for value in plan.values()):
        info["pairs_cleared"] = sum(not value for value in plan.values())  # scaled under 0.5: their demand goes
    info.update(_saturation_report(graph, {**others, **plan}, shares))
    if target is not None:
        info["degree_of_saturation_target"] = float(target)
        if sized_by == "storage":
            info["sized_by"] = "a link's queue fills its length before any junction reaches that degree of saturation (short links spill back into the junction behind): sized to what the links hold instead"
    return plan, info


def _routes(network: Network, pairs) -> dict:
    """The roads on each pair's shortest route (one-way roads kept): {(o, d): [road ids]}; absent: no route."""
    ahead = defaultdict(list)
    for road in network.all_roads():
        ahead[road.from_node].append((road.to_node, road.length_m, road.id))
        if road.direction == "two-way":
            ahead[road.to_node].append((road.from_node, road.length_m, road.id))
    found = {}
    for origin in {o for o, _ in pairs}:
        best, via, todo = {origin: 0.0}, {}, [(0.0, origin)]
        while todo:
            metres, here = heapq.heappop(todo)
            if metres > best.get(here, float("inf")):
                continue
            for there, step, road_id in ahead[here]:
                if metres + step < best.get(there, float("inf")):
                    best[there], via[there] = metres + step, (here, road_id)
                    heapq.heappush(todo, (metres + step, there))
        for o, d in pairs:
            if o == origin and d in best:
                path, node = [], d
                while node != origin:
                    node, road_id = via[node]
                    path.append(road_id)
                found[(o, d)] = path[::-1]
    return found


def calibration_plan(network: Network, flows: dict, args: dict) -> tuple[dict, dict]:
    """calibrate_demand's numbers, nothing changed: the prior hourly flows `flows` {(o, d): veh/h} scaled
    so the roads' modelled flows meet the counts, ME2's multiplicative balancing (Van Zuylen and Willumsen 1980):
    T_od ← T_od × (count_a / modelled_a)^p_od,a for each counted road a, repeated. p_od,a is the share of the
    pair's vehicles that drive road a on the routes the simulation gives them (Graph.route_shares: fastest, with a
    two-phase signal's offside turn as an out-and-back, counted each time it is driven), not the shortest
    distance: on those, 80 of a 3 x 3 grid's 132 pairs went another way, and a fit of GEH 0 loaded 16.8-23.7."""
    counts = args.get("counts")
    if isinstance(counts, str):
        counts = _parsed_json(counts)
    if not isinstance(counts, list) or not counts:
        raise ValueError("Give counts like [{\"road\": \"1,1-2,1\", \"vph\": 800}].")
    wanted = {}
    for count in counts:
        road = str(count.get("road", "")) if isinstance(count, dict) else ""
        if road not in network.roads and road not in network.approach_roads:
            raise ValueError(f"No road called {road!r} to count on.")
        vph = count.get("vph")
        if not isinstance(vph, (int, float)) or vph < 0:
            raise ValueError(f"{road}: vph must be a count, not below zero.")
        wanted[road] = float(vph)
    prior = {pair: float(value) for pair, value in flows.items() if value}
    if not prior:
        raise ValueError("There's no hourly demand to start from: enter some first (set_od_demand or generate_demand).")
    iterations = max(1, min(_int(args.get("iterations") or 50), 500))
    graph = Graph(network)
    use: dict = {road: {} for road in wanted}  # road -> {pair: vehicles on it per vehicle of the pair}
    for pair in prior:
        for share, route in graph.route_shares(*pair):
            for link in route:
                if link.road in use:
                    use[link.road][pair] = use[link.road].get(pair, 0.0) + share
    on = {road: pairs for road, pairs in use.items()}
    uncovered = [road for road, pairs in on.items() if not pairs]
    if len(uncovered) == len(wanted):
        raise ValueError(f"No OD pair's route uses {', '.join(uncovered)}: the counts can't be met by this demand.")

    def modelled(trips):
        return {road: sum(trips[pair] * p for pair, p in pairs.items()) for road, pairs in on.items()}

    before, trips = modelled(prior), dict(prior)
    for _ in range(iterations):
        for road, pairs in on.items():
            now = sum(trips[pair] * p for pair, p in pairs.items())
            if now > 0:
                factor = wanted[road] / now
                for pair, p in pairs.items():
                    trips[pair] *= factor ** min(p, 1.0)
    after = modelled(trips)
    plan = {pair: round(value) for pair, value in trips.items() if round(value) > 0}
    if not plan:
        raise ValueError("Fitted to those counts, every pair's demand rounds to 0: the matrix would be cleared. "
                         "Check the counts (a 0 on a road every route uses empties it). Nothing was changed.")
    rows = [{"road": road, "counted_vph": wanted[road], "before_vph": round(before[road]), "after_vph": round(after[road]),
             "geh_before": round(geh(before[road], wanted[road]), 2), "geh_after": round(geh(after[road], wanted[road]), 2)}
            for road in wanted]
    info = {"method": "ME2 multiplicative balancing (Van Zuylen and Willumsen 1980) on the routes the simulation "
                      "drives, from the demand entered", "counts": rows, "iterations": iterations,
            "fits": sum(row["geh_after"] < 5 for row in rows), "of": len(rows),
            "total_before_vph": round(sum(prior.values())), "total_after_vph": sum(plan.values())}
    if uncovered:
        info["note"] = f"No route uses {', '.join(uncovered)}: left as it was."
    return plan, info


# HCM 2000's default mode-choice utility (p1057-1058): U = -0.025 IVT - 0.050 OVT - 0.005 cost (minutes, cents)
LOGIT_DEFAULTS = {"in_vehicle_per_min": -0.025, "out_of_vehicle_per_min": -0.050, "cost": -0.005}


def metro_cost(alternative: dict, ride: dict) -> float:
    """A metro ride's cost: its line's fare, plus an alternative's own cost and cost_per_km (on the km ridden). WALK_KMH
    (metro.py) is also an institution's walk_kmh by default."""
    return ride["cost"] + float(alternative.get("cost") or 0) + float(alternative.get("cost_per_km") or 0) * ride["km"]


def default_alternative(mode) -> dict:
    """An institution's mode split's assumed attributes for a vehicle type, when none are given: a timetabled
    type (a bus or tram) a 5 min walk to its stop and a 100 cent fare; any other 3 min to park and walk, and 15 cents a km."""
    if mode.headway_min or getattr(mode, "timetabled", False):
        return {"mode": mode.name, "out_of_vehicle_min": 5.0, "cost": 100.0}
    if getattr(mode, "vehicle_class", "") == "bicycle":  # a minute to lock it up; next to no running cost
        return {"mode": mode.name, "out_of_vehicle_min": 1.0, "cost_per_km": 0.0}
    return {"mode": mode.name, "out_of_vehicle_min": 3.0, "cost_per_km": 15.0}


def _cycled(mode, roads: list, minutes: float) -> float:
    """A bicycle's in-vehicle minutes as a cyclist feels them: those on roads with cycle lanes at
    lane_graph.CYCLE_LANE_TIME_FACTOR (in the length's proportion); any other type's, as they are."""
    if getattr(mode, "vehicle_class", "") != "bicycle":
        return minutes
    total = sum(road.length_m for road in roads)
    laned = sum(road.length_m for road in roads if road.cycle_lanes) / total if total else 0.0
    return minutes * (1 - laned + laned * CYCLE_LANE_TIME_FACTOR)


def institution_split(network: Network, item, modes: list, args: dict) -> tuple[dict, dict]:
    """An institution's mode split, nothing changed: people % by mode. Each junction or end of its catchment
    is an origin, its door the destination, the route km between them the trip. Each mode's utility U =
    constant + in_vehicle_per_min x IVT + out_of_vehicle_per_min x OVT + cost x cost: vehicle types as choose_modes
    (IVT at free flow, OVT the minutes given plus half the headway), Walk all out-of-vehicle (km at walk_kmh).
    The shares are the logit's, averaged over the catchment (each origin an equal share, as its trips are).
    observed_percent calibrates the observed modes' constants, c += ln(observed / predicted) until they match
    (Train 2009, section 2.8); the other modes share the rest in the model's proportions."""
    names = {mode.name.lower(): mode for mode in modes}
    by_metro = bool(network.metro_lines) and METRO.lower() not in names  # the network's metro as a mode
    offered = ", ".join(["Walk", *(m.name for m in modes), *([METRO] if by_metro else [])])
    given = _parsed_json(args["alternatives"]) if isinstance(args.get("alternatives"), str) else args.get("alternatives")
    assumed = not given
    if assumed:
        given = [{"mode": WALK}, *(default_alternative(mode) for mode in modes), *([{"mode": METRO}] if by_metro else [])]
    if not isinstance(given, list) or len(given) < 2 or not all(isinstance(a, dict) for a in given):
        raise ValueError(f"Give 2 or more alternatives like [{{\"mode\": \"Walk\"}}, {{\"mode\": \"Car\", "
                         f"\"cost_per_km\": 15}}]; modes: {offered}.")
    chosen = [WALK if str(a.get("mode", "")).strip().lower() == "walk" else
              METRO if by_metro and str(a.get("mode", "")).strip().lower() in (METRO.lower(), "metro") else
              getattr(names.get(str(a.get("mode", "")).strip().lower()), "name", None) for a in given]
    if None in chosen or len(set(chosen)) != len(chosen):
        raise ValueError(f"Each alternative is a different mode: {offered}.")
    coefficients = {**LOGIT_DEFAULTS, **(args.get("coefficients") or {})}
    if set(coefficients) != set(LOGIT_DEFAULTS) or any(not isinstance(v, (int, float)) or v > 0 for v in coefficients.values()):
        raise ValueError(f"coefficients are {', '.join(LOGIT_DEFAULTS)}, each 0 or below (more time or cost is worse).")
    walk_kmh = float(args.get("walk_kmh") or WALK_KMH)
    if not 1 <= walk_kmh <= 10:
        raise ValueError("walk_kmh must be 1 to 10.")
    for alternative in given:
        for key in ("cost", "cost_per_km", "out_of_vehicle_min"):
            if float(alternative.get(key) or 0) < 0:
                raise ValueError(f"{alternative['mode']}: {key} can't be below 0.")
    observed_in = _parsed_json(args["observed_percent"]) if isinstance(args.get("observed_percent"), str) \
        else args.get("observed_percent") or {}
    canonical = {name.lower(): name for name in chosen}
    observed = {}
    for name, percent in (observed_in.items() if isinstance(observed_in, dict) else [(None, None)]):
        mode = canonical.get(str(name).strip().lower())
        if mode is None or not isinstance(percent, (int, float)) or isinstance(percent, bool) or not 0 <= percent <= 100:
            raise ValueError(f"observed_percent is a share 0-100 for any of {', '.join(chosen)}, e.g. {{\"Car\": 30}}.")
        observed[mode] = float(percent)
    total = sum(observed.values())
    if total > 100.5 or (len(observed) == len(chosen) and abs(total - 100) > 0.5):
        raise ValueError(f"The observed shares add up to {total:g} %: at most 100, and exactly 100 when every mode has one.")
    alternatives = {mode: alternative for mode, alternative in zip(chosen, given) if observed.get(mode, 1) > 0}
    observed = {mode: percent / 100 for mode, percent in observed.items() if percent > 0}
    if len(alternatives) < 2:
        raise ValueError("At least two modes must be possible (an observed 0 % leaves a mode out).")
    entrance, origins = door(network, item), catchment(network, item)
    if entrance is None or not origins:
        raise ValueError(f"{item.id}'s circle reaches no junction or end other than its door ({entrance}): make its "
                         "circle bigger (update_institution radius_m).")
    routes = _routes(network, [(origin, entrance) for origin in origins])
    trips = [(origin, [network.road(road_id) for road_id in routes[(origin, entrance)]])
             for origin in origins if (origin, entrance) in routes]
    if not trips:
        raise ValueError(f"No route from {item.id}'s catchment to its door {entrance}.")
    attributes = []  # per place (a junction or end of its circle, or a station): {mode: (IVT, OVT, cost)}
    for origin, roads in trips:
        km = sum(road.length_m for road in roads) / 1000
        found = {}
        for mode, alternative in alternatives.items():
            ovt, cost = float(alternative.get("out_of_vehicle_min") or 0), \
                float(alternative.get("cost") or 0) + float(alternative.get("cost_per_km") or 0) * km
            if mode == WALK:
                found[mode] = (0.0, ovt + km / walk_kmh * 60, cost)
                continue
            if mode == METRO:  # the roads' places don't ride the metro: its own stations are places (below)
                continue
            vehicle = names[mode.lower()]
            top = min(vehicle.max_speed_kmh, vehicle.desired_max_kmh)
            ivt = _cycled(vehicle, roads, sum(road.length_m / (min(top, road.speed_limit_kmh) / 3.6) for road in roads) / 60)
            found[mode] = (ivt, ovt + vehicle.headway_min / 2, cost)
        attributes.append((origin, km, found))
    # The stations in its circle are places its people come from too, as the junctions are (each an equal share):
    # there, the metro to its door station and the walk on, or walking all the way
    station_door, stations = metro_places(network, item) if METRO in alternatives else (None, [])
    walk_on = metro_door(network, item.x, item.y)[1] / 1000 / walk_kmh * 60 if station_door else 0.0
    rides = {}
    for station in stations:
        trip = rides[station] = metro_ride(network, station, station_door)
        stop = network.metro_stops[station]
        km = hypot(stop.x - item.x, stop.y - item.y) / 1000
        metro_alternative = alternatives[METRO]
        found = {METRO: (trip["ivt_min"], float(metro_alternative.get("out_of_vehicle_min") or 0) + trip["wait_min"]
                         + walk_on, metro_cost(metro_alternative, trip))}
        if WALK in alternatives:
            found[WALK] = (0.0, float(alternatives[WALK].get("out_of_vehicle_min") or 0) + km / walk_kmh * 60,
                           float(alternatives[WALK].get("cost") or 0))
        attributes.append((station, km, found))
    constants = {mode: float(alternative.get("constant") or 0) for mode, alternative in alternatives.items()}

    def predict() -> tuple[dict, list]:
        rows = [(origin, km, stats.logit({mode: constants[mode] + coefficients["in_vehicle_per_min"] * ivt
                                          + coefficients["out_of_vehicle_per_min"] * ovt + coefficients["cost"] * cost
                                          for mode, (ivt, ovt, cost) in found.items()}))
                for origin, km, found in attributes]
        return {mode: sum(row[2].get(mode, 0.0) for row in rows) / len(rows) for mode in alternatives}, rows

    predicted, rows = predict()
    steps = 0
    while observed and steps < 200:
        free = [mode for mode in alternatives if mode not in observed]
        rest, free_total = 1 - sum(observed.values()), sum(predicted[mode] for mode in free)
        target = {**observed, **{mode: rest * predicted[mode] / free_total for mode in free if free_total}}
        if max(abs(predicted[mode] - target[mode]) for mode in observed) < 1e-6:
            break
        for mode in observed:
            constants[mode] += log(target[mode] / max(predicted[mode], 1e-12))
        predicted, rows = predict()
        steps += 1
    percent = {mode: round(100 * share, 2) for mode, share in predicted.items()}
    people = sum(hourly_people(item)[0])
    vehicles = {}
    for mode, share in predicted.items():
        vehicle = names.get(mode.lower()) if mode != WALK else None
        if vehicle is not None and not vehicle.headway_min:
            vehicles[mode] = round(people * share / max(vehicle.occupancy, 1e-9))
    info = {"institution": item.id, "people_percent": percent, "door": entrance, "origins": len(trips),
            "mean_trip_km": round(sum(km for _, km, _ in attributes) / len(attributes), 2),
            "method": "multinomial logit over the catchment's trips to the door, U = constant + in_vehicle_per_min·IVT "
                      "+ out_of_vehicle_per_min·OVT + cost·cost (HCM 2000's coefficients unless given); Walk all "
                      "out-of-vehicle at walk_kmh",
            "coefficients": coefficients, "walk_kmh": walk_kmh,
            "constants": {mode: round(value, 3) for mode, value in constants.items()},
            "people_per_day_each_way": round(people), "vehicles_per_day_each_way": vehicles,
            "by_origin": [{"from": origin, "km": round(km, 2), "people_percent": {m: round(100 * p, 1) for m, p in shares_.items()}}
                          for origin, km, shares_ in rows[:12]]}
    if observed:
        info["calibrated_to_percent"] = {mode: round(100 * share, 2) for mode, share in observed.items()}
        info["calibration"] = (f"constants fitted in {steps} steps (c += ln(observed/predicted), Train 2009 §2.8); "
                               "the unobserved modes keep their relative odds")
    if assumed:
        info["assumed"] = ("Walk, and every vehicle type: timetabled ones a 5 min walk to the stop and a 100 cent fare, "
                           "the others 3 min to park and 15 cents a km; give alternatives to change them, and "
                           "observed_percent to calibrate to known shares")
    timed = [mode for mode in alternatives if mode not in (WALK, METRO) and names[mode.lower()].headway_min]
    if timed:
        info["note"] = f"{', '.join(timed)} riders ride the network's timetabled buses: they add no vehicles of their own."
    if METRO in alternatives:
        info["metro"] = {"door": station_door, "stations": stations, "places": len(attributes),
                         "rides": [{"from": station, **{k: round(v, 2) if isinstance(v, float) else v
                                                        for k, v in trip.items()}} for station, trip in rides.items()],
                         "note": (f"The stations of its door station's line inside its circle are places its people "
                                  f"come from, as the junctions are; its door station is the nearest within "
                                  f"{METRO_WALK_M:g} m. Riders add no vehicles." if station_door else
                                  f"No station within {METRO_WALK_M:g} m of it: nobody comes by MRT.")}
    return percent, info


def mode_plan(network: Network, pairs: list, args: dict, modes: list, values: dict, trips=None,
              current: dict | None = None) -> tuple[dict, dict]:
    """choose_modes' split, nothing changed: per pair with demand ({pair: value} in `values`), each
    alternative's utility U = constant + β_ivt·IVT + β_ovt·OVT + β_cost·cost, its people share by
    multinomial logit, and the vehicle split over the untimetabled types (people ÷ occupancy). IVT is
    the mean trip time of that pair and type in `trips` (a run's (origin, destination, type, trip s, delay s)),
    else the free-flow time along the shortest route at min(type's speed, limit); OVT is the out-of-vehicle
    minutes given plus half the type's headway (random arrival at a timetabled stop). {pair: {type: share}}.
    With `current` ({pair: {type: share}}, the split the pair has now), each pair's people (its vehicles × their
    mean occupancy now) ride the new split, so its vehicles change: info["values"] {pair: veh/h or trips/day}. A
    shift to fuller vehicles then means fewer of them (with the vehicles kept, a shift to the bus left car traffic
    as it was: audit 2026-10-13, M6); people on a timetabled type add no vehicles (its runs are fixed)."""
    alternatives = _parsed_json(args.get("alternatives")) if isinstance(args.get("alternatives"), str) else args.get("alternatives")
    names = {mode.name.lower(): mode for mode in modes}
    if not isinstance(alternatives, list) or len(alternatives) < 2 or not all(isinstance(a, dict) for a in alternatives):
        raise ValueError("Give 2 or more alternatives like [{\"mode\": \"Car\", \"cost_per_km\": 30}, "
                         f"{{\"mode\": \"Bus\", \"cost\": 150}}]; types: {', '.join(m.name for m in modes)}.")
    chosen = [str(a.get("mode", "")).strip().lower() for a in alternatives]
    unknown = [name for name in chosen if name not in names]
    if unknown or len(set(chosen)) != len(chosen):
        raise ValueError(f"Each alternative is a different vehicle type: {', '.join(m.name for m in modes)}"
                         + (f" (no {', '.join(unknown)})." if unknown else "."))
    coefficients = {**LOGIT_DEFAULTS, **(args.get("coefficients") or {})}
    if set(coefficients) != set(LOGIT_DEFAULTS) or any(not isinstance(v, (int, float)) or v > 0 for v in coefficients.values()):
        raise ValueError(f"coefficients are {', '.join(LOGIT_DEFAULTS)}, each 0 or below (more time or cost is worse).")
    for alternative in alternatives:
        for key in ("cost", "cost_per_km", "out_of_vehicle_min"):
            if float(alternative.get(key) or 0) < 0:
                raise ValueError(f"{alternative['mode']}: {key} can't be below 0.")
    nests = {}
    for nest in (_parsed_json(args["nests"]) if isinstance(args.get("nests"), str) else args.get("nests")) or []:
        members = [str(m).strip().lower() for m in (nest.get("modes") or [])] if isinstance(nest, dict) else []
        scale = nest.get("scale", 1.0) if isinstance(nest, dict) else None
        if len(members) < 2 or any(m not in chosen for m in members) or not isinstance(scale, (int, float)) \
                or not 0 < scale <= 1 or any(m in {x for v, _ in nests.values() for x in v} for m in members):
            raise ValueError("Each nest is {\"name\": ..., \"modes\": [2 or more of the alternatives, in no other "
                             "nest], \"scale\": over 0 and at most 1}.")
        nests[str(nest.get("name") or len(nests) + 1)] = ([names[m].name for m in members], float(scale))
    if all(names[name].headway_min for name in chosen):
        raise ValueError("Every alternative runs to a timetable, so its vehicles are fixed: include an untimetabled type.")
    scope = [pair for pair in pairs if values.get(pair)]
    if not scope:
        raise ValueError("Those pairs have no demand of the kind shown: enter some first (set_od_demand or generate_demand).")
    measured: dict = defaultdict(list)
    for origin, destination, kind, trip_s, *_ in trips or ():
        measured[(origin, destination, kind)].append(trip_s)
    routes = _routes(network, scope)
    plan, rows, elasticities, new_values = {}, [], defaultdict(lambda: {"cost": [], "in_vehicle": []}), {}
    for pair in scope:
        if pair not in routes:
            continue
        roads = [network.road(road_id) for road_id in routes[pair]]
        km = sum(road.length_m for road in roads) / 1000
        utilities, sources, attributes = {}, set(), {}
        for alternative, name in zip(alternatives, chosen):
            mode = names[name]
            times = measured.get((*pair, mode.name))
            if times:
                ivt = sum(times) / len(times) / 60
                sources.add("run")
            else:
                top = min(mode.max_speed_kmh, mode.desired_max_kmh)
                ivt = sum(road.length_m / (min(top, road.speed_limit_kmh) / 3.6) for road in roads) / 60
                sources.add("free flow")
            ivt = _cycled(mode, roads, ivt)
            ovt = float(alternative.get("out_of_vehicle_min") or 0) + mode.headway_min / 2
            cost = float(alternative.get("cost") or 0) + float(alternative.get("cost_per_km") or 0) * km
            attributes[mode.name] = (ivt, cost)
            utilities[mode.name] = (float(alternative.get("constant") or 0) + coefficients["in_vehicle_per_min"] * ivt
                                    + coefficients["out_of_vehicle_per_min"] * ovt + coefficients["cost"] * cost)
        people, top = stats.nested_logit(utilities, nests)
        for name, share in people.items():  # direct point elasticities, β·x·(1 − P) (the logit form; nests: approximate)
            ivt, cost = attributes[name]
            elasticities[name]["cost"].append(coefficients["cost"] * cost * (1 - share))
            elasticities[name]["in_vehicle"].append(coefficients["in_vehicle_per_min"] * ivt * (1 - share))
        vehicles = {name: share / max(getattr(names[name.lower()], "occupancy", 1.0), 1e-9)
                    for name, share in people.items() if not names[name.lower()].headway_min}
        total = sum(vehicles.values())
        plan[pair] = {name: share / total for name, share in vehicles.items()}
        if current and current.get(pair):  # people now: vehicles × mean occupancy; then the vehicles they need
            occupancy = sum(share * getattr(names[name.lower()], "occupancy", 1.0)
                            for name, share in current[pair].items() if name.lower() in names)
            new_values[pair] = values[pair] * occupancy * total
        rows.append({"pair": f"{pair[0]}->{pair[1]}", "km": round(km, 2), "times_from": " and ".join(sorted(sources)),
                     "logsum": round(top, 3),
                     "people_percent": {name: round(100 * share, 1) for name, share in people.items()},
                     "vehicle_percent": {name: round(100 * share, 1) for name, share in plan[pair].items()}})
    if not plan:
        raise ValueError("None of those pairs has a route: nothing to choose between.")
    info = {"method": "multinomial logit, U = constant + in_vehicle_per_min·IVT + out_of_vehicle_per_min·OVT + cost·cost "
                      "(HCM 2000's defaults unless coefficients say); people shares to vehicle shares by occupancy",
            "coefficients": coefficients, "pairs_split": len(plan), "pairs": rows[:12],
            "model": "nested logit" if nests else "multinomial logit",
            "elasticities": {name: {key: round(sum(v) / len(v), 3) for key, v in found.items()}
                             for name, found in elasticities.items()}}
    if coefficients["cost"]:
        info["value_of_time_per_hour"] = round(60 * coefficients["in_vehicle_per_min"] / coefficients["cost"], 1)  # cents
    timed = [names[name].name for name in chosen if names[name].headway_min]
    if timed:
        info["note"] = (f"{', '.join(timed)} run to a timetable: their vehicles are fixed by the headway, so their people "
                        "share is reported but the split goes to the other types.")
    if len(rows) > 12:
        info["more_pairs"] = len(rows) - 12
    if new_values:
        info["values"] = new_values
        info["vehicles_before"], info["vehicles_after"] = (round(sum(values[p] for p in new_values)),
                                                           round(sum(new_values.values())))
    return plan, info
