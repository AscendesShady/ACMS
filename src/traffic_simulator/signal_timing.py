"""Signal timing methods: Webster, TRANSYT, Synchro, SCATS, SCOOT, actuated and max-pressure (no Qt, standard
library only).

Each follows its published method (sources in docs/architecture/sources.md). None is the commercial product: they
are built from the published descriptions, so their numbers won't match the real software exactly.

- Webster (Road Research Technical Paper 39, 1958): one junction on its own; the optimum cycle and
  greens from each phase's critical flow ratio.
- TRANSYT (Robertson, RRL Report LR 253, 1969): offline, a group of signals on one common cycle.
  Arrivals at each stop line are the upstream signals' departures, spread out on the way (Robertson's
  platoon dispersion); a queue model gives each link's delay and stops over the cycle; a hill-climb
  moves offsets and greens to lower the performance index, delay plus a weight per stop.
- Synchro (the percentile delay method of Synchro Studio): offline. Each junction's greens serve its
  90th percentile flows; the common cycle is the one with least percentile delay (control delay, the
  HCM's uniform and incremental terms, averaged over the 90th, 70th, 50th, 30th and 10th percentile
  flows); offsets are those TRANSYT's model finds best for delay alone.
- SCATS (Lowrie 1982): adaptive, from stop-line detectors. Each cycle it measures the degree of
  saturation of every lane's green; the region's cycle moves towards the length that brings its most
  saturated junction to its target, and each junction votes between four split plans.
- Actuated (HCM 2000 Ch 16 App. B; SUMO's gap-based actuated logic): a green extends while vehicles keep crossing its
  detectors within a unit extension, between a minimum and a maximum; it rests in green with no other call.
- Max-pressure (Varaiya 2013): after a minimum green, the phase with the most queued upstream less downstream goes.
- SCOOT (Hunt et al., TRRL LR 1014, 1981): adaptive, from detectors at the upstream end of each link.
  Their counts make cyclic flow profiles; TRANSYT's queue model predicts each stop line from them. The
  split optimiser moves each stage change by 4 s, the offset optimiser each junction's offset by 4 s
  a cycle, and the cycle optimiser the region's cycle every 5 minutes towards the target saturation.

Times are seconds, flows vehicles per second unless named _vph; a cyclic profile has one bin per
second of the cycle. A junction's phases run in turn, each green, then amber, then all-red; its offset
is when its first phase's green starts in the common cycle.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from math import ceil, floor, sqrt
from time import monotonic

AMBER_S, ALL_RED_S = 3.0, 2.0  # a junction's own come from its approach speed and width (clearance)
SPAN_S = AMBER_S + ALL_RED_S  # after each green
# ITE's kinematic change and clearance intervals: the driver's perception-reaction time, the deceleration
# designed for (10 ft/s²) and the vehicle length crossing (20 ft); MUTCD 2009 §4D.26 holds the yellow
# to 3-6 s and the red clearance to at most 6 s (the 1 s least is the app's).
REACTION_S, DESIGN_DECEL, CLEARING_LENGTH_M = 1.0, 3.05, 6.1
AMBER_RANGE_S, ALL_RED_RANGE_S = (3.0, 6.0), (1.0, 6.0)
# Calibrated to the simulated drivers (SUMO's default passenger car, with its startupDelay set to
# 0.9 s: mode_behavior.py) by the HCM's field method at a signal with a standing queue (4 seeds, an
# hour each, headways from the 5th queued vehicle on): saturation headway 1.744 s, i.e. 2,064 veh/h
# per lane; start-up lost time 1.82 s (the first four vehicles' excess over it); 2.37 s of the 3.3 s
# amber used, re-measured 2026-10-18 with SUMO's amber rule (stop if it can brake in time, no reaction distance:
# Simulation._may_enter; 2.77 s with an ITE reaction distance; the HCM's e is 2.0 s). Effective
# green = green + amber used − start-up lost (the HCM's extension of effective green, e).
# test_simulation.test_the_timing_methods_are_calibrated_to_the_simulated_drivers re-measures them.
SATURATION_PER_LANE_VPH = 2060.0
# Turning on a protected green, measured on the same drivers (a standing queue of one turn at a 4-phase signal, 3
# seeds of 30 min, headways from the 5th vehicle on): they slow for the curve, at low-speed turning friction with a
# floor per kind (simulation.JUNCTION_TURN_FRICTION, JUNCTION_TURN_MIN_KMH), to 0.87 and 0.91 of straight-on (HCM
# 2000 f_RT 0.85, f_LT 0.95 protected; RR67 0.87-0.91). Re-measured 2026-10-13: at open-road friction and a 15 km/h
# floor they were 1160 and 1100, 53-56 % (audit M4).
KERB_TURN_PER_LANE_VPH = 1770.0
OFFSIDE_TURN_PER_LANE_VPH = 1860.0
# A U-turn still turns at the 25 km/h floor: the offside turn's figure measured at that floor (crediting it the
# offside 1860 had a 3x2 grid, whose two-phase signals detour offside turns by U-turn, jam at X 0.65).
UTURN_PER_LANE_VPH = 1550.0
START_UP_LOST_S = 1.8
AMBER_USED_S = 2.4
MIN_GREEN_S, MAX_CYCLE_S = 7.0, 120.0
# Webster's cycle at light demand runs under 25 s (7 s greens): no shorter than this, a practical floor (FHWA
# Signal Timing Manual; Webster's own 25 s), so a pedestrian or a slow start still gets a usable green
MIN_CYCLE_S = 30.0
# TRANSYT: Robertson's platoon dispersion factor and travel time factor (TRANSYT's defaults); the
# offline optimisers' step sizes and the time they may take at Run.
DISPERSION_ALPHA, DISPERSION_BETA = 0.35, 0.8
OFFSET_STEPS_S, GREEN_STEPS_S = (16, 8, 4, 2, 1), (8, 4, 2, 1)
OPTIMISE_BUDGET_S = 3.0
# HCM incremental delay: analysis period (h), k for fixed-time control, upstream filtering I.
HCM_PERIOD_H, HCM_K, HCM_I = 0.25, 0.5, 1.0
# Synchro: the 90th, 70th, 50th, 30th and 10th percentile scenarios (standard normal z); cycles tried.
PERCENTILE_Z = (1.28, 0.52, 0.0, -0.52, -1.28)
CYCLE_STEP_S = 5
# SCATS: the unoccupied time a vehicle leaves over a stop-line loop at maximum flow (standard space
# time; SCATS calibrates it per detector, here measured on the simulated drivers as above: 0.88 s),
# the most the region's cycle changes in one cycle, a split plan's votes to be chosen.
STANDARD_SPACE_S = 0.9
SCATS_CYCLE_CHANGE_S = 6.0
SCATS_VOTES = 2  # of the last 3 cycles
SCATS_SMOOTH = 3  # cycles a junction's DS is averaged over before the region hears it
# SCOOT: its optimisers' steps, how often the cycle is reviewed, how far below target before it
# shortens, how much each cycle's counts weigh in the profile.
SCOOT_SPLIT_S, SCOOT_OFFSET_S = 4.0, 4.0
SCOOT_REVIEW_S, SCOOT_SLACK = 300.0, 0.1
SCOOT_SMOOTHING = 0.5
# Adaptive regions hold each junction's offset by lengthening or shortening a cycle by up to this.
REALIGN_S = 4.0
# Loop detectors: SCATS's at the stop line, SCOOT's near the upstream end of the link.
STOP_LINE_LOOP_M = 4.5
UPSTREAM_LOOP_AT_M, UPSTREAM_LOOP_M = 15.0, 2.0

# Actuated control (gap-out) and max-pressure: SUMO's actuated defaults, max-gap 3 s and a detector 2 s of travel before
# the stop line (sumo.dlr.de, Traffic Lights: actuated); the longest green, the app's choice.
UNIT_EXTENSION_S, DETECTOR_GAP_S, ACTUATED_MAX_GREEN_S = 3.0, 2.0, 50.0
MAX_RED_S = 2 * ACTUATED_MAX_GREEN_S  # max-pressure: a phase with traffic red this long goes next
ACTUATED = ("actuated", "max_pressure")  # each junction on its own, its greens decided step by step
COORDINATED = ("transyt", "synchro", "scats", "scoot")  # a group of junctions on one common cycle
ADAPTIVE = ("scats", "scoot")


def clearance(speed_mps: float, crossing_m: float) -> tuple[float, float]:
    """(amber, all-red) for a junction approached at `speed_mps` and `crossing_m` across, stop line to
    far side, by ITE's kinematic method on the level: amber Y = t + v / 2a, all-red R = (W + L) / v,
    each within its MUTCD range and to 0.1 s."""
    amber = REACTION_S + speed_mps / (2 * DESIGN_DECEL)
    all_red = (crossing_m + CLEARING_LENGTH_M) / speed_mps
    return (round(min(max(amber, AMBER_RANGE_S[0]), AMBER_RANGE_S[1]), 1),
            round(min(max(all_red, ALL_RED_RANGE_S[0]), ALL_RED_RANGE_S[1]), 1))


def lost_time(phases: int, span: float = SPAN_S) -> float:
    """Webster's lost time: every phase's start-up lost time, all-red and the amber not used (`span`:
    its amber plus all-red; the HCM's tL = l1 + Y − e)."""
    return phases * (START_UP_LOST_S + span - AMBER_USED_S)


def effective(green: float) -> float:
    return green + AMBER_USED_S - START_UP_LOST_S


def webster(flow_ratios: list[float], span: float = SPAN_S) -> tuple[float, list[float]]:
    """(cycle, green per phase) by Webster's method (Road Research Technical Paper 39, 1958) from
    each phase's critical flow ratio y = q/s:

        C0 = (1.5·L + 5) / (1 − Y),  Y = Σy,  L = Σ(start-up lost time + all-red)
        effective green g = (C0 − L)·y / Y,  shown green G = g − amber used + start-up lost time

    C0 is capped at MAX_CYCLE_S, and used at the cap when Y ≥ 0.95 (the junction is at or over
    capacity); the greens fill it (fit_greens), each at least MIN_GREEN_S. With no demand the greens
    split evenly. Minimum greens and clearances too long for the cap are refused."""
    count = len(flow_ratios)
    if count * (MIN_GREEN_S + span) > MAX_CYCLE_S:
        raise ValueError(f"{count} phases of {MIN_GREEN_S:g} s minimum green and {span:g} s amber and all-red "
                         f"need {count * (MIN_GREEN_S + span):g} s, more than the {MAX_CYCLE_S:g} s longest cycle.")
    total = sum(flow_ratios)
    lost = lost_time(count, span)
    cycle = MAX_CYCLE_S if total >= 0.95 else min(MAX_CYCLE_S, max(MIN_CYCLE_S, (1.5 * lost + 5) / (1 - total)))
    greens = fit_greens(flow_ratios, cycle, span)
    return sum(green + span for green in greens), greens


def critical_saturation(flow_ratios: list[float], span: float = SPAN_S) -> float:
    """A junction's critical degree of saturation (flow over capacity, HCM's critical v/c) when
    Webster times it: X = Y·C / (C − L) with Webster's cycle C. It rises with the demand."""
    cycle, _ = webster(flow_ratios, span)
    return sum(flow_ratios) * cycle / (cycle - lost_time(len(flow_ratios), span))


def fit_greens(shares: list[float], cycle: float, span: float = SPAN_S) -> list[float]:
    """Shown greens that, with their ambers and all-reds (`span` each), fill exactly `cycle`: the effective
    green shared in proportion to `shares` (flow ratios: Webster's equisaturation), none below MIN_GREEN_S.
    A cycle too short for every minimum is lengthened to fit. No shares: even."""
    count = len(shares)
    least = effective(MIN_GREEN_S)
    room = max(cycle - lost_time(count, span), count * least)  # effective green to share
    weights = list(shares) if sum(shares) > 0 else [1.0] * count
    greens, free = [0.0] * count, list(range(count))
    while True:  # a phase whose share falls short of the minimum gets the minimum; the rest share the rest
        weight = sum(weights[i] for i in free)
        short = [i for i in free if weight <= 0 or room * weights[i] / weight < least]
        if not short or len(short) == len(free):
            break
        for i in short:
            greens[i], room = least, room - least
            free.remove(i)
    weight = sum(weights[i] for i in free)
    for i in free:
        greens[i] = room * weights[i] / weight if weight > 0 else room / len(free)
    return [green - AMBER_USED_S + START_UP_LOST_S for green in greens]


def phase_starts(greens: list[float], offset: float, span: float = SPAN_S) -> list[float]:
    """When each phase's green starts, from the first's at `offset`."""
    starts, t = [], offset
    for green in greens:
        starts.append(t)
        t += green + span
    return starts


def green_mask(cycle: int, start: float, green: float) -> list[bool]:
    """The seconds of the cycle a phase discharges: its effective green, from START_UP_LOST_S after
    its green starts to AMBER_USED_S into its amber."""
    mask = [False] * cycle
    for t in range(round(start + START_UP_LOST_S), round(start + green + AMBER_USED_S)):
        mask[t % cycle] = True
    return mask


def roll(profile: list[float], by: int) -> list[float]:
    """The profile `by` seconds later round the cycle."""
    cycle = len(profile)
    by %= cycle
    return profile[-by:] + profile[:-by] if by else list(profile)


def resample(profile: list[float], bins: int) -> list[float]:
    """A profile of rates over a cycle of another length, `bins` seconds long."""
    count = len(profile)
    if count == bins or not count:
        return list(profile) if count else [0.0] * bins
    return [profile[min(count - 1, int(i * count / bins))] for i in range(bins)]


def disperse(profile: list[float], travel_s: float) -> list[float]:
    """A platoon's flow profile after `travel_s` of cruise, as Robertson's dispersion (TRANSYT)
    predicts it: q'(i + t) = F·q(i) + (1 − F)·q'(i + t − 1), with t = β × the travel time and
    F = 1 / (1 + α·t). Round the cycle twice, so it starts as it ends; the total is kept."""
    cycle = len(profile)
    lag = max(1, round(DISPERSION_BETA * travel_s))
    f = 1 / (1 + DISPERSION_ALPHA * lag)
    spread, previous = [0.0] * cycle, sum(profile) / cycle
    for _ in range(2):
        for i in range(cycle):
            previous = f * profile[i] + (1 - f) * previous
            spread[(i + lag) % cycle] = previous
    return spread


def incremental_delay(saturation: float, capacity_vph: float) -> float:
    """The HCM's incremental (random and oversaturation) delay per vehicle, s:
    d2 = 900·T·[(X − 1) + √((X − 1)² + 8·k·I·X / (c·T))]."""
    if saturation <= 0 or capacity_vph <= 0:
        return 0.0
    x, t = saturation, HCM_PERIOD_H
    return 900 * t * ((x - 1) + sqrt((x - 1) ** 2 + 8 * HCM_K * HCM_I * x / (capacity_vph * t)))


def hcm_delay(flow_vph: float, saturation_vph: float, effective_s: float, cycle: float) -> float:
    """The HCM's control delay per vehicle at a signal, s: the uniform delay
    d1 = 0.5·C·(1 − g/C)² / (1 − min(1, X)·g/C) plus the incremental delay d2."""
    if flow_vph <= 0:
        return 0.0
    share = min(effective_s / cycle, 1.0)
    capacity = saturation_vph * share
    x = flow_vph / capacity if capacity > 0 else float("inf")
    uniform = 0.5 * cycle * (1 - share) ** 2 / max(1 - min(1.0, x) * share, 1e-9)
    return uniform + incremental_delay(x, capacity)


@dataclass
class Queue:
    """One stop line over one cycle."""
    departures: list  # veh leaving in each second of the cycle
    delay: float  # veh·s per cycle
    stops: float  # vehicles stopped per cycle
    saturation: float  # degree of saturation X: arrivals over what the green can serve


def cyclic_queue(arrivals: list[float], green: list[bool], capacity: float) -> Queue:
    """A stop line's queue over one cycle (TRANSYT's cyclic flow profile model): each second's
    arrivals join the queue, which discharges at the saturation flow `capacity` while the effective
    green lasts. It runs round the cycle twice, so the cycle starts with the queue it ends with. The
    delay is the queue waiting each second; a vehicle arriving at a red or behind a queue stops.
    Over capacity, the deterministic part runs at capacity (as the HCM's uniform delay caps X at 1)
    and the HCM's incremental term adds the random and oversaturation delay for everyone."""
    cycle, total = len(arrivals), sum(arrivals)
    served = capacity * sum(green)
    if served <= 0:
        return Queue([0.0] * cycle, total * cycle, total, float("inf") if total else 0.0)
    x = total / served
    scale = 1 / x if x > 1 else 1.0
    queue = delay = stops = 0.0
    departures = [0.0] * cycle
    for last in (False, True):
        for t in range(cycle):
            arriving = arrivals[t] * scale
            if last and (queue > 1e-9 or not green[t]):
                stops += arriving
            queue += arriving
            leaving = min(queue, capacity) if green[t] else 0.0
            queue -= leaving
            if last:
                departures[t] = leaving
                delay += queue
    if total <= 0:
        return Queue(departures, 0.0, 0.0, 0.0)
    modelled = total * scale
    per_vehicle = delay / modelled + incremental_delay(x, served / cycle * 3600)
    return Queue(departures, per_vehicle * total, stops / modelled * total, x)


# --- TRANSYT and Synchro: offline, a group on one common cycle ------------------------------------

@dataclass
class Approach:
    """A link arriving at a signal of a coordinated group, as the offline timing model sees it."""
    id: str
    node: str
    phase: int  # the phase serving it there
    capacity: float  # veh/s at saturation flow, all its lanes
    flow: float  # veh/s arriving, from the routed demand
    travel_s: float  # cruise time from the upstream stop line to this one
    feeds: list = field(default_factory=list)  # (upstream approach, share of its departures coming here)
    uniform: float = 0.0  # veh/s arriving evenly: traffic from outside the group
    stop_weight_s: float = 0.0  # the delay one stop counts as in the performance index


class TimingModel:
    """TRANSYT's traffic model of a coordinated group on a common cycle: each approach's arrivals are
    its upstream approaches' departures, dispersed on the way (plus traffic from outside the group,
    evenly); each stop line is a cyclic queue. The performance index (PI) is delay plus the stop
    weight per stop, per hour."""

    def __init__(self, approaches: list[Approach], greens: dict[str, list[float]], cycle: int,
                 spans: dict[str, float] | None = None) -> None:
        self.cycle = int(cycle)
        self.spans = spans or {}  # node -> its amber plus all-red, if not SPAN_S
        self.approaches = {approach.id: approach for approach in approaches}
        self.at: dict[str, list[Approach]] = defaultdict(list)  # node -> its approaches
        self.feeding: dict[str, list[Approach]] = defaultdict(list)  # approach id -> the approaches it feeds
        for approach in approaches:
            self.at[approach.node].append(approach)
            for upstream, _share in approach.feeds:
                self.feeding[upstream].append(approach)
        self.greens = {node: list(value) for node, value in greens.items()}
        self.offsets = {node: 0.0 for node in greens}
        self.queues: dict[str, Queue] = {}

    def mask(self, approach: Approach, greens=None, offset=None) -> list[bool]:
        greens = self.greens[approach.node] if greens is None else greens
        offset = self.offsets[approach.node] if offset is None else offset
        start = phase_starts(greens, offset, self.spans.get(approach.node, SPAN_S))[approach.phase]
        return green_mask(self.cycle, start, greens[approach.phase])

    def arrivals(self, approach: Approach, departures: dict | None = None) -> list[float]:
        profile = [approach.uniform] * self.cycle
        for upstream, share in approach.feeds:
            if departures and upstream in departures:
                leaving = departures[upstream]
            elif upstream in self.queues:
                leaving = self.queues[upstream].departures
            else:
                leaving = [self.approaches[upstream].flow] * self.cycle  # not solved yet: evenly
            for i, value in enumerate(disperse([rate * share for rate in leaving], approach.travel_s)):
                profile[i] += value
        return profile

    def queue(self, approach: Approach, departures=None, greens=None, offset=None) -> Queue:
        return cyclic_queue(self.arrivals(approach, departures), self.mask(approach, greens, offset),
                            approach.capacity)

    def solve(self, passes: int = 3) -> None:
        """Every stop line, in a few passes, so each one's arrivals come from its upstream ones'."""
        for _ in range(passes):
            for approach in self.approaches.values():
                self.queues[approach.id] = self.queue(approach)

    def cost(self, approach: Approach, queue: Queue) -> float:
        return (queue.delay + approach.stop_weight_s * queue.stops) * 3600 / self.cycle

    def pi(self) -> float:
        return sum(self.cost(self.approaches[key], queue) for key, queue in self.queues.items())

    def _touched(self, node: str) -> list[Approach]:
        """The approaches a node's timing changes: its own, and the ones they feed."""
        own = self.at[node]
        seen, touched = {approach.id for approach in own}, list(own)
        for approach in own:
            for fed in self.feeding[approach.id]:
                if fed.id not in seen:
                    seen.add(fed.id)
                    touched.append(fed)
        return touched

    def local(self, node: str) -> float:
        return sum(self.cost(approach, self.queues[approach.id]) for approach in self._touched(node))

    def trial(self, node: str, greens: list[float], offset: float) -> tuple[float, dict]:
        """The PI of the approaches the node's timing touches, were it timed so, and their queues."""
        own = self.at[node]
        new = {approach.id: self.queue(approach, greens=greens, offset=offset) for approach in own}
        departures = {key: queue.departures for key, queue in new.items()}
        for approach in self._touched(node)[len(own):]:
            new[approach.id] = self.queue(approach, departures)
        return sum(self.cost(self.approaches[key], queue) for key, queue in new.items()), new

    def accept(self, node: str, greens: list[float], offset: float, queues: dict) -> None:
        self.greens[node], self.offsets[node] = list(greens), offset % self.cycle
        self.queues.update(queues)

    def progression(self) -> None:
        """Offsets that let the heaviest flows through without stopping, a starting point: from the
        busiest junction, along the heaviest movement to each junction not yet placed, its phase for
        that movement turns green as the platoon from upstream arrives."""
        weight: dict[str, float] = defaultdict(float)
        edges = []  # (flow, upstream approach, the approach it feeds)
        for approach in self.approaches.values():
            weight[approach.node] += approach.flow
            for upstream, share in approach.feeds:
                edges.append((self.approaches[upstream].flow * share, self.approaches[upstream], approach))
        nodes = [node for node in self.greens]
        if not nodes:
            return
        placed = {max(nodes, key=lambda node: weight[node])}
        self.offsets = {node: 0.0 for node in nodes}
        edges.sort(key=lambda edge: -edge[0])
        while True:
            edge = next((edge for edge in edges if (edge[1].node in placed) != (edge[2].node in placed)), None)
            if edge is None:
                break
            _flow, upstream, downstream = edge
            up_start = phase_starts(self.greens[upstream.node], 0.0, self.spans.get(upstream.node, SPAN_S))[upstream.phase]
            down_start = phase_starts(self.greens[downstream.node], 0.0,
                                      self.spans.get(downstream.node, SPAN_S))[downstream.phase]
            if upstream.node in placed:  # downstream's green starts as the platoon arrives
                self.offsets[downstream.node] = (self.offsets[upstream.node] + up_start + downstream.travel_s
                                                 - down_start) % self.cycle
                placed.add(downstream.node)
            else:
                self.offsets[upstream.node] = (self.offsets[downstream.node] + down_start - downstream.travel_s
                                               - up_start) % self.cycle
                placed.add(upstream.node)
        self.offsets = {node: float(round(offset)) for node, offset in self.offsets.items()}


def hill_climb(model: TimingModel, greens: bool = True, budget_s: float = OPTIMISE_BUDGET_S) -> bool:
    """TRANSYT's optimiser: for each step size in turn, try moving each junction's offset, and
    (with `greens`) green between neighbouring phases, by that step either way, keeping every change
    that lowers the PI, until a round changes nothing. It stops early, keeping the best so far, once
    `budget_s` of real time has gone; returns False then."""
    deadline = monotonic() + budget_s
    model.solve()
    offset_steps = sorted({min(step, model.cycle // 2) for step in OFFSET_STEPS_S}, reverse=True)
    for step in offset_steps:
        for _ in range(6):
            better = False
            for node in model.greens:
                if monotonic() > deadline:
                    model.solve(2)
                    return False
                base = model.local(node)
                current, offset = model.greens[node], model.offsets[node]
                options = [(current, offset + step), (current, offset - step)]
                count = len(current)
                if greens and step in GREEN_STEPS_S:
                    for i in range(count if count > 2 else 1):
                        j = (i + 1) % count
                        for moved in (step, -step):
                            trial = list(current)
                            trial[i], trial[j] = trial[i] + moved, trial[j] - moved
                            if min(trial[i], trial[j]) >= MIN_GREEN_S:
                                options.append((trial, offset))
                for trial_greens, trial_offset in options:
                    cost, queues = model.trial(node, trial_greens, trial_offset)
                    if cost < base - 1e-6:
                        model.accept(node, trial_greens, trial_offset, queues)
                        base, current, offset, better = cost, model.greens[node], model.offsets[node], True
            if not better:
                break
        model.solve(2)
    return True


def common_cycle(ratios: dict[str, list[float]], low: float, high: float, spans: dict[str, float] | None = None) -> int:
    """A coordinated group's cycle: its critical junction's (the longest of their Webster cycles),
    held within [low, high], and long enough for every junction's minimum greens."""
    spans = spans or {}
    need = max(len(value) * (MIN_GREEN_S + spans.get(node, SPAN_S)) for node, value in ratios.items())
    cycle = max(webster(value, spans.get(node, SPAN_S))[0] for node, value in ratios.items())
    return int(ceil(max(min(max(cycle, low), high), need)))


def transyt(model: TimingModel, budget_s: float = OPTIMISE_BUDGET_S) -> dict:
    """TRANSYT: from equisaturation greens and progression offsets, the hill-climb on offsets and
    greens. Returns the PI before and after and whether it finished within the budget."""
    model.progression()
    model.solve()
    before = model.pi()
    finished = hill_climb(model, greens=True, budget_s=budget_s)
    return {"pi_before": before, "pi_after": model.pi(), "finished": finished}


def percentile_delay(phases: list[tuple[float, float]], greens: list[float], cycle: float) -> float:
    """Synchro's percentile delay for one junction, veh·s per hour: its control delay (the HCM's
    uniform plus incremental delay) averaged over the 90th, 70th, 50th, 30th and 10th percentile
    flows. Each phase is (flow, saturation flow), veh/h, of its critical lane group; a flow's
    percentile is its arrivals per cycle λ + z·√λ."""
    total = 0.0
    for z in PERCENTILE_Z:
        for (flow, saturation), green in zip(phases, greens):
            arrivals = flow * cycle / 3600
            scenario = max(0.0, arrivals + z * sqrt(arrivals)) * 3600 / cycle
            total += scenario * hcm_delay(scenario, saturation, effective(green), cycle)
    return total / len(PERCENTILE_Z)


def percentile_greens(phases: list[tuple[float, float]], cycle: float, span: float = SPAN_S) -> list[float]:
    """Synchro's splits: greens in proportion to each phase's 90th percentile flow ratio."""
    ratios = []
    for flow, saturation in phases:
        arrivals = flow * cycle / 3600
        ratios.append((arrivals + PERCENTILE_Z[0] * sqrt(arrivals)) * 3600 / cycle / saturation)
    return fit_greens(ratios, cycle, span)


def synchro_cycle(junctions: dict[str, list[tuple[float, float]]], low: float, high: float,
                  spans: dict[str, float] | None = None) -> int:
    """Synchro's common cycle: of the cycles from low to high in 5 s steps, the one with the least
    total percentile delay over the group, each junction on its percentile greens."""
    spans = spans or {}
    need = max(len(phases) * (MIN_GREEN_S + spans.get(node, SPAN_S)) for node, phases in junctions.items())
    first, last = int(ceil(max(low, need))), int(floor(max(high, need)))
    cycles = list(range(first, last + 1, CYCLE_STEP_S)) + ([last] if (last - first) % CYCLE_STEP_S else [])
    return min(cycles, key=lambda cycle: sum(
        percentile_delay(phases, percentile_greens(phases, cycle, spans.get(node, SPAN_S)), cycle)
        for node, phases in junctions.items()))


def synchro(model: TimingModel, budget_s: float = OPTIMISE_BUDGET_S) -> dict:
    """Synchro's offsets: from progression, the hill-climb on offsets alone (its greens are the
    percentile greens), for delay alone (its approaches carry no stop weight)."""
    model.progression()
    model.solve()
    before = model.pi()
    finished = hill_climb(model, greens=False, budget_s=budget_s)
    return {"pi_before": before, "pi_after": model.pi(), "finished": finished}


# --- SCATS and SCOOT: adaptive, a region on one common cycle ---------------------------------------

def realigned_length(now: float, reference: float, cycle: float, offset: float, most: float = REALIGN_S) -> float:
    """How long a junction's cycle starting at `now` should last so the next one starts on time
    (at reference + offset + k·cycle), changed from `cycle` by at most `most` seconds."""
    late = (now - reference - offset) % cycle
    if late > cycle / 2:
        late -= cycle  # early
    return cycle - min(max(late, -most), most)


class _Region:
    """A region of adaptive junctions on a common cycle, each keeping its offset from `reference`."""

    def __init__(self, cycle: float, low: float, high: float) -> None:
        self.cycle, self.low, self.high = float(cycle), float(low), float(high)
        self.reference = 0.0  # a time a cycle is due to start at offset 0

    def set_cycle(self, cycle: float, t: float) -> None:
        cycle = min(max(cycle, self.low), self.high)
        if cycle != self.cycle:  # from the last cycle start due by now, the new length
            self.reference += floor((t - self.reference) / self.cycle) * self.cycle
            self.cycle = cycle


def degree_of_saturation(green_s: float, unoccupied_s: float, spaces: int, space_s: float = STANDARD_SPACE_S) -> float:
    """SCATS's degree of saturation of a lane's green: DS = (g − (T − t·h)) / g, the share of the
    green g used by traffic, from the time T its stop-line loop was unoccupied during the green and
    the h spaces between the vehicles that crossed it, each allowed t at maximum flow."""
    if green_s <= 0:
        return 0.0
    return max(0.0, (green_s - (unoccupied_s - space_s * spaces)) / green_s)


class ScatsRegion(_Region):
    """SCATS's strategic control of a region of linked ('married') junctions: once a cycle the cycle
    moves towards the length that would bring its most saturated junction to its target degree of
    saturation, by at most SCATS_CYCLE_CHANGE_S."""

    def __init__(self, cycle: float, low: float, high: float) -> None:
        super().__init__(cycle, low, high)
        self.saturation: dict[str, float] = {}  # junction -> its latest DS over its target
        self.decided: float | None = None

    def report(self, node: str, saturation: float, t: float) -> None:
        self.saturation[node] = saturation
        if self.decided is not None and t - self.decided < 0.9 * self.cycle:
            return  # decided already this cycle
        self.decided = t
        worst = max(self.saturation.values())
        change = min(max(self.cycle * (worst - 1), -SCATS_CYCLE_CHANGE_S), SCATS_CYCLE_CHANGE_S)
        self.set_cycle(self.cycle + change, t)


class ScatsJunction:
    """SCATS at one junction. Its stop-line loops measure each lane's degree of saturation during its
    green; each cycle it reports its most saturated phase (over its target) to the region and votes for the split
    plan, of its four, that would even out its phases' saturation best; a plan is used once it has
    won 2 of the last 3 votes. Its cycles keep its offset in the region. A phase's DS is its lanes' DS weighted by
    the vehicles each passed, and the region hears its mean over the last SCATS_SMOOTH cycles: the worst single
    lane of one cycle is biased up and noisy, and took a corridor's cycle to its 120 s cap at DS 0.6-0.7 (audit
    2026-10-13, M1)."""

    def __init__(self, node: str, region: ScatsRegion, plans: list[list[float]], offset: float, target: float,
                 report=None, span: float = SPAN_S) -> None:
        self.node, self.region, self.plans, self.offset, self.target = node, region, plans, offset, target
        self.span = span  # its amber plus all-red
        self.report = report or (lambda node, plan: None)
        self.plan, self.votes = 0, deque(maxlen=3)
        self.lanes: dict[object, list] = {}  # lane -> [phase, green s, unoccupied s, vehicles]
        self.saturation = [0.0] * len(plans[0])
        self.reported: deque = deque(maxlen=SCATS_SMOOTH)  # its latest cycles' worst phase DS over target

    def add_lane(self, lane, phase: int) -> None:
        self.lanes[lane] = [phase, 0.0, 0.0, 0]

    def measure(self, lane, green: bool, occupied: bool, passed: int, dt: float) -> None:
        if green:
            record = self.lanes[lane]
            record[1] += dt
            record[2] += 0.0 if occupied else dt
            record[3] += passed

    def phase_starts(self, signal, index: int, t: float) -> None:
        pass  # SCATS's strategic decisions are made once a cycle

    def cycle_starts(self, signal, t: float) -> None:
        weighted, passed = [0.0] * len(self.saturation), [0] * len(self.saturation)
        for record in self.lanes.values():
            phase, green, unoccupied, vehicles = record
            if green > 0 and vehicles:
                weighted[phase] += degree_of_saturation(green, unoccupied, vehicles) * vehicles
                passed[phase] += vehicles
            record[1:] = [0.0, 0.0, 0]
        saturation = [w / n if n else 0.0 for w, n in zip(weighted, passed)]
        self.saturation = saturation
        self.reported.append(max(saturation) / self.target)
        self.region.report(self.node, sum(self.reported) / len(self.reported), t)
        cycle = realigned_length(t, self.region.reference, self.region.cycle, self.offset)
        current = [green for _, green in signal.phases]
        options = [fit_greens(plan, cycle, self.span) for plan in self.plans]

        def predicted(greens: list[float]) -> float:  # the worst phase's DS on these greens
            return max(ds * effective(now) / effective(new) for ds, now, new in zip(saturation, current, greens))

        self.votes.append(min(range(len(options)), key=lambda i: (predicted(options[i]), i)))
        winner = max(set(self.votes), key=self.votes.count)
        if self.votes.count(winner) >= SCATS_VOTES:
            self.plan = winner
        greens = options[self.plan]
        signal.set_greens(greens)
        self.report(self.node, {"method": "scats", "cycle_s": round(self.region.cycle, 1),
                                "greens_s": [round(green, 1) for green in greens], "offset_s": round(self.offset, 1),
                                "split_plan": self.plan + 1, "saturation": [round(ds, 2) for ds in saturation]})


@dataclass
class ScootLink:
    """A link arriving at a SCOOT junction, with its detector near the upstream end."""
    id: str
    phase: int
    capacity: float  # veh/s at saturation flow
    lag_s: float  # cruise time from its detector to the stop line
    source: str | None = None  # the SCOOT junction its traffic comes from, for the offset optimiser
    stop_weight_s: float = 0.0
    counts: list = field(default_factory=list)  # this cycle: vehicles in each second
    profile: list | None = None  # its cyclic flow profile at the detector, veh/s, smoothed over cycles


class ScootRegion(_Region):
    """SCOOT's cycle optimiser for a region: every SCOOT_REVIEW_S the cycle lengthens (4 s below
    64 s, 8 s above) while its most saturated junction is over its target, and shortens once every
    junction is well under it."""

    def __init__(self, cycle: float, low: float, high: float) -> None:
        super().__init__(cycle, low, high)
        self.junctions: dict[str, ScootJunction] = {}
        self.reviewed = 0.0

    def review(self, t: float) -> None:
        if t - self.reviewed < SCOOT_REVIEW_S:
            return
        self.reviewed = t
        worst = max((junction.worst() for junction in self.junctions.values()), default=0.0)
        step = 4.0 if self.cycle < 64 else 8.0
        if worst > 1:
            self.set_cycle(self.cycle + step, t)
        elif worst < 1 - SCOOT_SLACK:
            self.set_cycle(self.cycle - step, t)


class ScootJunction:
    """SCOOT at one junction. Its upstream detectors' counts make each link's cyclic flow profile;
    delayed by the cruise to the stop line and dispersed (TRANSYT's model), they predict the queues.
    As each phase starts, the split optimiser moves its green's end 4 s either way if that lowers the
    most saturated phase's degree of saturation (the stored split follows by 1 s). Once a cycle the
    offset optimiser moves the offset 4 s either way if that lowers the delay and stops on its own
    links and on the links it feeds at its neighbours."""

    def __init__(self, node: str, region: ScootRegion, shares: list[float], offset: float, target: float,
                 links: list[ScootLink], report=None, span: float = SPAN_S) -> None:
        self.node, self.region, self.shares, self.offset, self.target = node, region, list(shares), offset, target
        self.span = span  # its amber plus all-red
        self.links = {link.id: link for link in links}
        self.report = report or (lambda node, plan: None)
        self.start = 0.0  # when its current cycle began
        self.saturation = [0.0] * len(shares)
        region.junctions[node] = self

    def count(self, link: str, t: float, vehicles: int) -> None:
        if vehicles:
            counts, second = self.links[link].counts, max(0, int(t - self.start))
            counts.extend([0] * (second + 1 - len(counts)))
            counts[second] += vehicles

    def arrivals(self, link: ScootLink, cycle: int, shift: int = 0) -> list[float]:
        """Its predicted arrivals at the stop line, veh in each second of the cycle."""
        profile = resample(link.profile, cycle)
        return roll(disperse(profile, link.lag_s), shift)

    def nominal(self) -> list[float]:
        return fit_greens(self.shares, self.region.cycle, self.span)

    def saturations(self, greens: list[float]) -> list[float]:
        """Each phase's degree of saturation on these greens: its most saturated link's arrivals per
        cycle over what its effective green can serve."""
        cycle = round(sum(greens) + len(greens) * self.span)
        saturation = [0.0] * len(greens)
        for link in self.links.values():
            if link.profile:
                arriving = sum(resample(link.profile, cycle))
                served = link.capacity * effective(greens[link.phase])
                saturation[link.phase] = max(saturation[link.phase], arriving / served if served > 0 else 0.0)
        return saturation

    def worst(self) -> float:
        return max(self.saturation) / self.target

    def phase_starts(self, signal, index: int, t: float) -> None:
        if not any(link.profile for link in self.links.values()):
            return  # no counts yet
        greens, count = [green for _, green in signal.phases], len(signal.phases)
        best, score = 0.0, max(self.saturations(greens))
        for delta in (-SCOOT_SPLIT_S, SCOOT_SPLIT_S):
            trial = list(greens)
            trial[index] += delta
            if index + 1 < count:
                trial[index + 1] -= delta
            if min(trial) >= MIN_GREEN_S and max(self.saturations(trial)) < score - 1e-9:
                best, score = delta, max(self.saturations(trial))
        if best:
            greens[index] += best
            if index + 1 < count:
                greens[index + 1] -= best
            signal.set_greens(greens)
            room = max(self.region.cycle - lost_time(count, self.span), 1.0)  # the stored split moves by 1 s
            total = sum(self.shares) or 1.0
            shares = [share / total for share in self.shares]
            moved = (1.0 if best > 0 else -1.0) / room
            shares[index] = max(0.0, shares[index] + moved)
            if index + 1 < count:
                shares[index + 1] = max(0.0, shares[index + 1] - moved)
            self.shares = shares

    def _cost(self, link: ScootLink, arrivals: list[float], greens: list[float]) -> float:
        cycle = len(arrivals)
        start = phase_starts(greens, 0.0, self.span)[link.phase]
        queue = cyclic_queue(arrivals, green_mask(cycle, start, greens[link.phase]), link.capacity)
        return queue.delay + link.stop_weight_s * queue.stops

    def _offset_cost(self, delta: int) -> float:
        """Delay and stops on its links and on those it feeds at its neighbours, were its timing
        `delta` seconds later."""
        cycle, total = round(self.region.cycle), 0.0
        greens = self.nominal()
        for link in self.links.values():
            if link.profile:  # its own greens later: the same arrivals come `delta` earlier
                total += self._cost(link, self.arrivals(link, cycle, -delta), greens)
        for neighbour in self.region.junctions.values():
            fed = [link for link in neighbour.links.values() if link.source == self.node and link.profile]
            if fed:
                theirs = neighbour.nominal()
                total += sum(neighbour._cost(link, neighbour.arrivals(link, cycle, delta), theirs) for link in fed)
        return total

    def cycle_starts(self, signal, t: float) -> None:
        for link in self.links.values():  # the cycle's counts join the profile
            if self.start < t:
                measured = [float(n) for n in link.counts] + [0.0] * max(0, round(t - self.start) - len(link.counts))
                if link.profile is None:
                    link.profile = measured
                else:
                    old = resample(link.profile, len(measured))
                    link.profile = [SCOOT_SMOOTHING * new + (1 - SCOOT_SMOOTHING) * was for new, was in zip(measured, old)]
            link.counts = []
        self.start = t
        if any(link.profile for link in self.links.values()):
            self.saturation = self.saturations(self.nominal())
            step = int(SCOOT_OFFSET_S)
            costs = {delta: self._offset_cost(delta) for delta in (0, -step, step)}
            best = min(costs, key=lambda delta: (costs[delta], abs(delta)))
            self.offset = (self.offset + best) % self.region.cycle
        self.region.review(t)
        greens = fit_greens(self.shares, realigned_length(t, self.region.reference, self.region.cycle, self.offset),
                            self.span)
        signal.set_greens(greens)
        self.report(self.node, {"method": "scoot", "cycle_s": round(self.region.cycle, 1),
                                "greens_s": [round(green, 1) for green in greens], "offset_s": round(self.offset, 1),
                                "saturation": [round(ds, 2) for ds in self.saturation]})


class ActuatedJunction:
    """Gap-out actuated control at one junction (HCM 2000 Ch 16 App. B; SUMO's MSActuatedTrafficLightLogic,
    references/reverse_engineering/simulation/traffic_lights.md). A detector on each lane DETECTOR_GAP_S of travel upstream of
    the stop line places a call for its phase. A green runs at least MIN_GREEN_S, then on while vehicles keep
    crossing its detectors within UNIT_EXTENSION_S of each other, up to ACTUATED_MAX_GREEN_S; it rests in green while
    no other phase has a call. The next phase is the next one in turn with a call (none: the next in turn)."""

    def __init__(self, node: str, phases: int, report=None) -> None:
        self.node = node
        self.last_seen = [-1e9] * phases  # when a vehicle last crossed a detector of each phase
        self.call = [False] * phases  # a vehicle waiting (or coming) for a phase that isn't green
        self.report = report or (lambda node, plan: None)
        self.used: list[float] = [0.0] * phases  # the greens of the cycle under way

    def detect(self, phase: int, t: float, signal) -> None:
        """A vehicle over (or past) one of this phase's detectors at time t."""
        self.last_seen[phase] = t
        index, into = signal._position(t)
        if index != phase or into >= signal.phases[index][1]:
            self.call[phase] = True

    def phase_starts(self, signal, index: int, t: float) -> None:
        self.call[index] = False
        signal.set_green(index, MIN_GREEN_S)

    def cycle_starts(self, signal, t: float) -> None:
        self.report(self.node, {"greens_s": [round(green, 1) for _, green in signal.phases]})

    def next_phase(self, signal, finished: int, t: float) -> int:
        count = len(signal.phases)
        for step in range(1, count + 1):
            candidate = (finished + step) % count
            if self.call[candidate] and candidate != finished:
                return candidate
        return (finished + 1) % count

    def check(self, signal, t: float) -> None:
        """Each step: extend the green, rest in it, or end it (gap out, or max out against a call)."""
        index, into = signal._position(t)
        green = signal.phases[index][1]
        if into >= green:
            return  # amber or all-red
        called = any(self.call[p] for p in range(len(self.call)) if p != index)
        if not called:
            signal.set_green(index, max(green, into + 1.0))  # nobody else waiting: rest in green
        elif into >= MIN_GREEN_S and (t - self.last_seen[index] >= UNIT_EXTENSION_S or into >= ACTUATED_MAX_GREEN_S):
            signal.set_green(index, into)  # gap out, or max out: amber now
        else:
            signal.set_green(index, min(max(green, into + 1.0, MIN_GREEN_S), ACTUATED_MAX_GREEN_S))


class MaxPressureJunction:
    """Max-pressure control at one junction (Varaiya 2013, "Max pressure control of a network of signalized
    intersections", Transportation Research Part C 36; Wei et al. 2019's PressLight form): each phase's pressure is,
    over its approaches, the vehicles on them less the mean on the links they feed (`pressure(links)`, from the
    simulation: Varaiya's link occupancy x, moving or queued). Once a green has run MIN_GREEN_S it ends as soon as another phase's pressure is higher, and that
    phase goes next (not the next in turn); it ends at ACTUATED_MAX_GREEN_S at most. A phase with traffic that has
    been red MAX_RED_S goes next whatever the pressures (a maximum red: Varaiya's stability bounds the queues, not
    one vehicle's wait; two busy arms passed the green between them while a light one waited 300 s)."""

    def __init__(self, node: str, groups: list, pressure, report=None) -> None:
        self.node, self.groups, self.pressure = node, groups, pressure
        self.chosen: int | None = None
        self.report = report or (lambda node, plan: None)
        self.red_since = {phase: 0.0 for phase in range(len(groups))}  # when each phase's last green ended

    def phase_starts(self, signal, index: int, t: float) -> None:
        self.chosen = None
        self.red_since.pop(index, None)  # green now
        signal.set_green(index, MIN_GREEN_S)

    def cycle_starts(self, signal, t: float) -> None:
        self.report(self.node, {"greens_s": [round(green, 1) for _, green in signal.phases]})

    def next_phase(self, signal, finished: int, t: float) -> int:
        self.red_since[finished] = t
        return self.chosen if self.chosen is not None else (finished + 1) % len(signal.phases)

    def check(self, signal, t: float) -> None:
        index, into = signal._position(t)
        green = signal.phases[index][1]
        if into >= green:
            return
        if into < MIN_GREEN_S:  # within its minimum: kept a step ahead, so the decision comes before its amber
            signal.set_green(index, max(green, into + 1.0))
            return
        pressures = [self.pressure(group) for group in self.groups]
        best = max(range(len(pressures)), key=lambda p: (pressures[p], p == index))
        starved = [p for p, since in self.red_since.items() if p != index and pressures[p] > 0 and t - since >= MAX_RED_S]
        if starved:  # the one red longest goes next
            self.chosen = min(starved, key=lambda p: self.red_since[p])
            signal.set_green(index, into)
            return
        if (best != index and pressures[best] > pressures[index]) or into >= ACTUATED_MAX_GREEN_S:
            self.chosen = best if best != index else (index + 1) % len(self.groups)
            signal.set_green(index, into)
        else:
            signal.set_green(index, max(green, into + 1.0))
