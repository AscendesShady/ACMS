"""Run analyses (analysis.py) and the fuel model (emissions.py): HCM level of service and signal diagnosis on a real
headless run, run health, slow links, resilience, emissions.
Run with `python tests/test_analysis.py` (or the test monitor)."""

import tempfile
from pathlib import Path

from traffic_simulator import analysis, emissions, experiments, results
from traffic_simulator.mode_behavior import VehicleMode
from traffic_simulator.network_generator import generate_network
from traffic_simulator.results import MINUTE_COLUMNS
from traffic_simulator.simulation import PairDemand

_RUN = {}


def signalised_run() -> dict:
    """A 3 × 3 grid, every junction signalised (Webster), 20 simulated minutes of busy demand: its KPIs with what it
    observed, as MainWindow._observed_kpis joins them."""
    if not _RUN:
        network = generate_network(3, 3)
        for node in network.nodes.values():
            node.control = "signalised"
        ends = sorted(network.approach_nodes)
        demand = [PairDemand(o, d, 120.0, None, [], "Random", (100,)) for o in ends for d in ends if o != d][:40]
        modes = [VehicleMode("Car", "#888888", occupancy=1.5)]
        recorder = results.RunRecorder(network, modes, folder=Path(tempfile.mkdtemp()) / "run")
        simulation, recorder = experiments.run_headless(network, demand, modes, 1, 20 * 60, recorder=recorder, observe=True)
        kpis = recorder.kpis()
        kpis["observed"] = {**recorder.observed(), **simulation.observations()}
        _RUN.update(kpis)
    return _RUN


def test_the_fuel_model_gives_its_published_figures():
    assert emissions.fuel_rate_ml_s(0.0, 0.0) == emissions.ALPHA_ML_S  # idling: alpha, 0.375 mL/s
    assert emissions.fuel_rate_ml_s(10.0, -3.0) == emissions.ALPHA_ML_S  # braking: no tractive force, idle rate
    cruise = emissions.fuel_rate_ml_s(15.0, 0.0)  # 0.375 + 0.09 × (0.333 + 0.00108 × 225) × 15
    assert abs(cruise - (0.375 + 0.09 * (0.333 + 0.00108 * 225) * 15)) < 1e-9
    assert 6 < cruise / 15 * 1000 / 10 < 9  # about 7.7 L/100 km at 54 km/h: a car's
    assert emissions.fuel_rate_ml_s(15.0, 1.0) > cruise  # accelerating costs more
    assert emissions.light_motor(5.0, 200) and emissions.light_motor(6.5, 200)  # car, van
    assert not emissions.light_motor(12.0, 85) and not emissions.light_motor(1.6, 20) and not emissions.light_motor(2.2, 200)
    found = emissions.summary(10_000, 100, 1.5)  # 10 L over 100 km
    assert found["fuel_l"] == 10 and found["fuel_l_per_100km"] == 10 and found["co2_kg"] == 23.5
    assert found["co2_g_per_person_km"] == round(23_500 / 150, 1)


def test_hcm_tables():
    assert analysis.grade(10, analysis.SIGNAL_LOS) == "A" and analysis.grade(10.1, analysis.SIGNAL_LOS) == "B"
    assert analysis.grade(80, analysis.SIGNAL_LOS) == "E" and analysis.grade(80.1, analysis.SIGNAL_LOS) == "F"
    assert analysis.grade(50, analysis.UNSIGNALISED_LOS) == "E" and analysis.grade(51, analysis.UNSIGNALISED_LOS) == "F"
    assert analysis.grade(None, analysis.SIGNAL_LOS) is None
    # HCM 2000 Exhibits 16-11 and 16-12: arrival type and f_PA by platoon ratio
    assert analysis.arrival_type(0.4) == (1, 1.00) and analysis.arrival_type(0.7) == (2, 0.93)
    assert analysis.arrival_type(1.0) == (3, 1.00) and analysis.arrival_type(1.3) == (4, 1.15)
    assert analysis.arrival_type(1.8) == (5, 1.00) and analysis.arrival_type(2.5) == (6, 1.00)


