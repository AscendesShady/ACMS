"""Institutions: buildings that make trips, placed on the map from the Demand Generator's
Institutions mode (homes, a school, a hospital, offices, a shop). No Qt.

Placing one works like an RTS game's build placement (Red Alert 2): the footprint follows the
mouse, green where it can stand and red on a road, a junction or another building; the first
click sets it down, the wheel sizes its circle of influence in metres, the second click
finishes (`Placement`).

The four-step model, each building a zone:
- generation (`hourly_people`): everyone makes one trip each way a day, at the shift's times;
- distribution (`door`, `catchment`): the circle of influence is the catchment its people come from. Its
  junctions and boundary ends share them equally, and they travel to and from the building's door, its
  nearest junction;
- mode split (`mode_shares`, from network_tools.institution_split's logit, else `car_share` by car): walkers
  make no vehicle trip; the others' vehicles are people / occupancy; bus riders ride the timetabled buses;
- assignment: `run_demand` turns them into OD pairs (catchment -> door arriving, door -> catchment leaving)
  with the building's daily profile, which a Run spawns like any other.
The figures entered drive it; the kinds' defaults are illustrative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import hypot, sqrt

from .geometry import polyline_meets_box
from .models import (INSTITUTION_KINDS, MAX_AREA_M2, MAX_INFLUENCE_M, Institution, Network,
                     institution_problem)
from .roundabouts import design_roundabout

HOURS = 24
WALK = "Walk"  # the mode that makes no vehicle trip
VERGE_M = 3.0  # buildings keep this far off every carriageway (a bus bay's width) and junction
GROW = 1.15  # one wheel step's change to the circle of influence


@dataclass(frozen=True)
class Kind:
    glyph: int  # its icon: a Segoe Fluent Icons / MDL2 Assets code point
    people: str  # what its people count is
    together: bool  # its people come at the shift's start and go at its end (else they come and go all through it)
    home: bool  # its people live here: they leave at the shift's start and come back at its end
    defaults: tuple  # area m², people, employees, shift start and end (hours), % by car


KINDS = {
    "Residential": Kind(0xE80F, "Residents", True, True, (4000, 400, 0, 8.0, 17.5, 60)),
    "School": Kind(0xE7BE, "Students", True, False, (6000, 600, 50, 8.5, 15.5, 30)),
    "Hospital": Kind(0xE95E, "Patients and visitors a day", False, False, (12000, 800, 300, 7.0, 19.0, 70)),
    "Office": Kind(0xE821, "Visitors a day", False, False, (5000, 50, 400, 9.0, 17.0, 60)),
    "Shop": Kind(0xE719, "Customers a day", False, False, (3000, 1500, 40, 9.0, 21.0, 70)),
}
assert tuple(KINDS) == INSTITUTION_KINDS


def footprint_problem(network: Network, x: float, y: float, area_m2: float) -> str | None:
    """What a square building this big centred at (x, y) would stand on ("road 1,1-2,1", "junction
    2,1", "IN3"), or None where it can go. Roads count their carriageway, junctions their box or
    roundabout, each with VERGE_M round it."""
    half = sqrt(area_m2) / 2
    widest: dict[str, float] = {}  # each junction's widest road
    for road in network.all_roads():
        width = road.width_m
        # ponytail: the footprint grown by the half width is a square, not rounded at its corners, so it
        # keeps a little farther off a road passing a corner diagonally than it must.
        clear = half + width / 2 + VERGE_M
        if polyline_meets_box(network.road_points(road), x - clear, y - clear, x + clear, y + clear):
            return f"road {road.id}"
        for end in (road.from_node, road.to_node):
            widest[end] = max(widest.get(end, 0.0), width)
    for node in network.nodes.values():
        # A roundabout's circle, or a junction box (1.28 road widths across, as drawn): its half diagonal.
        reach = (design_roundabout(network, node.id).inscribed_diameter_m / 2 if node.control == "roundabout"
                 else 0.91 * widest.get(node.id, 0.0)) + VERGE_M
        if hypot(max(abs(node.x - x) - half, 0.0), max(abs(node.y - y) - half, 0.0)) < reach:
            return f"junction {node.id}"
    for other in network.institutions.values():
        if max(abs(other.x - x), abs(other.y - y)) < half + sqrt(other.area_m2) / 2:
            return other.id
    return None


def links(network: Network, x: float, y: float, radius_m: float) -> list[str]:
    """The junctions and boundary ends within a circle of influence, nearest first."""
    reached = sorted((hypot(node.x - x, node.y - y), node.id) for node in network.all_nodes())
    return [node_id for distance, node_id in reached if distance <= radius_m]


def hourly_trips(item: Institution) -> tuple[list[float], list[float]]:
    """Arrivals at and departures from the building by car in each hour of the day (people a hour: the
    car share of `hourly_people`; with a mode split, its Car share)."""
    car = shares(item).get("Car", 0.0) / 100
    arrive, leave = hourly_people(item)
    return [v * car for v in arrive], [v * car for v in leave]


def hourly_people(item: Institution) -> tuple[list[float], list[float]]:
    """People arriving at and leaving the building in each hour of the day, by any mode. Everyone makes
    one trip each way a day, in the hour it happens: employees arrive in the hour the shift starts and
    leave in the hour it ends; so do people who come together (students), while visitors and customers
    come and go evenly all through the shift; residents leave at its start and come back at its end."""
    kind = KINDS[item.kind]
    arrive, leave = [0.0] * HOURS, [0.0] * HOURS
    start, end = int(item.shift_start_h) % HOURS, int(item.shift_end_h) % HOURS
    staff, people = float(item.employees), float(item.people)
    arrive[start] += staff
    leave[end] += staff
    if not kind.together:
        for hour, share in enumerate(_through(item.shift_start_h, item.shift_end_h)):
            arrive[hour] += people * share
            leave[hour] += people * share
    elif kind.home:
        leave[start] += people
        arrive[end] += people
    else:
        arrive[start] += people
        leave[end] += people
    return arrive, leave


def _through(start: float, end: float) -> list[float]:
    """Each hour's share of the time from start to end, past midnight if end comes first."""
    length = (end - start) % HOURS
    return [sum(max(0.0, min(hour + 1 + day, start + length) - max(hour + day, start)) for day in (0, HOURS)) / length
            for hour in range(HOURS)]


def door(network: Network, item: Institution) -> str | None:
    """The building's door: the junction nearest it (not a boundary end), where its trips start and end."""
    nearest = min(network.nodes.values(), key=lambda node: (round(hypot(node.x - item.x, node.y - item.y), 6), node.id),
                  default=None)  # equally near: the first by name, as links orders them
    return nearest.id if nearest is not None else None


def catchment(network: Network, item: Institution) -> list[str]:
    """Where its people come from: the junctions and boundary ends inside its circle, nearest first, but
    the door."""
    entrance = door(network, item)
    return [node_id for node_id in links(network, item.x, item.y, item.radius_m) if node_id != entrance]


def shares(item: Institution) -> dict[str, float]:
    """Percent of its people by mode: its mode split, else its car share by Car and the rest on foot."""
    return dict(item.mode_shares) or {"Car": item.car_share, WALK: 100 - item.car_share}


def vehicle_split(item: Institution, modes: list) -> tuple[float, list[float], list[str]]:
    """Vehicles per person (the sum of share / occupancy over the untimetabled types), each type's share of
    those vehicles (in `modes`' order, as a pair's split), and the modes it can't put on the road: names no
    type has. Walkers and timetabled riders (they ride the buses) add no vehicle."""
    by_name = {mode.name.lower(): (index, mode) for index, mode in enumerate(modes)}
    vehicles, unknown = [0.0] * len(modes), []
    for name, percent in shares(item).items():
        found = by_name.get(name.lower())
        if found is None:
            if name != WALK and percent:
                unknown.append(name)
            continue
        index, mode = found
        if not mode.headway_min:
            vehicles[index] += percent / 100 / max(getattr(mode, "occupancy", 1.0), 1e-9)
    total = sum(vehicles)
    return total, [v / total for v in vehicles] if total else vehicles, unknown


def run_demand(network: Network, modes: list, daily: bool) -> tuple[list, list[str]]:
    """Every institution's trips as OD pairs for a Run (simulation.PairDemand), and notes on what couldn't
    be: catchment -> door for arrivals, door -> catchment for departures, each catchment junction or end an
    equal share, its split by vehicle type, no buses of its own (`timetabled` False: riders take the
    network's). Daily: the trips a day spread over the day in the building's own hourly profile. Hourly:
    each pair's flow in the design hour, the hour when all institutions together make the most vehicle trips."""
    from .simulation import PairDemand  # noqa: PLC0415 - the simulation's type

    made, notes = [], []
    for item in network.institutions.values():
        per_person, split, unknown = vehicle_split(item, modes)
        if unknown:
            notes.append(f"{item.id}: no vehicle type called {', '.join(unknown)}, so those trips are left out.")
        entrance, origins = door(network, item), catchment(network, item)
        if not per_person:
            continue
        if entrance is None or not origins:
            notes.append(f"{item.id}: its circle reaches no junction or end other than its door, so its trips have "
                         "nowhere to come from: make its circle bigger.")
            continue
        arrive, leave = hourly_people(item)
        for profile, pairs in ((arrive, [(o, entrance) for o in origins]), (leave, [(entrance, d) for d in origins])):
            vehicles = [v * per_person / len(origins) for v in profile]
            if sum(vehicles):
                made += [(origin, destination, vehicles, split) for origin, destination in pairs]
    if not made:
        return [], notes
    hour = max(range(HOURS), key=lambda h: sum(row[2][h] for row in made))
    demand = [PairDemand(o, d, None, sum(v), [x / sum(v) for x in v], "Poisson", tuple(split), timetabled=False)
              if daily else PairDemand(o, d, v[hour], None, [], "Poisson", tuple(split), timetabled=False)
              for o, d, v, split in made if daily or v[hour]]
    if not daily:
        notes.append(f"Institutions' trips are taken at {hour:02d}:00-{hour + 1:02d}:00, their busiest hour together.")
    return demand, notes


def place(network: Network, kind: str, x: float, y: float, area_m2: float, radius_m: float,
          people: int = 0, employees: int = 0, shift_start_h: float = 8.0, shift_end_h: float = 17.0,
          car_share: float = 60.0) -> Institution:
    """Add a building, named IN1, IN2... Everything is checked first: a refusal (ValueError, saying
    why) changes nothing."""
    number = len(network.institutions) + 1
    while f"IN{number}" in network.institutions:
        number += 1
    item = Institution(f"IN{number}", kind, float(x), float(y), float(area_m2), float(radius_m), int(people),
                       int(employees), float(shift_start_h), float(shift_end_h), float(car_share))
    if (problem := institution_problem(item)) is not None:
        raise ValueError(problem)
    if (problem := footprint_problem(network, x, y, area_m2)) is not None:
        raise ValueError(f"A building can't stand on {problem}.")
    network.institutions[item.id] = item
    return item


@dataclass
class Placement:
    """A building being placed: `move` while its footprint follows the mouse, `click` to set it down
    (refused where it can't stand), `grow` with the wheel to size its circle, `click` again to finish."""
    area_m2: float
    x: float | None = None  # where the footprint is; None until the mouse is on the map
    y: float | None = None
    radius_m: float | None = None  # the circle of influence once set down; None while it follows the mouse
    problem: str | None = None  # what it would stand on here
    links: list[str] = field(default_factory=list)  # the junctions and ends its circle reaches

    def move(self, network: Network, x: float, y: float) -> None:
        if self.radius_m is None:
            self.x, self.y = x, y
            self.problem = footprint_problem(network, x, y, self.area_m2)

    def resize(self, network: Network, area_m2: float) -> None:
        self.area_m2 = min(max(area_m2, 1.0), MAX_AREA_M2)
        if self.x is not None:
            self.problem = footprint_problem(network, self.x, self.y, self.area_m2)
        if self.radius_m is not None:
            self.grow(network, 0)

    def click(self, network: Network) -> bool:
        """Set it down, or finish: True once it is done (clear ground and a circle set)."""
        if self.x is None or self.problem is not None:
            return False
        if self.radius_m is None:
            self.radius_m = sqrt(self.area_m2) / sqrt(2) + 25  # just past its corners, to start
            self.grow(network, 0)
            return False
        return True

    def grow(self, network: Network, steps: int) -> bool:
        """The wheel: each step up makes the circle GROW times bigger, each step down smaller. False
        while there is no circle yet."""
        if self.radius_m is None:
            return False
        self.radius_m = min(max(self.radius_m * GROW ** steps, sqrt(self.area_m2) / 2), MAX_INFLUENCE_M)
        self.links = links(network, self.x, self.y, self.radius_m)
        return True
