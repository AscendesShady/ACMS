"""The OD matrix's demand as data (no Qt): its cells, the vehicle-type split arithmetic, the demand tools' argument
rules and read-out, and a run's pair demand.

od_matrix.py's grid and OD Pair editor show and edit these cells; AI Mode's demand tools still work that editor's
controls (ODCellPanel.command), so the user sees the values arrive, but read their arguments, check them and report
the matrix through the functions here. A cell's split is stored in the vehicle types' order; the split in effect
(split_of) fixes each timetabled type at its vehicles over the pair's trips, the others sharing the rest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from math import floor

from .daily_profile import (ARRIVAL_PATTERNS, DEGREES, HOURS, MIN_GAP_H, DailyProfile, phf_of, phf_quarters,
                            quarter_shares)

DEFAULT_SPLIT = (10, 85, 5)  # the three starting vehicle types' shares (bus, car, bike)
UNITS = {"hourly": "veh/h", "daily": "veh/day"}

@dataclass
class ODCell:
    flow: int | None = None  # hourly demand, veh/h; None until entered
    daily: int | None = None  # daily demand, trips per day; None until entered
    profile: DailyProfile = field(default_factory=DailyProfile)  # how the day's trips spread
    pattern: str = "Random"
    split: tuple[int, ...] = DEFAULT_SPLIT  # percent per vehicle type, in Mode Behavior's order; sums to 100
    quarters: tuple[float, ...] = ()  # hourly demand: each 15 minutes' share of the hour (its PHF); () even
    # Trips that end part way: (vehicle type index, where as a share of the trip's length 0-1, % of that type's
    # trips ending there), sorted; whatever no point takes reaches the destination
    drops: tuple[tuple[int, float, int], ...] = ()

def whole_percent(weights, total: int = 100) -> list[int]:
    """Whole numbers in proportion to `weights` that add up to `total`; the largest remainders
    get the leftover points. All-zero weights give everything to the first."""
    weights = list(weights)
    if not weights:
        return []
    if sum(weights) <= 0:
        return [total, *([0] * (len(weights) - 1))]
    exact = [weight * total / sum(weights) for weight in weights]
    shares = [floor(value) for value in exact]
    for i in sorted(range(len(weights)), key=lambda i: exact[i] - shares[i], reverse=True)[:total - sum(shares)]:
        shares[i] += 1
    return shares


def redistribute(split: tuple[int, ...], index: int) -> tuple[int, ...]:
    """`split` without mode `index`: its share goes to the others in proportion to theirs, in
    whole percent that still add up to 100 (largest remainders get the leftover points)."""
    return tuple(whole_percent(share for i, share in enumerate(split) if i != index))


def drops_after(drops: tuple, action: str, index: int) -> tuple:
    """A cell's drop-offs once Mode Behavior adds or deletes vehicle type `index`: a deleted type's points go and
    later types move down one; a new type (always last) has none."""
    if action != "deleted":
        return drops
    return tuple((kind - (kind > index), at, share) for kind, at, share in drops if kind != index)


def drops_problem(drops, types: int | None) -> str | None:
    """Why a saved pair's drop-offs can't be read (None: they can): each [type index, share of the trip 0-1 (not
    its ends), whole %], a type's points adding up to 100 % at most."""
    if not isinstance(drops, list):
        return "its drop-offs must be a list"
    taken: dict[int, int] = {}
    for point in drops:
        if not (isinstance(point, list) and len(point) == 3 and isinstance(point[0], int) and point[0] >= 0
                and (types is None or point[0] < types) and isinstance(point[1], (int, float)) and 0 < point[1] < 1
                and isinstance(point[2], int) and 0 < point[2] <= 100):
            return "each drop-off needs a vehicle type, where along the trip (between 0 and 1) and a whole %"
        taken[point[0]] = taken.get(point[0], 0) + point[2]
    return "a vehicle type's drop-offs add up to more than 100 %" if any(v > 100 for v in taken.values()) else None


MAX_DROP_PAIRS = 20  # set_od_demand's drop_offs: pairs at a time (each needs its own route found)


