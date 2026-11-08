"""Iterative searches over headless runs (no Qt): route choice towards equilibrium by successive averages, and each
pair's trip times for elastic demand. equilibrium takes the quick_run callable the window passes (self._quick_run).
Cut from main.MainWindow's _equilibrium and _equilibrate_demand (docs/architecture/experiments.md)."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from . import experiments
from .lane_graph import Graph, path_size_logit


def equilibrium(network, demand, modes, seed, duration, iterations, choice, override, progress, cancelled,
                until_gap=None, label="Route choice", *, quick_run) -> tuple[dict, list]:
    """Route choice towards equilibrium by the method of successive averages over headless runs (Handbook of
    Transportation Science p391-392): each iteration runs with the link times so far, measures each link's
    traversal time and moves the times 1/n of the way to it, every run on the same seed (common random numbers:
    a new seed each time kept the gap at 0.05-0.09 by noise alone). The relative gap is the share of the flow off
    its logit share at the times just measured (½Σ|P − P_logit|, 0 at stochastic user equilibrium); the time spent
    over that on the fastest route (`excess_cost`) has a positive floor under logit choice, so it never falls to 0.
    `until_gap` stops it once the gap is that low. Returns (route choice, each iteration's figures). `network` is
    changed (its route_choice)."""
    choice = deepcopy(choice)
    rates = {(pair.origin, pair.destination): pair.hourly for pair in experiments.at_busiest_hour(demand)}
    history = []
    for n in range(1, iterations + 1):
        if cancelled():
            break
        network.route_choice = deepcopy(choice)
        kpis = quick_run(network, demand, modes, seed, duration * 60, override, progress, cancelled,
                          f"{label}, iteration {n} of {iterations}")
        observed = kpis["observed"]
        measured = {link: mean for link, (count, mean) in observed["traversals"].items() if count >= 3}
        old = {link: choice["link_times_s"].get(link, info["free_s"]) for link, info in observed["links"].items()}
        new = dict(choice["link_times_s"])
        for link, mean in measured.items():
            new[link] = round(old[link] + (mean - old[link]) / n, 2)
        graph = Graph(network)  # the routes drivers took, by the times so far
        # Each move's own time (the link and that move's junction delay, counted once: Graph.move_time)
        turns = deepcopy(choice.get("turn_times_s", {}))
        for link, moves in observed.get("turn_traversals", {}).items():
            for after, (count, mean) in moves.items():
                if count >= 3:
                    was = graph.move_time(graph.links[link], graph.links[after])
                    turns.setdefault(link, {})[after] = round(was + (mean - was) / n, 2)
        network.route_choice = {**choice, "link_times_s": new, "turn_times_s": turns}
        priced = Graph(network)
        spent = best = off = total = 0.0
        theta = float(choice.get("theta_per_min", 0.5))
        for pair, q in rates.items():
            options = graph.route_shares(*pair)
            costs = [priced.path_time(path) for _, path in options]
            if costs:
                spent += q * sum(share * cost for (share, _), cost in zip(options, costs))
                best += q * min(costs)
                aims = path_size_logit([path for _, path in options], costs, theta)
                off += q * sum(abs(share - aim) for (share, _), aim in zip(options, aims)) / 2
                total += q
        gap = round(off / total, 4) if total else None
        history.append({"iteration": n, "seed": seed, "mean_trip_s": kpis["mean_trip_s"],
                        "delay_veh_h": kpis["delay_veh_h"], "links_measured": len(measured), "relative_gap": gap,
                        "excess_cost": round((spent - best) / best, 4) if best else None})
        choice["link_times_s"], choice["turn_times_s"] = new, turns
        if until_gap is not None and gap is not None and gap <= until_gap:
            break
    network.route_choice = choice
    return choice, history


def trip_times(observed) -> dict:  # each pair's trip times, the unfinished ones' so far too
    found: dict = defaultdict(list)
    for origin, destination, _kind, trip, *_ in [*observed["trips"], *observed.get("unfinished", [])]:
        found[(origin, destination)].append(trip)
    return found
