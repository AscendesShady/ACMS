"""Freeway weaving segments by the HCM 2000 Chapter 24 (no Qt): where a merge is closely followed by a diverge.

- `segments(graph)`: every link that starts at a merge and ends at a diverge, both junctions uncontrolled, the
  merging and diverging roads on the kerb side (as interchanges.py lays ramps). Lanes are counted the HCM's way
  (Exhibit 24-5): a ramp's lanes on the kerb side of the segment, the main road's on the offside; a weaving
  movement's lane changes are the lanes between its entry's and its exit's. A link with no lane beyond the main
  road's in and out (no auxiliary lane) is two ramp junctions, not a weave (Chapter 25's merge and diverge).
- `operate(...)`: speeds, density and LOS (Eqs 24-2 to 24-6, Exhibits 24-2, 24-6, 24-7), unconstrained or
  constrained, and `capacity(...)`: the flow scaled until the LOS E/F density (27 pc/km/ln freeway, 25 multilane),
  capped by a basic segment's capacity and the field limits on weaving flow and volume ratio (p24-9).
- `report(graph, rates)`: each segment's four movements from the demand on the routes a Run takes
  (`Graph.route_shares`), analysed; `measured(...)` adds a run's own speed and density on the segment.
The method is calibrated for freeways and multilane highways (free-flow speed 70-120 km/h): below that it is
said, not refused. One-sided weaves only (no two-sided Type C, no offside ramps); length node to node.
"""

from __future__ import annotations

MAX_WEAVE_M = 750.0  # beyond it, merge and diverge are analysed apart (p24-9)
S_MIN_KMH = 24.0  # Eq 24-3: the speeds run from 24 km/h to the free-flow speed + 8
# Exhibit 24-6: (a, b, c, d) of W = a(1 + VR)^b (v/N)^c / (3.28 L)^d, weaving then nonweaving, by constrained
CONSTANTS = {"A": {False: ((0.15, 2.2, 0.97, 0.80), (0.0035, 4.0, 1.3, 0.75)),
                   True: ((0.35, 2.2, 0.97, 0.80), (0.0020, 4.0, 1.3, 0.75))},
             "B": {False: ((0.08, 2.2, 0.70, 0.50), (0.0020, 6.0, 1.0, 0.50)),
                   True: ((0.15, 2.2, 0.70, 0.50), (0.0010, 6.0, 1.0, 0.50))},
             "C": {False: ((0.08, 2.3, 0.80, 0.60), (0.0020, 6.0, 1.1, 0.60)),
                   True: ((0.14, 2.3, 0.80, 0.60), (0.0010, 6.0, 1.1, 0.60))}}
NW_MAX = {"A": 1.4, "B": 3.5, "C": 3.0}  # Exhibit 24-7: the most lanes weaving vehicles can use
WEAVING_FLOW_MAX = {"A": 2800.0, "B": 4000.0, "C": 3500.0}  # pc/h, field limits (p24-9)
VR_MAX = {"B": 0.80, "C": 0.50}  # and Type A's by its lanes: 1.00, 0.45, 0.35, 0.20 for 2-5 (p24-9)
VR_MAX_A = {2: 1.00, 3: 0.45, 4: 0.35, 5: 0.20}
LOS_FREEWAY = (6.0, 12.0, 17.0, 22.0, 27.0)  # Exhibit 24-2, pc/km/ln: A-E upper bounds, F beyond
LOS_MULTILANE = (8.0, 15.0, 20.0, 23.0, 25.0)
FREEWAY_KMH = 90.0  # free-flow speed from which the freeway criteria apply (HCM freeways run 90-120)


def configuration(changes_ad: int, changes_bc: int) -> str | None:
    """Exhibit 24-5: the type from the lane changes each weaving movement needs; None where it isn't feasible."""
    small, large = sorted((changes_ad, changes_bc))
    if small == 0:
        return "B" if large <= 1 else "C"
    return "A" if large == 1 else None


def basic_capacity(ffs_kmh: float, multilane: bool) -> float:
    """A basic segment's capacity a lane, pc/h: freeway 2,400 at 120 km/h to "approximately 2,250" at 90 (HCM 2000
    Chapter 13), multilane 2,200, 2,100, 2,000, 1,900 at 100, 90, 80, 70 (Chapter 21); straight lines between."""
    if multilane:
        return 1900.0 + 10.0 * (min(max(ffs_kmh, 70.0), 100.0) - 70.0)
    return 2250.0 + 5.0 * (min(max(ffs_kmh, 90.0), 120.0) - 90.0)