def drops_from(given, names: list[str], route, pair: tuple, drops: tuple = ()) -> tuple:
    """set_od_demand's drop_offs for one pair, as the drop-off slider would hold them: [{mode, at, percent}], `at` a
    junction on the pair's fastest route (`route`: its metres and (share, junction) list, the window's
    _route_along; None: no route) or a share of the trip 0-1. They replace the pair's `drops` of the types named,
    the others' stay; [] clears them all. ValueError says what to fix."""
    given = listed(given)
    if not given:
        return ()
    if route is None:
        raise ValueError(f"No route from {pair[0]} to {pair[1]}, so nowhere to drop off. Nothing was changed.")
    stops = {junction: share for share, junction in route[1]}
    points = []
    for item in given:
        item = item if isinstance(item, dict) else {}
        kind = next((i for i, name in enumerate(names) if name.lower() == str(item.get("mode", "")).strip().lower()),
                    None)
        if kind is None:
            raise ValueError(f"No vehicle type called {item.get('mode')!r}. The types are {', '.join(names)}.")
        at = str(item.get("at", "")).strip()
        try:
            at = stops[at] if at in stops else float(at)
        except ValueError:
            at = None
        if at is None or not 0 < at < 1:
            listed_stops = ", ".join(f"{junction} ({share:.0%})" for junction, share in list(stops.items())[:15])
            raise ValueError(f"{item.get('at')!r} isn't on {pair[0]}->{pair[1]}'s route. Its junctions, at their "
                             f"share of the trip: {listed_stops or 'none'}; or give a share between 0 and 1.")
        share = item.get("percent")
        points.append([kind, round(at, 4), int(share) if isinstance(share, (int, float)) and share == int(share)
                       else share])
    kinds = {point[0] for point in points}
    merged = [list(point) for point in drops if point[0] not in kinds] + points
    problem = drops_problem(merged, len(names))
    if problem:
        raise ValueError(f"{pair[0]}->{pair[1]}: {problem}. Nothing was changed.")
    return tuple(sorted(tuple(point) for point in merged))


def drops_record(drops: tuple, names: list[str]) -> dict:
    """read_od_matrix's drop-offs: per vehicle type, each point's share of the trip's length and its % of trips."""
    record: dict = {}
    for kind, at, share in drops:
        if kind < len(names):
            record.setdefault(names[kind], []).append({"at_share_of_trip": round(at, 3), "percent": share})
    return record


def fixed_split(split: tuple[int, ...], vehicles: dict[int, float], trips: int | None) -> tuple[int, ...]:
    """`split` with each timetabled type's share fixed at its `vehicles` (index -> vehicles in
    the same period as `trips`) over the pair's `trips`; the other types share the rest in
    proportion to their current shares. Unchanged while there are no trips or no timetables;
    timetabled vehicles beyond the trips take all of them."""
    if not vehicles or not trips or trips <= 0:
        return tuple(split)
    timed = sorted(vehicles)
    free = [i for i in range(len(split)) if i not in vehicles]
    timed_total = 100 if not free else min(100, round(sum(vehicles.values()) * 100 / trips))
    result = [0] * len(split)
    for i, share in zip(timed, whole_percent([vehicles[i] for i in timed], timed_total)):
        result[i] = share
    for i, share in zip(free, whole_percent([split[i] for i in free], 100 - timed_total)):
        result[i] = share
    return tuple(result)

def quarters_record(cell: ODCell) -> dict:
    """An hourly pair's peak-hour factor, and its 15-minute shares of the hour when they aren't even."""
    return {"peak_hour_factor": round(phf_of(cell.quarters), 3),
            **({"quarters": [round(share, 4) for share in cell.quarters]} if cell.quarters else {})}


def lead_with_what_was_kept(info: dict, share: float, kept: float) -> None:
    """A demand the check cut: the result starts with what the matrix now holds (qwen3.5 reported the 6,420 veh/h
    made as the demand when 47 were kept), the made total renamed; under half kept, the busiest junctions that
    only give way are named with what could let more hold (the model decides: signalise them, or main roads)."""
    made = info.pop("total_made", None)
    if made is None:  # scale_demand's: its total after scaling, and the factor that the check then cut too
        made = info.pop("total_after", None)
        if isinstance(info.get("factor"), (int, float)):
            info["factor"] = round(info["factor"] * share, 3)
    rest = dict(info)
    info.clear()
    info.update({"total_now": kept, "kept_after_check": f"{share:.0%} of {made} (the rest jammed in a run)",
                 **rest, "total_before_check": made})
    if share < 0.5:
        giving_way = [node for node, control in (info.get("junction_controls") or {}).items() if control == "uncontrolled"]
        if giving_way:
            info["to_hold_more"] = (f"The busiest junctions only give way: {', '.join(giving_way)}. Signalising them "
                                    "(set_junction_control, control 'signalised'), or main roads only "
                                    "(network_from_background, road_classes 'main'), may let more demand hold; then "
                                    "generate_demand again. Tell the user what was kept either way.")