def test_junction_level_of_service_on_a_real_run():
    kpis = signalised_run()
    observed = kpis["observed"]
    assert len(observed["traversal_log"]) == sum(n for n, _ in observed["traversals"].values())  # every traversal logged
    found = analysis.junction_los(kpis)
    assert found["junctions_graded"] == 9  # every junction had traffic
    for junction in found["junctions"]:
        assert junction["los"] in "ABCDEF" and junction["control"] == "signalised"
        total = sum(a["vehicles"] for a in junction["approaches"])
        weighted = sum(a["control_delay_s"] * a["vehicles"] for a in junction["approaches"]) / total
        assert abs(weighted - junction["control_delay_s"]) < 0.2  # flow-weighted over its approaches
        assert junction["los"] == analysis.grade(junction["control_delay_s"], analysis.SIGNAL_LOS)
        for approach in junction["approaches"]:
            assert approach["control_delay_s"] >= 0 and approach["v_c"] > 0 and approach["capacity_vph"] > 0
    delays = [j["control_delay_s"] for j in found["junctions"]]
    assert delays == sorted(delays, reverse=True)
    assert found["periods"] is None  # 20 min: one whole 15-min period, and a grid needs two


def test_signal_diagnosis_on_a_real_run():
    kpis = signalised_run()
    found = analysis.diagnose_signal(kpis)
    assert len(found["signals"]) == 9
    for signal in found["signals"]:
        assert abs(signal["Y"] - sum(signal["flow_ratios"])) < 0.01
        lost = len(signal["flow_ratios"]) * 5.0  # amber 3 s + all-red 2 s a phase
        assert abs(signal["critical_v_c"] - signal["Y"] * signal["cycle_s"] / (signal["cycle_s"] - lost)) < 0.02
        assert signal["causes"]  # at least "none found"
        for approach in signal["approaches"]:
            if "arriving_on_green" in approach:
                assert 0 <= approach["arriving_on_green"] <= 1 and 1 <= approach["arrival_type"] <= 6
                assert approach["hcm_delay_s"] > 0
    one = analysis.diagnose_signal(kpis, "2,2")
    assert [s["junction"] for s in one["signals"]] == ["2,2"]
    try:
        analysis.diagnose_signal(kpis, "9,9")
    except ValueError as exc:
        assert "isn't a signalised junction" in str(exc)
    else:
        raise AssertionError("an unknown junction is refused")
    plan = {"method": "fixed", "cycle_s": 120}
    over = analysis._causes(plan, [{"approach": "a", "v_c": 1.3, "flow_vph": 900}], 1.2, None)
    assert over[0]["cause"] == "over capacity" and "update_roads" in over[0]["fix"]
    split = analysis._causes(plan, [{"approach": "a", "v_c": 1.2, "flow_vph": 900}], 0.8, 60)
    assert [c["cause"] for c in split] == ["greens don't match demand", "cycle too long"]
    held = analysis._causes(plan, [{"approach": "a", "v_c": 0.5, "flow_vph": 300, "simulated_delay_s": 90,
                                    "hcm_delay_s": 20}], 0.5, 100)
    assert held[0]["cause"] == "held beyond the signal"


def test_emissions_of_a_real_run():
    kpis = signalised_run()
    found = analysis.emissions_report(kpis)
    assert found["total"]["fuel_l"] > 0 and set(found["by_type"]) == {"Car"}
    assert 5 < found["total"]["fuel_l_per_100km"] < 40  # stop-go city driving in a 1980s car
    car = found["by_type"]["Car"]
    assert abs(car["co2_g_per_person_km"] - car["co2_g_per_km"] / 1.5) < 0.2  # occupancy 1.5
    assert abs(analysis.co2_kg(kpis) - found["total"]["co2_kg"]) < 0.05
    assert analysis.co2_kg({"observed": {}}) is None


def minute_rows(rows: list[dict]) -> list[list]:
    """Per-minute rows (MINUTE_COLUMNS) from a few named values a minute."""
    found = []
    for i, values in enumerate(rows, 1):
        row = [0] * len(MINUTE_COLUMNS)
        row[0] = i * 60.0
        for name, value in values.items():
            row[MINUTE_COLUMNS.index(name)] = value
        found.append(row)
    return found


