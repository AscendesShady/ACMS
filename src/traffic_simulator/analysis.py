"""What a headless run's observations say about junctions, signals, the run's health, a disruption and fuel (no Qt).

Each function reads a run's KPIs with what it observed (`kpis["observed"]`: results.RunRecorder.observed() and
Simulation.observations(), as MainWindow._observed_kpis joins them), so the assistant's tools can ask any run of
the session:
- `junction_los`: control delay per approach and junction, graded A–F by the HCM 2000 (Exhibit 16-2 for signals,
  17-2 / 17-22 for give-way and stop control), per 15-minute period too; a roundabout entry's capacity, simulated
  and by the HCM 6 (roundabouts.hcm_entry_capacity), against the flow circulating in front of it.
- `diagnose_signal`: each signal's critical v/c, flow ratios, arrivals on green (arrival type, HCM Exhibit 16-11),
  HCM delay against the simulated one, spillback, and the likely causes ranked with a fix.
- `run_health`: gridlock, demand that can't get in or has no route, a run too short for its trips or warm-up.
- `slow_links`: links driven slower than free flow with no signal to explain it (a pre-flight at low demand).
- `resilience`: a disrupted run against its seed's baseline, minute by minute: the drop, recovery time, the
  vehicle-hours lost (Bruneau et al.'s resilience triangle; Taylor, Vulnerability Analysis for Transportation).
- `emissions_report`: fuel and CO2 of the cars and vans (emissions.py), by type and road.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict

from . import emissions, stats
from .results import MINUTE_COLUMNS
from .roundabouts import hcm_entry_capacity, tanner_entry_capacity
from .lane_graph import ROUNDABOUT_ENTRY_VPH, measured_capacity
from .signal_timing import ALL_RED_S, AMBER_S, COORDINATED, effective, incremental_delay, lost_time, webster

# HCM 2000 control delay thresholds, s/veh: (upper bound, LOS); above the last, F
SIGNAL_LOS = ((10, "A"), (20, "B"), (35, "C"), (55, "D"), (80, "E"))  # Exhibit 16-2
UNSIGNALISED_LOS = ((10, "A"), (15, "B"), (25, "C"), (35, "D"), (50, "E"))  # Exhibits 17-2 (TWSC), 17-22 (AWSC)
# HCM 2000 Exhibits 16-11 and 16-12: arrival type by platoon ratio Rp = P / (g/C), and its f_PA
ARRIVAL_TYPES = ((0.50, 1, 1.00), (0.85, 2, 0.93), (1.15, 3, 1.00), (1.50, 4, 1.15), (2.00, 5, 1.00))
LAST_ARRIVAL_TYPE = (6, 1.00)
SPILLBACK_SHARE = 0.9  # a 95th-percentile queue this share of its road's length: spilling back
GRIDLOCK_MIN = 3  # minutes in a row with vehicles on the road, none arriving and next to no movement
GRIDLOCK_KMH = 3.0


def grade(delay_s: float | None, thresholds) -> str | None:
    if delay_s is None:
        return None
    return next((los for top, los in thresholds if delay_s <= top), "F")


def queue_p95(observed: dict, link: str) -> float:
    """A link's (one direction of a road's) 95th-percentile queue, metres a lane of it, from the samples it had one
    (zeros: those it hadn't)."""
    counts, samples = observed.get("queues", {}).get(link, {}), observed.get("samples", 0)
    rank, seen, q95 = 0.95 * samples, samples - sum(counts.values()), 0.0
    for metres in sorted(counts, key=float):
        if seen >= rank:
            break
        seen += counts[metres]
        q95 = float(metres)
    return q95


def _approach_delays(observed: dict) -> dict[str, list[tuple[float, float]]]:
    """Each link ending at a junction: (when it left, control delay s) of every vehicle that drove it: its time on
    the link and through the junction less that at its own free flow (its free speed on the link, then through the
    turn it took; a log without it: the speed limit, then the mean of the turns' speeds)."""
    links, controls = observed.get("links", {}), observed.get("controls", {})
    found: dict = defaultdict(list)
    for link, entered, took, *rest in observed.get("traversal_log", []):
        info = links.get(link)
        if info is None or info["to"] not in controls:
            continue
        free = rest[2] if len(rest) > 2 and rest[2] is not None else info["free_s"] + info.get("cross_s", 0.0)
        found[link].append((entered + took, max(0.0, took - free)))
    return found


def junction_los(kpis: dict, every: bool = False, period_min: int = 15) -> dict:
    """Control delay and level of service per approach and junction (HCM 2000; see the module's docstring), the
    signals' v/c, each approach's 95th-percentile queue against its road, and a grid of LOS per period."""
    observed = kpis["observed"]
    links, controls, signals = observed.get("links", {}), observed.get("controls", {}), observed.get("signals", {})
    seconds = observed.get("observed_s") or kpis["time_s"]
    if not observed.get("traversal_log"):
        raise ValueError("That run recorded no link traversals (a run from before this tool, or too short): run it again.")
    delays = _approach_delays(observed)
    by_junction: dict = defaultdict(list)
    for link, found in delays.items():
        info = links[link]
        node = info["to"]
        flow = len(found) * 3600 / seconds
        mean = statistics.fmean(d for _, d in found)
        thresholds = SIGNAL_LOS if controls[node] == "signalised" else UNSIGNALISED_LOS
        row = {"approach": link, "road": info["road"], "vehicles": len(found), "flow_vph": round(flow),
               "control_delay_s": round(mean, 1), "los": grade(mean, thresholds)}
        if len(found) >= 2:
            row["delay_p95_s"] = round(stats.percentile([d for _, d in found], 95), 1)
        plan = signals.get(node)
        if plan:
            share = _green_share(plan, link)
            saturation = plan["saturation_vph"].get(link) or 0
            if share and saturation:
                row["capacity_vph"] = round(saturation * share)
                row["v_c"] = round(flow / (saturation * share), 2)
        if "circulating" in info:  # a roundabout entry: its capacity against the flow circulating in front of it
            moves = observed.get("turn_traversals", {})
            vc = sum(moves.get(into, {}).get(out, (0,))[0] for into, out in info["circulating"]) * 3600 / seconds
            capacity, hcm = measured_capacity(ROUNDABOUT_ENTRY_VPH, vc), hcm_entry_capacity(vc, info["lanes"])
            row.update(circulating_vph=round(vc), capacity_vph=round(capacity), v_c=round(flow / capacity, 2),
                       hcm_capacity_vph=None if hcm is None else round(hcm))
            if info["lanes"] == 1:  # against a bunched one-lane ring, which the HCM's random stream isn't
                row["tanner_capacity_vph"] = round(tanner_entry_capacity(vc))
        q95, length = queue_p95(observed, link), info["length_m"]
        if q95:
            row.update(queue_p95_m=q95, queue_share_of_road=round(q95 / length, 2) if length else None)
        by_junction[node].append(row)
    junctions = []
    for node, rows in by_junction.items():
        total = sum(row["vehicles"] for row in rows)
        mean = sum(row["control_delay_s"] * row["vehicles"] for row in rows) / total
        thresholds = SIGNAL_LOS if controls[node] == "signalised" else UNSIGNALISED_LOS
        worst = max(rows, key=lambda row: row["control_delay_s"])
        junctions.append({"junction": node, "control": controls[node], "vehicles": total,
                          "control_delay_s": round(mean, 1), "los": grade(mean, thresholds),
                          "worst_approach": worst["approach"], "worst_los": worst["los"],
                          "approaches": sorted(rows, key=lambda row: -row["control_delay_s"])})
    junctions.sort(key=lambda row: -row["control_delay_s"])
    span, start = period_min * 60, observed.get("observed_from_s", 0.0)  # periods from the end of a warm-up
    periods = int(seconds // span)
    grid = {}
    if periods >= 2:
        for node in by_junction:
            thresholds = SIGNAL_LOS if controls[node] == "signalised" else UNSIGNALISED_LOS
            bins: dict = defaultdict(list)
            for link in (row["approach"] for row in by_junction[node]):
                for left, delay in delays[link]:
                    bins[int((left - start) // span)].append(delay)
            grid[node] = [grade(statistics.fmean(bins[i]), thresholds) if bins[i] else "-" for i in range(periods)]
    return {"junctions": junctions if every else junctions[:15], "junctions_graded": len(junctions),
            "periods": {"minutes": period_min, "los": grid} if grid else None,
            "notes": ["Control delay: each vehicle's time on the approach and through the junction less that at its "
                      "own free speed there (a bicycle's own, not the limit), as the HCM measures it (Appendix A of "
                      "Ch 16).",
                      "Signals graded by HCM 2000 Exhibit 16-2; give-way, stop and roundabout entries by Exhibits "
                      "17-2 and 17-22 (HCM 2000 has no roundabout LOS; the HCM 2010 uses the same thresholds).",
                      "v/c of a signal approach: its flow over saturation flow × green share of the cycle.",
                      "Roundabout entry: capacity measured on the simulated drivers against the flow circulating in "
                      "front of it, beside the HCM 6 (Ch 22) entry capacity; the simulated ring is one lane."]}


def _green_share(plan: dict, link: str) -> float | None:
    """The effective green share g/C of the phase serving `link` in a signal's plan."""
    for phase in plan["phases"]:
        if link in phase["links"]:
            return min(effective(phase["green_s"]) / plan["cycle_s"], 1.0) if plan["cycle_s"] else None
    return None


def arrival_type(platoon_ratio: float) -> tuple[int, float]:
    """HCM 2000 Exhibits 16-11 and 16-12: (arrival type 1–6, f_PA) for a platoon ratio Rp."""
    for top, kind, f_pa in ARRIVAL_TYPES:
        if platoon_ratio <= top:
            return kind, f_pa
    return LAST_ARRIVAL_TYPE


def diagnose_signal(kpis: dict, junction: str | None = None) -> dict:
    """Each signalised junction (or the one asked): flow ratio y = q/s per phase (the highest of its approaches),
    Y = Σy, the critical v/c Xc = Y·C/(C − L) and Webster's cycle for it (HCM 2000 Eq 16-8; Webster 1958); per
    approach v/c, the share arriving on green P (vehicles crossing without having stopped), Rp = P/(g/C), the arrival
    type and progression factor PF = (1 − P)·f_PA/(1 − g/C) (Eq 16-10), the HCM delay d1·PF + d2 (Eqs 16-11, 16-12)
    against the simulated control delay, and the 95th-percentile queue against the road. Then the likely causes,
    worst first, each with the tool that fixes it."""
    observed = kpis["observed"]
    signals, links = observed.get("signals", {}), observed.get("links", {})
    if junction is not None and junction not in signals:
        raise ValueError(f"{junction} isn't a signalised junction in that run; signalised: {', '.join(sorted(signals)) or 'none'}.")
    seconds = observed.get("observed_s") or kpis["time_s"]
    delays = _approach_delays(observed)
    arrivals = observed.get("arrivals", {})  # link -> (arrived, of them on green)
    found = []
    for node in [junction] if junction else sorted(signals):
        plan = signals[node]
        cycle, phases = plan["cycle_s"], plan["phases"]
        span = plan.get("lost_s_per_phase", AMBER_S + ALL_RED_S)  # the junction's own amber and all-red
        lost = lost_time(len(phases), span)  # the HCM's t_L = l1 + Y - e a phase, as Webster times it (not Y + all-red)
        approaches, ratios = [], []
        for phase in phases:
            ys = []
            for link in phase["links"]:
                flow = len(delays.get(link, ())) * 3600 / seconds
                saturation = plan["saturation_vph"].get(link) or 0
                share = min(effective(phase["green_s"]) / cycle, 1.0)
                capacity = saturation * share
                x = flow / capacity if capacity else None
                ys.append(flow / saturation if saturation else 0.0)
                row = {"approach": link, "road": links.get(link, {}).get("road"), "flow_vph": round(flow),
                       "saturation_vph": saturation, "green_share": round(share, 3),
                       "v_c": None if x is None else round(x, 2)}
                arrived, on_green = arrivals.get(link, (0, 0))
                if arrived >= 10 and share > 0:
                    p = on_green / arrived
                    kind, f_pa = arrival_type(p / share)
                    pf = (1 - p) * f_pa / (1 - share) if share < 1 else 1.0
                    if kind >= 3:
                        pf = min(pf, 1.0)
                    row.update(arriving_on_green=round(p, 2), platoon_ratio=round(p / share, 2), arrival_type=kind,
                               progression_factor=round(pf, 2))
                else:
                    pf = 1.0
                if x is not None and flow > 0:
                    uniform = 0.5 * cycle * (1 - share) ** 2 / max(1 - min(1.0, x) * share, 1e-9)
                    row["hcm_delay_s"] = round(uniform * pf + incremental_delay(x, capacity), 1)
                if delays.get(link):
                    row["simulated_delay_s"] = round(statistics.fmean(d for _, d in delays[link]), 1)
                q95, length = queue_p95(observed, link), links.get(link, {}).get("length_m") or 0
                if q95 and length:
                    row.update(queue_p95_m=q95, spills_back=q95 >= SPILLBACK_SHARE * length)
                approaches.append(row)
            ratios.append(max(ys, default=0.0))
        big_y = sum(ratios)
        xc = big_y * cycle / (cycle - lost) if cycle > lost else None
        webster_cycle = round(webster(ratios, span)[0]) if big_y < 0.95 else None
        report = {"junction": node, "method": plan["method"], "cycle_s": cycle, "flow_ratios": [round(y, 3) for y in ratios],
                  "Y": round(big_y, 3), "critical_v_c": None if xc is None else round(xc, 2), "webster_cycle_s": webster_cycle,
                  "approaches": approaches, "causes": _causes(plan, approaches, xc, webster_cycle)}
        found.append(report)
    found.sort(key=lambda r: -(r["critical_v_c"] or 0))
    return {"signals": found, "notes": [
        "Flows are the vehicles that drove each approach in the run; saturation flows are the simulation's own "
        "(measured on its drivers, per lane and turn).",
        "Effective green = green + 2.8 s of amber used - 1.8 s start-up lost (signal_timing.py); L = the junction's "
        "amber + all-red a phase (from its approach speed and width).",
        "Arrivals on green: vehicles that joined the queue or reached the stop line while their signal was green "
        "(HCM 2000 p16-19).",
        "The HCM's delay assumes nothing beyond the stop line holds traffic back; a simulated delay well above it "
        "means something does (a full exit, lane changes, a queue from the next junction)."]}


def _causes(plan: dict, approaches: list[dict], xc: float | None, webster_cycle: float | None) -> list[dict]:
    """The likely reasons a signal performs badly, worst first, each with a fix."""
    causes = []
    worst_x = max((row["v_c"] for row in approaches if row["v_c"] is not None), default=0.0)
    if xc is not None and xc > 1.0:
        causes.append({"cause": "over capacity", "severity": round(xc, 2),
                       "why": f"Critical v/c {xc:.2f}: no timing can serve this demand.",
                       "fix": "More capacity (update_roads: lanes), less demand through it (assign_routes, scale_demand), "
                              "or grade separation (build_interchange)."})
    elif worst_x > 1.0:
        causes.append({"cause": "greens don't match demand", "severity": round(worst_x, 2),
                       "why": f"An approach runs at v/c {worst_x:.2f} while the junction as a whole could cope "
                              f"(critical v/c {xc:.2f})." if xc is not None else
                              f"An approach runs at v/c {worst_x:.2f}; the cycle is no longer than its lost time.",
                       "fix": "Retime: set_signal_timing method webster, or optimise_signals."})
    elif xc is not None and xc > 0.9:
        causes.append({"cause": "near capacity", "severity": round(xc, 2),
                       "why": f"Critical v/c {xc:.2f}: delay climbs steeply with a little more traffic.",
                       "fix": "Watch it in run_experiment at demand_scale 1.1; plan capacity."})
    if webster_cycle and plan["cycle_s"] > 1.5 * webster_cycle and (xc or 0) < 0.85:
        causes.append({"cause": "cycle too long", "severity": round(plan["cycle_s"] / webster_cycle, 2),
                       "why": f"Cycle {plan['cycle_s']:.0f} s against Webster's {webster_cycle} s for this demand: long reds.",
                       "fix": "set_signal_timing method webster, or a lower cycle_max_s."})
    poor = [row for row in approaches if row.get("arrival_type", 3) <= 2 and row["flow_vph"] >= 100]
    if plan["method"] in COORDINATED and poor:
        causes.append({"cause": "poor progression", "severity": len(poor),
                       "why": "Platoons reach " + ", ".join(row["approach"] for row in poor) + " on red (arrival type 1-2).",
                       "fix": "Offsets: set_signal_timing method transyt (it optimises them) for the corridor."})
    held = [row for row in approaches if row.get("simulated_delay_s") is not None and row.get("hcm_delay_s") is not None
            and row["simulated_delay_s"] > 2 * row["hcm_delay_s"] and row["simulated_delay_s"] - row["hcm_delay_s"] > 20]
    if held:
        causes.append({"cause": "held beyond the signal", "severity": len(held),
                       "why": "Delay on " + ", ".join(f"{row['approach']} ({row['simulated_delay_s']:.0f} s against the "
                                                      f"HCM's {row['hcm_delay_s']:.0f} s)" for row in held)
                              + ": more than the timing explains.",
                       "fix": "Look past the stop line: the exit road's queue (junction_los downstream), lane changes "
                              "(read_traffic), or turning traffic giving way."})
    spill = [row for row in approaches if row.get("spills_back")]
    if spill:
        causes.append({"cause": "queue spills back", "severity": len(spill),
                       "why": "The 95th-percentile queue fills " + ", ".join(row["approach"] for row in spill) + ".",
                       "fix": "Longer green for it, a longer approach, or less demand; check the upstream junction too."})
    return causes or [{"cause": "none found", "severity": 0, "why": "Within capacity, cycle near Webster's, no spillback.",
                       "fix": ""}]


def run_health(kpis: dict) -> dict:
    """Whether a run can be trusted, and if not why: gridlock (GRIDLOCK_MIN minutes in a row with vehicles on the
    road, none arriving, mean speed under GRIDLOCK_KMH), vehicles that couldn't enter (waiting at their origin),
    pairs with no route, most trips still under way at the end, a warm-up (MSER-5) of half the run or more, and
    signal approaches over capacity. Each problem comes with what to do (FHWA Traffic Analysis Toolbox III,
    error checking, p59–66)."""
    observed = kpis["observed"]
    rows = observed.get("minute_rows", [])
    problems, checks = [], {}
    on, arrived, speed = map(MINUTE_COLUMNS.index, ("Vehicles on the road", "Arrived this minute", "Mean speed (km/h)"))
    run, start, stuck = 0, None, []
    for row in rows:
        if row[on] > 0 and row[arrived] == 0 and (row[speed] or 0) < GRIDLOCK_KMH:
            run += 1
            if run == GRIDLOCK_MIN:
                start = row[0] / 60 - GRIDLOCK_MIN
        else:
            if run >= GRIDLOCK_MIN:
                stuck.append((start, run))
            run = 0
    if run >= GRIDLOCK_MIN:
        stuck.append((start, run))
    checks["gridlock"] = [{"from_min": round(s), "minutes": n} for s, n in stuck]
    if stuck:
        problems.append({"problem": "gridlock", "detail": f"Nothing moved for {stuck[0][1]} min from minute {stuck[0][0]:.0f}.",
                         "fix": "Find the jam (junction_los, diagnose_signal, read_traffic); a junction blocking itself "
                                "or too much demand (scale_demand)."})
    waiting = sum(observed.get("waiting_to_enter", {}).values())
    checks["waiting_to_enter"] = waiting
    spawned = max(kpis.get("spawned") or 0, 1)
    if waiting > max(10, 0.05 * spawned):
        worst = sorted(observed["waiting_to_enter"].items(), key=lambda item: -item[1])[:5]
        problems.append({"problem": "demand can't get in", "detail": f"{waiting} vehicles waiting at their origin at the "
                         "end: " + ", ".join(f"{link} {n}" for link, n in worst) + ".",
                         "fix": "The first junction or road from those ends is full: more lanes there, or less demand."})
    teleports = observed.get("teleports", [])
    checks["teleports"] = len(teleports)
    if teleports:  # each a vehicle stuck TIME_TO_TELEPORT_S at a queue's front, moved on (a gridlock broken)
        where = Counter(event["from"] for event in teleports).most_common(5)
        problems.append({"problem": "gridlock broken by teleports", "detail": f"{len(teleports)} vehicles stood 5 min at a "
                         "queue's front and were moved on along their routes, as SUMO does: from " +
                         ", ".join(f"{link} {n}" for link, n in where) + ".",
                         "fix": "The junctions those links lead to lock up: signalise them (set_junction_control) or "
                                "lower the demand; the run's figures are right, but that gridlock is real."})
    unreachable = observed.get("unreachable", [])
    checks["unreachable_pairs"] = unreachable
    if unreachable:
        problems.append({"problem": "pairs with no route", "detail": ", ".join(unreachable[:10]),
                         "fix": "Connect them (add_road) or remove their demand: their trips never ran."})
    on_road = kpis.get("on_road") or 0
    checks["on_road_at_end_share"] = round(on_road / spawned, 2)
    if on_road > 0.5 * spawned and spawned > 20:
        problems.append({"problem": "most trips unfinished", "detail": f"{on_road} of {spawned} vehicles still on the road.",
                         "fix": "Run longer than the longest trip (or check for gridlock): trip figures cover only short trips."})
    if len(rows) >= 10:
        warm = stats.mser([row[on] for row in rows])
        checks["warm_up_min"] = warm
        if warm >= len(rows) / 2:
            problems.append({"problem": "too short to settle", "detail": f"Warm-up (MSER-5) takes {warm} of {len(rows)} min.",
                             "fix": "Run at least three times the warm-up, or the network is filling up (over capacity)."})
    over = []
    if observed.get("signals") and observed.get("traversal_log"):
        for signal in diagnose_signal(kpis)["signals"]:
            over += [row["approach"] for row in signal["approaches"] if (row["v_c"] or 0) > 1.0]
    checks["signal_approaches_over_capacity"] = over
    if over:
        problems.append({"problem": "signal approaches over capacity", "detail": ", ".join(over[:10]),
                         "fix": "diagnose_signal shows why and what to change; their queues grow all run, so a longer run "
                                "reads worse."})
    return {"healthy": not problems, "problems": problems, "checks": checks}


def slow_links(kpis: dict, factor: float = 1.5, slack_s: float = 5.0) -> list[dict]:
    """Links whose vehicles took over `factor` × their free-flow time (plus `slack_s`) though no signal ends them:
    at low demand that is the network or the simulation, not traffic (FHWA Toolbox III, p63: vehicles slowing or
    stopping without reason)."""
    observed = kpis["observed"]
    links, controls = observed.get("links", {}), observed.get("controls", {})
    found = []
    for link, (count, mean) in observed.get("traversals", {}).items():
        info = links.get(link)
        if info is None or count < 3 or controls.get(info["to"]) == "signalised":
            continue
        free = info["free_s"] + info.get("cross_s", 0.0)
        if mean > factor * free + slack_s:
            found.append({"link": link, "road": info["road"], "to": info["to"], "traversals": count,
                          "mean_s": round(mean, 1), "free_flow_s": round(free, 1), "ratio": round(mean / free, 2) if free else None})
    return sorted(found, key=lambda row: -(row["ratio"] or 0))


RESILIENCE_MEASURES = {"throughput": ("Throughput (veh/h)", True), "speed": ("Mean speed (km/h)", True),
                       "on_road": ("Vehicles on the road", False)}


def resilience(base: dict, disrupted: dict, measure: str = "throughput", tolerance: float = 0.05) -> dict:
    """A disrupted run against its seed's undisrupted run, minute by minute (both smoothed over 3 minutes): the
    disruption's start (the first minute the measure is off by more than `tolerance`), the biggest drop and when,
    the minutes to recover (back within `tolerance` for 3 minutes running), the share of performance kept over the
    disrupted window (1 − the area of the triangle over its length), and the extra vehicle-hours of delay
    (Bruneau et al. 2003; Taylor, Vulnerability Analysis for Transportation, Ch 2)."""
    if measure not in RESILIENCE_MEASURES:
        raise ValueError(f"measure is one of {', '.join(RESILIENCE_MEASURES)}.")
    name, higher = RESILIENCE_MEASURES[measure]
    column, delay = MINUTE_COLUMNS.index(name), MINUTE_COLUMNS.index("Delay (veh·h)")
    a, b = base["observed"].get("minute_rows", []), disrupted["observed"].get("minute_rows", [])
    minutes = min(len(a), len(b))
    if minutes < 6:
        raise ValueError("Both runs need 6 minutes or more.")

    def smooth(rows):
        values = [row[column] or 0.0 for row in rows[:minutes]]
        return [statistics.fmean(values[max(0, i - 1):i + 2]) for i in range(minutes)]

    base_values, hit_values = smooth(a), smooth(b)
    deficit = []
    for x, y in zip(base_values, hit_values):
        gap = (x - y) if higher else (y - x)
        deficit.append(gap / x if x else 0.0)
    start = next((i for i, d in enumerate(deficit) if d > tolerance), None)
    extra = sum(row[delay] for row in b[:minutes]) - sum(row[delay] for row in a[:minutes])
    if start is None:
        return {"measure": measure, "disrupted": False, "extra_delay_veh_h": round(extra, 2),
                "note": f"The disrupted run never fell more than {tolerance:.0%} below the baseline."}
    worst = max(range(start, minutes), key=deficit.__getitem__)
    recovered = next((i for i in range(worst, minutes - 2) if all(d <= tolerance for d in deficit[i:i + 3])), None)
    end = recovered if recovered is not None else minutes
    window = deficit[start:end] or [0.0]
    return {"measure": measure, "disrupted": True, "starts_min": start, "worst_min": worst,
            "biggest_drop_percent": round(100 * deficit[worst], 1),
            "recovered_after_min": None if recovered is None else recovered - start,
            "recovered": recovered is not None,
            "performance_kept": round(1 - statistics.fmean(max(d, 0.0) for d in window), 3),
            "lost_area_percent_min": round(100 * sum(max(d, 0.0) for d in window), 1),
            "extra_delay_veh_h": round(extra, 2),
            "note": "Smoothed over 3 minutes; recovery = back within the tolerance for 3 minutes running. "
                    "'Not recovered' within the run means it needs to run longer after the disruption."}


def emissions_report(kpis: dict) -> dict:
    """Fuel and CO2 of the run's cars and vans (emissions.py: Biggs and Akçelik's power model), each type's and the
    roads burning most, per vehicle-km and person-km (the types' occupancy)."""
    fuel = kpis["observed"].get("fuel")
    if not fuel:
        raise ValueError("That run recorded no fuel (a run from before this tool): run it again.")
    occupancy = kpis.get("occupancy", {})
    by_type = {kind: emissions.summary(ml, fuel["by_type_km"].get(kind, 0.0), occupancy.get(kind, 1.0))
               for kind, ml in sorted(fuel["by_type_ml"].items())}
    total_ml, total_km = sum(fuel["by_type_ml"].values()), sum(fuel["by_type_km"].values())
    roads = sorted(fuel["by_road_ml"].items(), key=lambda item: -item[1])[:10]
    return {"total": emissions.summary(total_ml, total_km), "by_type": by_type,
            "roads_burning_most": [{"where": where, "fuel_l": round(ml / 1000, 2)} for where, ml in roads],
            "left_out": "Buses, lorries, motorcycles and bicycles (the car model doesn't fit them).",
            "notes": ["Biggs and Akçelik's (1986) power model with Bowyer et al.'s (1985) 1,200 kg car: reads high for a "
                      "modern car; use it to compare designs.", "CO2 = 2.35 kg a litre of petrol (US EPA)."]}


STARVE_S = 180.0  # an arm holding traffic on red longer than this: starved (W_max-like; Smith et al.'s
# compliance pattern in Explainable AI for ITS Ch 2: a fixed scenario battery checked against invariants)
EMERGENCY_STANDSTILL_S = 30.0  # an emergency vehicle standing still longer than this at once: held up


def _held_on_red(onsets: list, crossed: list, driven: list, free_s: float, arrived: int, start: float,
                 end: float) -> float:
    """The longest a signalised approach held traffic on red, seconds: for each vehicle that left on a green that
    began after it reached the stop line (entered + the link's free-flow time), that green's onset less the later of
    when it reached the line and the approach's last crossing before the onset (vehicles cross on green or amber, so
    the red began after it); with vehicles still on it at the end (arrived, not crossed), from its last onset or
    crossing to the end. A green that rests, or a long cycle's green, holds no one."""
    longest = 0.0
    for entered, took in driven:
        reached, left = entered + free_s, entered + took
        on = bisect_right(onsets, left) - 1  # the green it left on
        if on < 0 or onsets[on] <= reached:
            continue  # it came on that green (or left on one from before the record)
        before = bisect_left(crossed, onsets[on]) - 1
        longest = max(longest, onsets[on] - max(reached, crossed[before] if before >= 0 else start))
    if arrived > len(crossed):  # some still waiting
        longest = max(longest, end - max([start, *onsets[-1:], *crossed[-1:]]))
    return longest


def invariants(kpis: dict, reported: dict | None = None, starve_s: float = STARVE_S,
               standstill_s: float = EMERGENCY_STANDSTILL_S) -> list[dict]:
    """What a run with an agent deciding broke, each with its evidence: a signal arm holding traffic on red for
    over `starve_s` (from the run's start to its end), an emergency vehicle standing still over `standstill_s` at
    once, gridlock, and agent errors (its extension's report). Empty: it kept every one."""
    observed = kpis["observed"]
    broken = []
    start = observed.get("observed_from_s", 0.0)  # recorded from the end of a warm-up
    end = start + (observed.get("observed_s") or kpis["time_s"])
    arrivals, links = observed.get("arrivals", {}), observed.get("links", {})
    driven, crossed = defaultdict(list), defaultdict(list)
    for link, entered, took, *_ in observed.get("traversal_log", []):
        driven[link].append((entered, took))
    for _, link, _, at, *_ in observed.get("stop_lines", []):
        crossed[link].append(at)
    for link, (arrived, _) in arrivals.items():
        if not arrived:
            continue
        gap = _held_on_red(sorted(observed.get("green_starts", {}).get(link, [])), sorted(crossed[link]), driven[link],
                           links.get(link, {}).get("free_s", 0.0), arrived, start, end)
        if gap > starve_s:
            broken.append({"invariant": "no arm starved", "where": f"{link} at {links.get(link, {}).get('to')}",
                           "evidence": f"{gap:.0f} s on red with traffic waiting"})
    for vehicle, waited in observed.get("emergency_standstill", {}).items():
        if waited > standstill_s:
            broken.append({"invariant": "emergency vehicles keep moving", "where": vehicle,
                           "evidence": f"stood still {waited:.0f} s at once"})
    if run_health(kpis)["checks"]["gridlock"]:
        broken.append({"invariant": "no gridlock", "where": "network", "evidence": "nothing moved for 3 minutes"})
    errors = (reported or {}).get("agent_errors")
    if errors:
        broken.append({"invariant": "the agent answers", "where": "agent", "evidence": f"{errors} errors"})
    return broken


def co2_kg(kpis: dict) -> float | None:
    """The run's CO2 from cars and vans, kg (None when it recorded no fuel)."""
    fuel = (kpis.get("observed") or {}).get("fuel")
    if not fuel:
        return None
    return round(sum(fuel["by_type_ml"].values()) / 1000 * emissions.CO2_KG_PER_L, 2)