def operate(flows: dict, lanes: int, length_m: float, ffs_kmh: float, kind: str, multilane: bool | None = None) -> dict:
    """The HCM's operational analysis. `flows`: {"vo1", "vo2", "vw1", "vw2"} pc/h (the peak 15 minutes' rate: the
    nonweaving and weaving flows, the larger of each first or not). Speeds km/h, density pc/km/ln."""
    multilane = ffs_kmh < FREEWAY_KMH if multilane is None else multilane
    vw, vnw = flows["vw1"] + flows["vw2"], flows["vo1"] + flows["vo2"]
    v = vw + vnw
    if v <= 0:
        return {"flow_pch": 0.0}
    ratio = vw / v
    length = min(length_m, MAX_WEAVE_M)

    def speeds(constrained: bool) -> list[float]:
        return [S_MIN_KMH + (ffs_kmh - 16.0) / (1 + a * (1 + ratio) ** b * (v / lanes) ** c / (3.28 * length) ** d)
                for a, b, c, d in CONSTANTS[kind][constrained]]

    sw, snw = speeds(False)
    if kind == "A":
        needed = 1.21 * lanes * ratio ** 0.571 * length ** 0.234 / sw ** 0.438
    elif kind == "B":
        needed = lanes * (0.085 + 0.703 * ratio + 71.57 / length - 0.0112 * (snw - sw))
    else:
        needed = lanes * (0.761 + 0.047 * ratio - 0.00036 * length - 0.0031 * (snw - sw))
    constrained = needed >= NW_MAX[kind]
    if constrained:
        sw, snw = speeds(True)
    speed = v / ((vw / sw if vw else 0.0) + (vnw / snw if vnw else 0.0))
    density = v / lanes / speed
    bounds = LOS_MULTILANE if multilane else LOS_FREEWAY
    return {"flow_pch": round(v), "volume_ratio": round(ratio, 3),
            "weaving_ratio": round(min(flows["vw1"], flows["vw2"]) / vw, 3) if vw else 0.0,
            "weaving_speed_kmh": round(sw, 1), "nonweaving_speed_kmh": round(snw, 1), "speed_kmh": round(speed, 1),
            "density_pc_km_ln": round(density, 1), "los": "ABCDEF"[sum(density > bound for bound in bounds)],
            "lanes_weaving_need": round(needed, 2), "lanes_weaving_max": NW_MAX[kind],
            "operation": "constrained" if constrained else "unconstrained"}


def capacity(flows: dict, lanes: int, length_m: float, ffs_kmh: float, kind: str,
             multilane: bool | None = None) -> dict:
    """The total flow, in these movements' proportions, at the LOS E/F density (p24-9: found by trial, here by
    bisection), capped by a basic segment's capacity and the field limits on weaving flow; and what bounds it."""
    multilane = ffs_kmh < FREEWAY_KMH if multilane is None else multilane
    v = sum(flows.values())
    if v <= 0:
        return {"capacity_pch": None}
    limit = (LOS_MULTILANE if multilane else LOS_FREEWAY)[-1]
    low, high = 0.0, 20000.0 / v
    for _ in range(60):
        scale = (low + high) / 2
        found = operate({k: value * scale for k, value in flows.items()}, lanes, length_m, ffs_kmh, kind, multilane)
        low, high = (scale, high) if found["density_pc_km_ln"] < limit else (low, scale)
    ratio = (flows["vw1"] + flows["vw2"]) / v
    bounds = {"density": low * v, "basic segment": basic_capacity(ffs_kmh, multilane) * lanes}
    if ratio:
        bounds["weaving flow"] = WEAVING_FLOW_MAX[kind] / ratio
    bound = min(bounds, key=bounds.get)
    return {"capacity_pch": round(bounds[bound]), "capacity_bound": bound}


def _lanes_between(entry_lanes: int, segment: int, exit_lanes: int, ramp_in: int, ramp_out: int) -> tuple[int, int]:
    """Lane changes (A to D, B to C): A the main road in, B the ramp in, C the main road out, D the ramp out. The
    ramps' lanes are the segment's kerbside ones (0 up), the main road's its offside ones."""
    return max(0, (segment - entry_lanes) - (ramp_out - 1)), max(0, (segment - exit_lanes) - (ramp_in - 1))


def _main_first(links: list) -> list:
    """The main road first: the faster, then the wider, then by id (a ramp is slower or narrower)."""
    return sorted(links, key=lambda link: (-link.speed, -len(link.lanes), link.id))