def test_run_health_finds_what_makes_a_run_untrustworthy():
    flowing = [{"Vehicles on the road": 50, "Arrived this minute": 20, "Mean speed (km/h)": 30}] * 12
    good = {"spawned": 300, "on_road": 50, "observed": {"minute_rows": minute_rows(flowing)}}
    assert analysis.run_health(good)["healthy"]
    stuck = flowing[:4] + [{"Vehicles on the road": 120, "Arrived this minute": 0, "Mean speed (km/h)": 0.5}] * 5
    bad = {"spawned": 300, "on_road": 200, "observed": {
        "minute_rows": minute_rows(stuck), "waiting_to_enter": {"1W>": 40, "2N>": 3}, "unreachable": ["1E->3N"]}}
    found = analysis.run_health(bad)
    problems = {p["problem"]: p for p in found["problems"]}
    assert not found["healthy"] and set(problems) >= {"gridlock", "demand can't get in", "pairs with no route",
                                                      "most trips unfinished"}
    assert found["checks"]["gridlock"] == [{"from_min": 4, "minutes": 5}] and "1W> 40" in problems["demand can't get in"]["detail"]


def test_slow_links_need_no_signal_to_blame():
    kpis = {"observed": {"links": {"a>": {"road": "a", "to": "1,1", "free_s": 10.0, "cross_s": 2.0},
                                   "b>": {"road": "b", "to": "2,1", "free_s": 10.0, "cross_s": 2.0},
                                   "c>": {"road": "c", "to": "3,1", "free_s": 10.0, "cross_s": 2.0}},
                         "controls": {"1,1": "uncontrolled", "2,1": "signalised", "3,1": "uncontrolled"},
                         "traversals": {"a>": (5, 40.0), "b>": (5, 60.0), "c>": (5, 14.0)}}}
    found = analysis.slow_links(kpis)
    assert [row["link"] for row in found] == ["a>"]  # b's wait is its signal's; c is near free flow
    assert found[0]["ratio"] == round(40 / 12, 2)


def test_invariants_name_what_a_run_with_an_agent_broke():
    flowing = minute_rows([{"Vehicles on the road": 20, "Arrived this minute": 10, "Mean speed (km/h)": 30}] * 10)
    kept = {"time_s": 600, "spawned": 100, "on_road": 20, "observed": {
        "observed_s": 600, "minute_rows": flowing, "links": {"a>": {"to": "1,1"}}, "arrivals": {"a>": (40, 20)},
        "green_starts": {"a>": [50, 150, 250, 350, 450, 550]}, "emergency_standstill": {"Emergency3": 12.0}}}
    assert analysis.invariants(kept) == []
    starved = {**kept, "observed": {**kept["observed"], "green_starts": {"a>": [50, 400]}, "emergency_standstill":
                                    {"Emergency3": 45.0}}}
    broken = {b["invariant"]: b for b in analysis.invariants(starved, {"agent_errors": 2})}
    assert set(broken) == {"no arm starved", "emergency vehicles keep moving", "the agent answers"}
    assert "350 s without a green" in broken["no arm starved"]["evidence"] and "1,1" in broken["no arm starved"]["where"]
    quiet = {**kept, "observed": {**kept["observed"], "arrivals": {"a>": (0, 0)}, "green_starts": {}}}
    assert analysis.invariants(quiet) == []  # an arm nobody came to isn't starved


def test_resilience_triangle():
    base = {"observed": {"minute_rows": minute_rows(
        [{"Throughput (veh/h)": 600, "Delay (veh·h)": 1.0}] * 20)}}
    shape = [600] * 5 + [300] * 5 + [600] * 10
    hit = {"observed": {"minute_rows": minute_rows(
        [{"Throughput (veh/h)": v, "Delay (veh·h)": 1.0 if v == 600 else 3.0} for v in shape])}}
    found = analysis.resilience(base, hit)
    assert found["disrupted"] and found["starts_min"] == 4  # smoothed over 3 minutes: the drop shows from minute 4
    assert found["biggest_drop_percent"] == 50.0 and found["recovered"]
    assert 5 <= found["recovered_after_min"] <= 8 and found["extra_delay_veh_h"] == 10.0
    assert 0 < found["performance_kept"] < 1
    same = analysis.resilience(base, base)
    assert not same["disrupted"]
    try:
        analysis.resilience(base, hit, "beauty")
    except ValueError as exc:
        assert "measure is one of" in str(exc)
    else:
        raise AssertionError("an unknown measure is refused")


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