def parsed(value):
    """A tool argument as sent, or decoded when a small model sent a list or object as JSON text."""
    if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def listed(value) -> list:
    """End names as a list: ["1W", "2W"], JSON text of one, or a single name."""
    value = parsed(value)
    return [] if value in (None, "") else value if isinstance(value, list) else [value]


def profile_from(args: dict) -> DailyProfile:
    """The curve a shape_daily_curve call describes: points, a polynomial or a preset."""
    points, polynomial, preset = parsed(args.get("points")), parsed(args.get("polynomial")), args.get("preset")
    if sum(item is not None and item != [] for item in (points, polynomial, preset)) != 1:
        raise ValueError("Give exactly one of points, polynomial or preset.")
    if preset:
        if preset not in ("weekday", "flat"):
            raise ValueError("preset must be 'weekday' or 'flat'.")
        return DailyProfile() if preset == "weekday" else DailyProfile("points", [(0.0, 1.0), (24.0, 1.0)])
    if polynomial is not None:
        try:
            coefficients = [float(a) for a in polynomial]
        except (TypeError, ValueError):
            raise ValueError("polynomial must be a list of numbers [a0, a1, ..., an].") from None
        if not 2 <= len(coefficients) <= max(DEGREES) + 1:
            raise ValueError(f"polynomial needs {min(DEGREES) + 1} to {max(DEGREES) + 1} coefficients (degree "
                             f"{min(DEGREES)} to {max(DEGREES)}).")
        return DailyProfile("poly", [], coefficients + [0.0] * (min(DEGREES) + 1 - len(coefficients)))
    try:
        pairs = sorted((float(p["hour"]), float(p["level"])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
                       for p in points)
    except (TypeError, ValueError, KeyError, IndexError):
        raise ValueError("points must be [[hour, level], ...] with hours 0-24 and levels 0-1.") from None
    if len(pairs) < 2 or not all(0 <= hour <= HOURS for hour, _ in pairs) or min(level for _, level in pairs) < 0:
        raise ValueError("points need at least two [hour, level] pairs, hours 0-24, levels not below zero.")
    top = max(level for _, level in pairs)
    if top <= 0:
        raise ValueError("At least one point needs a level above zero.")
    pairs = [(hour, level / top) for hour, level in pairs] if top > 1 else pairs  # relative levels are fine
    if pairs[0][0] > 0:
        pairs.insert(0, (0.0, pairs[0][1]))  # the curve runs from midnight...
    if pairs[-1][0] < HOURS:
        pairs.append((float(HOURS), pairs[-1][1]))  # ...to midnight
    kept = [pairs[0]]
    for hour, level in pairs[1:]:
        if hour - kept[-1][0] >= MIN_GAP_H:
            kept.append((hour, level))
        elif hour == HOURS:
            kept[-1] = (hour, level)  # keep the midnight end
    return DailyProfile("points", kept)


def curve_record(profile: DailyProfile) -> dict:
    if profile.kind == "poly":
        return {"kind": "polynomial", "coefficients": [round(a, 4) for a in profile.coefficients]}
    return {"kind": "points", "points": [[round(hour, 2), round(level, 3)] for hour, level in profile.points]}


def split_of(cell: ODCell, per_hour: dict[int, float], mode: str) -> tuple[int, ...]:
    """The split in effect: each timetabled type (`per_hour`: index -> vehicles an hour, from its headway) fixed at
    its vehicles over the pair's trips, per hour against the hourly flow in `mode` "hourly", per day against the
    daily trips (running all 24 hours) in "daily" (the other kind when the pair has none of this one); the other
    types share the rest in their stored proportions. Stored splits keep those proportions, so no flow typed on
    the way loses them."""
    if cell.flow is not None and (mode == "hourly" or cell.daily is None):
        return fixed_split(cell.split, per_hour, cell.flow)
    return fixed_split(cell.split, {i: count * 24 for i, count in per_hour.items()}, cell.daily)


def pairs_from(args: dict, origins: list[str], destinations: list[str]) -> list[tuple[str, str]]:
    """The (origin, destination) pairs a tool names: `pairs` like "1W->1E", or every pair
    from `origins` and/or to `destinations` (one of them left out means all ends); none named
    means every pair."""
    named = parsed(args.get("pairs"))
    if named:
        pairs = []
        for item in named if isinstance(named, list) else [named]:
            # "1W->1E", and as small models write it: "1W-1E", "1W->>1E", "1W=>1E", "1W to 1E" (no end has a "-")
            parts = item if isinstance(item, (list, tuple)) else \
                re.split(r"\s*(?:-+>*|=+>|→|>+|\bto\b|,)\s*", str(item).strip())
            if len(parts) != 2:
                raise ValueError(f"Can't read the pair {item!r}; write it as \"origin->destination\", e.g. \"1W->1E\".")
            pairs.append((str(parts[0]).strip(), str(parts[1]).strip()))
    else:
        chosen_o = [str(end) for end in listed(args.get("origins")) or origins]
        chosen_d = [str(end) for end in listed(args.get("destinations")) or destinations]
        pairs = [(o, d) for o in chosen_o for d in chosen_d if o != d]
    unknown = sorted({o for o, _ in pairs if o not in origins} | {d for _, d in pairs if d not in destinations})
    if unknown:
        raise ValueError(f"No such origin or destination: {', '.join(unknown)}. Origins: {', '.join(origins)}; "
                         f"destinations: {', '.join(destinations)}.")
    pairs = [pair for pair in dict.fromkeys(pairs) if pair[0] != pair[1]]
    if not pairs:
        raise ValueError("A trip can't start and end at the same boundary end.")
    return pairs


def demand_plan(args: dict, shown: str, names: list[str]) -> tuple:
    """(demand, value, pattern, shares, quarters) from set_od_demand's arguments, checked; nothing changes. `shown`:
    the demand the matrix shows (the default); `names`: the vehicle types, in order."""
    demand = args.get("demand") or shown
    if demand not in UNITS:
        raise ValueError("demand must be 'hourly' (veh/h) or 'daily' (trips per day).")
    value, pattern, split = args.get("value"), args.get("arrival_pattern"), parsed(args.get("mode_split"))
    split = split if split not in ([], {}) else None  # empty: llama3.2's filler, no split given
    phf, quarters = args.get("peak_hour_factor"), parsed(args.get("quarters"))
    if value is None and not pattern and split is None and phf is None and quarters is None \
            and args.get("drop_offs") is None:
        raise ValueError("Give a value, an arrival_pattern, a mode_split, drop_offs and/or a peak_hour_factor or "
                         "quarters.")
    if phf is not None or quarters is not None:
        if demand == "daily":
            raise ValueError("A peak-hour factor or 15-minute quarters apply to hourly demand only (the daily "
                             "curve shapes daily demand).")
        if phf is not None and quarters is not None:
            raise ValueError("Give peak_hour_factor or quarters, not both.")
        if phf is not None and not isinstance(phf, (int, float)):
            raise ValueError("peak_hour_factor must be a number, 0.25 to 1.")
        quarters = phf_quarters(phf) if phf is not None else quarter_shares(quarters)
    if value is not None:
        value = round(float(value))
        top = 2_000_000 if demand == "daily" else 100_000
        if not 0 <= value <= top:
            raise ValueError(f"{demand} demand must be 0 to {top:,} {UNITS[demand]}.")
    if pattern:
        matches = [name for name in ARRIVAL_PATTERNS if name.lower() == str(pattern).lower().replace("_", " ")]
        if not matches:
            raise ValueError(f"arrival_pattern must be one of {', '.join(ARRIVAL_PATTERNS)}.")
        pattern = matches[0]
    if split is not None:
        shares = shares_from(split, names)
    return demand, value, pattern, None if split is None else shares, quarters


def shares_from(split, names: list[str]) -> list[int]:
    """Whole percent per vehicle type, in Mode Behavior's order (`names`), from a mode_split given as
    {name: percent}, [{"mode": name, "percent": n}, ...] or one number per type. Types left out get 0.
    Fractions adding up to 1 are read as percent (llama3.2 sent [0.4, 0.6])."""
    if isinstance(split, list) and split and all(isinstance(item, dict) for item in split):
        split = {str(item.get("mode", "")): item.get("percent", 0) for item in split}
    if isinstance(split, dict):
        given = {str(name).strip().lower(): share for name, share in split.items()}
        unknown = [name for name in given if name not in [known.lower() for known in names]]
        if unknown:
            raise ValueError(f"No vehicle type called {', '.join(unknown)}. The types are {', '.join(names)} "
                             "(set in the Mode Behavior tab).")
        split = [given.get(name.lower(), 0) for name in names]
    try:
        given = [float(share) for share in split]
    except (TypeError, ValueError):
        raise ValueError(f"mode_split must give a percent for the vehicle types {', '.join(names)}.") from None
    scale = 100 if given and abs(sum(given) - 1) < 0.01 else 1
    # Rounded together, not each on its own: 12.5 / 82.5 / 5 and thirds still add up to 100
    if len(given) != len(names) or min(given, default=0) < 0 or abs(sum(given) * scale - 100) > 0.5:
        raise ValueError(f"mode_split must give {len(names)} percentages, one each for {', '.join(names)} in that "
                         f"order, adding up to 100 (got {[f'{share:g}' for share in given]}).")
    return whole_percent(given)


def demand_now(cells: dict, origins: list[str], destinations: list[str], demand: str,
               scope: list) -> tuple[dict, dict | None]:
    """The matrix's demand of this kind ({pair: veh/h or trips/day}) and, for daily demand, the 24
    hourly shares of those pairs and of `scope` (a new pair's: the default curve's)."""
    daily = demand == "daily"
    values = {pair: cell.daily if daily else cell.flow for pair, cell in cells.items()
              if pair[0] in origins and pair[1] in destinations
              and (cell.daily if daily else cell.flow)}
    shares = {pair: cells.get(pair, ODCell()).profile.shares() for pair in {*values, *scope}} if daily else None
    return values, shares


def read_od(cells: dict, origins: list[str], destinations: list[str], shown: str, names: list[str],
            per_hour: dict[int, float]) -> dict:
    """read_od_matrix, compact for small models: every pair's values in one map per kind, what most pairs
    share (pattern, split, daily curve) once under "usual", and only the pairs that differ under "different"."""
    entered = {f"{o}->{d}": cell for (o, d), cell in cells.items()
               if o in origins and d in destinations and (cell.flow is not None or cell.daily is not None)}
    options = {pair: {"arrival_pattern": cell.pattern, "mode_split": dict(zip(names, split_of(cell, per_hour, shown))),
                      **({"daily_curve": curve_record(cell.profile)} if cell.daily is not None else {}),
                      **({"quarters": [0.25] * 4, **quarters_record(cell)} if cell.flow is not None else {}),
                      **({"drop_offs": drops_record(cell.drops, names)} if cell.drops else {})}
               for pair, cell in entered.items()}
    usual: dict = {}
    for key in ("arrival_pattern", "mode_split", "daily_curve", "peak_hour_factor", "quarters"):
        seen = [json.dumps(option[key]) for option in options.values() if key in option]
        if seen:
            usual[key] = json.loads(max(set(seen), key=seen.count))
    different = {pair: {key: value for key, value in option.items() if usual.get(key) != value}
                 for pair, option in options.items()}
    total = sum(1 for o in origins for d in destinations if o != d)
    return {"showing": shown, "vehicle_types": names, "origins": origins,
            "destinations": destinations, "pairs_total": total, "pairs_with_data_count": len(entered),
            # the whole matrix, so a "how much demand" question isn't a small model's sum of dozens of pairs
            "total_hourly_vph": round(sum(cell.flow or 0 for cell in entered.values())),
            "total_daily_trips": round(sum(cell.daily or 0 for cell in entered.values())),
            "hourly_vph": {pair: cell.flow for pair, cell in entered.items() if cell.flow is not None},
            "daily_trips": {pair: cell.daily for pair, cell in entered.items() if cell.daily is not None},
            "usual": usual, "different": {pair: values for pair, values in different.items() if values}}


def pair_demand(cells: dict, origins: list[str], destinations: list[str], mode: str,
                per_hour: dict[int, float]) -> list:
    """Every OD pair with demand of the kind `mode` ("hourly" or "daily") between these ends, as a run's
    simulation.PairDemand (the split in effect, a daily pair's hourly shares, an hourly pair's quarters)."""
    from .simulation import PairDemand  # noqa: PLC0415 - the simulation's type
    daily = mode == "daily"
    return [PairDemand(origin, destination, None if daily else cell.flow, cell.daily if daily else None,
                       cell.profile.shares() if daily else [], cell.pattern, split_of(cell, per_hour, mode),
                       quarters=() if daily else cell.quarters, drops=cell.drops)
            for (origin, destination), cell in cells.items()
            if origin in origins and destination in destinations
            and (cell.daily if daily else cell.flow)]