def segments(graph) -> list[dict]:
    """Every link from a merge to a diverge (each of two roads, at uncontrolled junctions, the second road joining
    or leaving on the kerb side) up to MAX_WEAVE_M: its lanes, length, legs and HCM type, or "ramp junctions"
    when it has no auxiliary lane."""
    network, found = graph.network, []
    for link in graph.links.values():
        start, end = network.nodes.get(link.start), network.nodes.get(link.end)
        if start is None or end is None or start.control != "uncontrolled" or end.control != "uncontrolled" or \
                link.length > MAX_WEAVE_M:
            continue
        entries = _main_first([into for into in graph.arriving[link.start] if any(
            after == link.id and turn.kind not in ("uturn", "offside") for (_, after), turn in into.turns.items())])
        exits = _main_first(list({turn.lane_out.link.id: turn.lane_out.link for turn in link.turns.values()
                                  if turn.kind not in ("uturn", "offside")}.values()))
        if len(entries) != 2 or len(exits) != 2:
            continue
        (main_in, ramp_in), (main_out, ramp_out) = entries, exits
        if not any(turn.lane_out.index == 0 for (_, after), turn in ramp_in.turns.items() if after == link.id):
            continue  # it doesn't join on the kerb side
        if not any(turn.lane_in.index == 0 for (_, after), turn in link.turns.items() if after == ramp_out.id):
            continue
        n = len(link.lanes)
        ad, bc = _lanes_between(len(main_in.lanes), n, len(main_out.lanes), len(ramp_in.lanes), len(ramp_out.lanes))
        auxiliary = n > len(main_in.lanes) and n > len(main_out.lanes)
        kind = configuration(ad, bc) if auxiliary else None
        found.append({"link": link.id, "road": link.road, "length_m": round(link.length, 1), "lanes": n,
                      "from": main_in.id, "joining": ramp_in.id, "to": main_out.id, "leaving": ramp_out.id,
                      "lane_changes": {"main_to_exit": ad, "entry_to_main": bc},
                      "type": kind if auxiliary else "ramp junctions",
                      "ffs_kmh": round((main_in.speed + main_out.speed) / 2 * 3.6, 1)})
    return found


def limits(segment: dict) -> dict:
    """What its type can take (p24-9): the weaving flow and the share of the flow that weaves."""
    kind = segment["type"]
    if kind not in CONSTANTS:
        return {}
    ratio = VR_MAX_A.get(segment["lanes"], 0.20 if segment["lanes"] > 5 else 1.0) if kind == "A" else VR_MAX[kind]
    return {"max_weaving_pch": WEAVING_FLOW_MAX[kind], "max_volume_ratio": ratio}


def movement_flows(graph, segment: dict, rates: dict) -> dict:
    """pc/h of its four movements: {(origin, destination): pc/h} on the routes a Run takes (route_shares)."""
    keys = {(segment["from"], segment["to"]): "vo1", (segment["joining"], segment["leaving"]): "vo2",
            (segment["from"], segment["leaving"]): "vw1", (segment["joining"], segment["to"]): "vw2"}
    flows = dict.fromkeys(keys.values(), 0.0)
    for (origin, destination), rate in rates.items():
        for share, route in graph.route_shares(origin, destination):
            ids = [link.id for link in route]
            if segment["link"] in ids:
                at = ids.index(segment["link"])
                if 0 < at < len(ids) - 1 and (ids[at - 1], ids[at + 1]) in keys:
                    flows[keys[(ids[at - 1], ids[at + 1])]] += rate * share
    return flows


def report(graph, rates: dict) -> list[dict]:
    """Every weaving segment analysed at these rates ({(origin, destination): pc/h}), most dense first."""
    rows = []
    for segment in segments(graph):
        row = {**segment, **limits(segment)}
        if segment["type"] in CONSTANTS:
            flows = movement_flows(graph, segment, rates)
            row["movements_pch"] = {k: round(v) for k, v in flows.items()}
            args = (flows, segment["lanes"], segment["length_m"], segment["ffs_kmh"], segment["type"])
            row.update(operate(*args))
            if row.get("flow_pch"):
                row.update(capacity(*args))
                row["v_c"] = round(row["flow_pch"] / row["capacity_pch"], 2)
                warnings = []
                if row["volume_ratio"] > row["max_volume_ratio"]:
                    warnings.append(f"volume ratio {row['volume_ratio']} over the {row['max_volume_ratio']} its type "
                                    "takes: worse than predicted, may fail")
                if flows["vw1"] + flows["vw2"] > row["max_weaving_pch"]:
                    warnings.append(f"weaving flow over {row['max_weaving_pch']:.0f} pc/h: likely to fail")
                if segment["type"] == "C" and row["weaving_ratio"] > 0.40:
                    warnings.append("Type C weaving ratio over 0.40")
                if segment["ffs_kmh"] < 70:
                    warnings.append("free-flow speed under 70 km/h: outside the HCM's calibration (freeways and "
                                    "multilane highways)")
                if warnings:
                    row["warnings"] = warnings
        rows.append(row)
    return sorted(rows, key=lambda row: -(row.get("density_pc_km_ln") or 0))


def measured(row: dict, observed: dict, hours: float) -> dict:
    """The run's own figures on the segment's link: traversals' flow, space-mean speed and density a lane."""
    count, mean_s = (observed.get("traversals") or {}).get(row["link"], (0, 0.0))
    if count < 3 or not mean_s or hours <= 0:
        return {}
    speed = row["length_m"] / mean_s * 3.6
    flow = count / hours
    return {"simulated": {"flow_vph": round(flow), "speed_kmh": round(speed, 1),
                          "density_veh_km_ln": round(flow / speed / row["lanes"], 1), "traversals": count}}
