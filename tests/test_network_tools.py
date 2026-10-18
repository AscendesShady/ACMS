"""The assistant's network tools. Run with `python tests/test_network_tools.py` (or the test monitor)."""

from traffic_simulator import network_tools as tools
from traffic_simulator.network_generator import generate_network


def run(network, name, **args):
    return tools.run_tool(network, name, args)


def refused(network, name, **args) -> str:
    try:
        run(network, name, **args)
    except tools.ToolError as exc:
        return str(exc)
    raise AssertionError(f"{name} {args} should have been refused")


def test_every_tool_is_described_for_the_model():
    names = [spec["name"] for spec in tools.tool_specs()]
    assert len(names) == len(set(names))
    for spec in tools.tool_specs():
        parameters = spec["parameters"]
        assert spec["description"] and parameters["type"] == "object"
        assert set(parameters["required"]) <= set(parameters["properties"]), spec["name"]
        assert spec["name"] in tools._FUNCTIONS or spec["name"] in tools.UI_TOOLS
        assert tools.describe_call(spec["name"], {})  # never crashes on missing arguments


def test_the_assistant_can_do_only_what_its_tools_say():
    """Its whole reach, pinned: a new tool, or a new way to change things, fails here until it is
    added on purpose. No tool writes files, so it can't change the app's code; demand it changes
    only through the Demand Generator's tools."""
    advertised = {spec["name"] for spec in tools.tool_specs()}
    assert set(tools._FUNCTIONS) | tools.UI_TOOLS == advertised  # nothing callable beyond the list
    assert tools.EDIT_TOOLS == {
        "set_od_demand", "shape_daily_curve", "generate_demand", "scale_demand", "calibrate_demand",  # demand: numbers, spread, gravity, scaling, counts
        "choose_modes",  # the split by mode choice (logit)
        "set_junction_control", "set_signal_timing", "update_roads", "add_junction", "add_road", "add_boundary_end",
        "move_junctions", "delete_objects", "build_interchange", "add_slip_road", "set_end_flows", "generate_grid",
        "deploy_institutions", "update_institution", "network_from_background", "place_map",  # buildings; the background
        "place_institution", "institution_mode_split",  # one building with its catchment; its logit mode split
        "add_bus_stop", "update_bus_stop",
        "create_vehicle_types", "update_vehicle_type",  # vehicle types made as SUMO classes
        "run_simulation", "run_experiment", "set_extension",  # tests: they write result workbooks, so they ask too
        "schedule_emergency", "start_batch", "replay_run",  # the extensions' own actions: a schedule, a batch, a replay
        "preflight_check",  # two checking runs
        "equilibrate_demand",  # elastic demand, by runs
        "test_agent_compliance", "decision_sensitivity",  # the agent's battery of runs; re-asking it
        "parameter_sensitivity",  # runs with each calibration parameter varied
        "assign_routes", "optimise_signals",  # route choice saved on the network; a long search
        "build_scenario"}  # a described scenario built and tested in one go
    assert tools.TYPE_TOOLS == {"list_vehicle_types", "create_vehicle_types", "update_vehicle_type"}
    assert tools.OD_TOOLS == {"read_od_matrix", "set_od_demand", "shape_daily_curve", "generate_demand", "scale_demand",
                              "equilibrate_demand",
                              "calibrate_demand", "choose_modes"}
    assert tools.RUN_TOOLS == {"run_simulation", "run_experiment", "read_results", "compare_runs", "set_extension",
                               "read_extensions", "schedule_emergency", "start_batch", "replay_run",
                               "junction_los", "weaving_los", "metro_report", "diagnose_signal", "check_run_health", "preflight_check",
                               "resilience",
                               "emissions_report", "power_analysis", "rank_scenarios", "test_agent_compliance",
                               "read_decisions", "decision_sensitivity", "parameter_sensitivity", "design_audit",
                               "accessibility_report",
                               "benefit_cost",
                               "analyse_run", "check_calibration", "read_detectors", "analyse_safety", "forecast",
                               "critical_links", "regress", "assign_routes", "optimise_signals", "build_scenario",
                               "institution_mode_split"}
    assert not tools.SCENARIO_TOOLS & (tools.OD_TOOLS | tools.RUN_TOOLS | tools.TYPE_TOOLS | {"generate_grid", "place_map"})
    assert tools.SCENARIO_TOOLS <= set(tools._FUNCTIONS)  # a scenario changes only a copy of the network
    assert "There is no tool" in refused(generate_network(1, 1), "write_file", path="src/x.py", text="")


def test_arguments_are_read_as_the_schema_gives_or_refused_saying_what_the_tool_takes():
    """What small models send: misspelt or plural names, numbers and true/false as text, one value in a list,
    choices in another case, nulls; and what can't be read refused before anything runs."""
    assert tools.normalise("set_end_flows", {"ends": "3W", "Supply_VPH": "50", "demand_vph": None}) ==         {"end": "3W", "supply_vph": 50.0}
    assert tools.normalise("set_end_flows", '{"end": ["3W"], "demand_vph": 7}') == {"end": "3W", "demand_vph": 7.0}
    assert tools.normalise("set_junction_control", {"junction": ["2,2"], "control": "ROUNDABOUT"}) ==         {"junctions": ["2,2"], "control": "roundabout"}  # lists stay as sent: ids like 2,2 hold commas
    assert tools.normalise("run_simulation", {"duration_min": "30", "seed": 4.0}) == {"duration_min": 30.0, "seed": 4}
    # llama3.2 fills every optional argument it means to leave out, real names or not (a live Dhanmondi run): left out
    assert tools.normalise("run_simulation", {"duration_min": "20", "seed": "null", "weather": "null", "incidents": "[]",
                                              "lateral": "false", "measure_from_min": "0"}) == \
        {"duration_min": 20.0, "incidents": [], "lateral": False, "measure_from_min": 0.0}  # a real 0 stays
    assert tools.normalise("generate_demand", {"demand": "hourly", "arrival_pattern": "None", "pairs": " NONE ",
                                               "factor": "None", "total": "n/a"}) == {"demand": "hourly"}
    # a level in a choice of another kind is the degree of saturation it means (llama3.2: demand "peak", then 0 when
    # refused), where the tool takes one and none in range was given; else refused saying where it goes
    assert tools.normalise("generate_demand", {"demand": "peak", "degree_of_saturation": "0", "total": "0"}) == \
        {"degree_of_saturation": 0.85}  # and a total of 0 is none
    assert tools.normalise("network_from_background", {"width_m": 1200, "height_m": 0.0}) == {"width_m": 1200.0}
    assert tools.normalise("generate_demand", {"arrival_pattern": "light"}) == {"degree_of_saturation": 0.4}
    assert tools.normalise("generate_demand", {"degree_of_saturation": 0.0, "demand": "hourly"}) == {"demand": "hourly"}
    assert tools.normalise("generate_demand", {"degree_of_saturation": "busy"}) == {"degree_of_saturation": 0.85}
    assert tools.normalise("generate_demand", {"demand": "heavy", "arrival_pattern": "heavy", "degree_of_saturation": 0.6}
                           ) == {"degree_of_saturation": 0.6}  # the level said again beside the one given: left out
    stray ={"demand": "hourly", "peak_hour_factor": 0.85, "frobnicate": 1}  # another tool's, and no tool's
    assert "set_od_demand's" in tools.drop_strays("generate_demand", stray) and stray == {"demand": "hourly", "frobnicate": 1}
    assert tools.drop_strays("generate_demand", {"Demand": "hourly"}) is None  # its own, misspelt: normalise reads it
    for name, args, why in (("run_simulation", {"duration_min": 20, "weather": "heavy"}, "degree_of_saturation"),
                            ("set_od_demand", {"arrival_pattern": "rush hour"}, "to generate_demand"),
                            ("network_from_background", {"place": "Dhanmondi"}, "place_map's")):  # another tool's
        try:
            tools.normalise(name, args)
        except tools.ToolError as exc:
            assert why in str(exc), exc
        else:
            raise AssertionError(args)
    for args, why in (({"duration_min": "None"}, "needs duration_min"), ({"duration_min": 5, "factor": 2}, "no argument")):
        try:
            tools.normalise("run_simulation", args)
        except tools.ToolError as exc:
            assert why in str(exc), exc
        else:
            raise AssertionError(args)  # a required one sent as nothing, or a real value under no name: refused
    for name, args, message in (
            ("set_end_flows", {"flows": [{"end": "3W"}]}, "has no argument 'flows'. set_end_flows takes end (text, required)"),
            ("set_end_flows", {"end": ["3W", "1E"]}, "takes one value; call set_end_flows once for each"),
            ("set_end_flows", {"supply_vph": 5}, "set_end_flows needs end"),
            ("set_end_flows", {"end": "3W", "supply_vph": "lots"}, "supply_vph must be number"),
            ("set_junction_control", {"junctions": ["2,2"], "control": "maybe"}, "control must be one of"),
            ("run_simulation", {"duration_min": 30, "seed": 1.5}, "seed must be whole number"),
            ("set_end_flows", {"end": "3W", "ends": "1E"}, "got end twice"),
            ("set_end_flows", {"end": "3W", "supply_vph": "nan"}, "supply_vph must be number"),  # once crashed later
            ("set_od_demand", {"pairs": ["1W->1E"], "value": float("inf")}, "value must be number"),
            ("no_such_tool", {}, "There is no tool")):
        try:
            tools.normalise(name, args)
        except tools.ToolError as exc:
            assert message in str(exc), (message, str(exc))
        else:
            raise AssertionError(f"{name} {args} should be refused")


def test_values_inside_a_lists_objects_are_read_too():
    counts = tools.normalise("calibrate_demand", {"counts": [{"road": "1,1-2,1", "vph": "800"}]})["counts"]
    types = tools.normalise("create_vehicle_types", {"types": [{"name": "Tram", "kind": "TRAM", "headway_min": "8"}]})
    assert counts == [{"road": "1,1-2,1", "vph": 800.0}]
    assert types["types"] == [{"name": "Tram", "kind": "tram", "headway_min": 8}]
    assert "demand" not in tools.TOOLS[[t["name"] for t in tools.TOOLS].index("set_od_demand")]["parameters"]["required"]


def test_calibration_that_would_clear_the_matrix_is_refused():
    network = generate_network(2, 2)
    flows = {("1W", "1E"): 100.0, ("2W", "2E"): 100.0}
    routes = tools._routes(network, list(flows))
    counts = [{"road": r, "vph": 0} for r in {r for path in routes.values() for r in path}]  # nothing counted anywhere
    try:
        tools.calibration_plan(network, flows, {"counts": counts})
    except ValueError as exc:
        assert "rounds to 0" in str(exc) and "Nothing was changed" in str(exc)
    else:
        raise AssertionError("counts of 0 everywhere would clear the matrix: refused")


def test_an_edit_that_changes_nothing_is_refused_and_one_that_does_says_what_changed():
    network = generate_network(3, 3)
    from copy import deepcopy

    before = deepcopy(network)
    assert "Nothing would change" in refused(network, "set_junction_control", junctions=["2,2"], control="signalised")
    assert network == before  # refused, untouched
    done = run(network, "set_end_flows", end="1W", supply_vph=300)
    assert done["changed"] == ["1W: supply 0 → 300 veh/h"]
    assert "Nothing would change" in refused(network, "set_end_flows", end="1W", supply_vph=300)  # already so


def test_read_tools_describe_the_network():
    network = generate_network(3, 4)
    summary = run(network, "network_summary")
    assert len(summary["junctions"]["signalised"]) == 12 and summary["roads"] == 17
    assert {end["id"] for end in summary["boundary_ends"]} >= {"1E", "3W", "1N", "4S"}
    assert run(network, "list_roads", node="2,2")["count"] == 4
    run(network, "update_roads", roads=["2,2-3,2"], direction="one-way")
    assert [road["id"] for road in run(network, "list_roads", direction="one-way")["roads"]] == ["2,2-3,2"]
    junction = run(network, "describe", id="2,2")
    assert junction["kind"] == "junction" and len(junction["roads"]) == 4
    assert {"id": "2,2-3,2", "to": "3,2", "traffic": "one-way out", "lanes_per_direction": 2,
            "speed_limit_kmh": 50.0} in junction["roads"]
    assert "signal_settings" in junction and run(network, "describe", id="1E")["kind"] == "boundary end"
    assert "Unknown id" in refused(network, "describe", id="9,9")
    assert run(network, "analyse_network")["valid"] is True


def test_analysis_finds_design_problems():
    network = generate_network(3, 4)
    assert not any(value for key, value in run(network, "analyse_network").items()
                   if key not in ("valid", "separate_parts"))
    run(network, "delete_objects", ids=["1,2-2,2", "2,2-3,2", "2,3-2,2"])  # 2,2 keeps only its road south
    run(network, "update_roads", roads=["2,2-2,1"], direction="reverse")  # one-way 2,1 -> 2,2: a trap
    findings = run(network, "analyse_network")
    assert findings["dead_end_junctions"] == ["2,2"]
    assert findings["junctions_traffic_cannot_leave"] == ["2,2"] and findings["separate_parts"] == 1
    run(network, "add_junction", x=5000, y=5000)
    assert run(network, "analyse_network")["unconnected_junctions"] == ["N1"]


def test_edits_go_through_the_model_and_roll_back_on_failure():
    network = generate_network(3, 4)
    result = run(network, "set_junction_control", junctions='["2,2"]', control="roundabout")  # list sent as text
    assert network.nodes["2,2"].control == "roundabout" and result["designs"]["2,2"]["inscribed_diameter_m"] > 0
    run(network, "update_roads", roads=["1,1-2,1"], direction="reverse", lanes_per_direction=3)
    road = network.roads["1,1-2,1"]
    assert (road.from_node, road.direction, road.lanes_per_direction) == ("2,1", "one-way", 3)
    before = network.as_records()
    assert "lanes_per_direction" in refused(network, "update_roads", roads=["1,1-2,1", "1,2-2,2"],
                                            lanes_per_direction=13)
    assert "speed_limit_kmh must be 5 to 130" in refused(network, "update_roads", roads=["1,2-2,2"], speed_limit_kmh=-5)
    assert "add_junction needs y. add_junction takes x (number, required)" in refused(network, "add_junction", x=1)
    assert network.as_records() == before  # nothing half-applied
    assert "is a boundary end, not a junction" in refused(network, "set_junction_control", junctions=["1E"], control="roundabout")


def test_new_roads_ask_how_to_cross_and_interchanges_build():
    network = generate_network(3, 4)
    message = refused(network, "add_road", from_junction="1,1", to_junction="2,3")
    assert "1,2-2,2" in message and "roundabout" in message
    added = run(network, "add_road", from_junction="1,1", to_junction="2,3", crossings="roundabout")
    assert added["roundabouts_added"] == ["N1"] and network.nodes["N1"].control == "roundabout"
    end = run(network, "add_boundary_end", from_junction="4,1", x=2200, y=1000)
    assert end["added_end"].endswith("E")
    built = run(network, "build_interchange", kind="diamond", junctions=["1,2", "3,2", "2,1", "2,3"],
                freeway=["1,2", "3,2"])
    assert built["built"].startswith("DI") and len(built["new_nodes"]) == 4
    assert "needs 3 junctions" in refused(network, "build_interchange", kind="trumpet", junctions=["1,1"])
    slip = run(network, "add_slip_road", roads=["1,2-1,1", "1W"], single_lane=True)
    assert slip["lanes"] == 1 and network.roads[slip["added"]].single_lane
    assert "kerb-side turn" in refused(network, "add_slip_road", roads=["1,1-2,1", "2,1-3,1"])
    assert "2 roads" in refused(network, "add_slip_road", roads=["1W"])
    made = run(network, "set_junction_control", junctions=["1,1"], control="roundabout")
    assert made["slip_roads_moved_out"] == [slip["added"]]
    run(network, "set_end_flows", end="1E", supply_vph=600)
    assert network.approach_nodes["1E"].supply_vph == 600
    moved = run(network, "move_junctions", junctions=["4,3"], dx=20, dy=-10)
    assert moved["moved"]["4,3"] == [1520.0, -10.0]
    run(network, "generate_grid", horizontal=2, vertical=2, lanes_per_direction=3)
    assert len(network.nodes) == 4 and network.default_lanes_per_direction == 3


def test_source_reading_stays_inside_the_project():
    listing = run(generate_network(1, 1), "read_source")["files"]
    assert "src/traffic_simulator/roundabouts.py" in listing and "ARCHITECTURE.md" in listing
    part = run(generate_network(1, 1), "read_source", path="src/traffic_simulator/roundabouts.py", end_line=3)
    assert part["lines"].startswith("1-3 of") and part["text"].startswith("1: ")
    assert "docs/architecture/simulation.md" in listing
    doc = run(generate_network(1, 1), "read_source", path="docs/architecture/simulation.md", end_line=1)
    assert doc["text"].startswith("1: # simulation.py")
    for path in ("../README.md", ".venv/pyvenv.cfg", "C:/Windows/win.ini"):
        assert "Can't read" in refused(generate_network(1, 1), "read_source", path=path)
    # find: the lines holding every word, so a question reads a few lines instead of whole pages
    found = run(generate_network(1, 1), "read_source", find="Webster MAX_CYCLE_S")  # everywhere: line numbers, not pages
    assert found["hits"] and all("webster" in h["text"].lower() and "max_cycle_s" in h["text"].lower() for h in found["hits"])
    hit = run(generate_network(1, 1), "read_source", path="src/traffic_simulator/signal_timing.py",
              find="MAX_CYCLE_S =")["hits"][0]
    page = run(generate_network(1, 1), "read_source", path=hit["path"], start_line=hit["line"], end_line=hit["line"])
    assert "MAX_CYCLE_S" in page["text"]
    one = run(generate_network(1, 1), "read_source", path="docs/architecture/sources.md", find="roundabout")
    assert one["hits"] and {h["path"] for h in one["hits"]} == {"docs/architecture/sources.md"}
    assert "fewer" in run(generate_network(1, 1), "read_source", find="zz" + "qx nothing")["next"]  # built here, so this line has no match
    assert "Can't read" in refused(generate_network(1, 1), "read_source", path="../README.md", find="a")
    long = run(generate_network(1, 1), "read_source", path="docs/architecture/sources.md")
    end = int(long["lines"].split("-")[1].split()[0])  # a page is whole lines up to READ_CHARS (sources.md's are long)
    assert len(long["text"]) <= tools.READ_CHARS and long["next_start_line"] == end + 1 and end < tools.READ_LINES
    on = run(generate_network(1, 1), "read_source", path="docs/architecture/sources.md", start_line=end + 1)
    assert on["text"].startswith(f"{end + 1}: ")


def test_changes_are_described_for_the_assistant():
    from copy import deepcopy

    network = generate_network(3, 4)
    before = deepcopy(network)
    assert tools.describe_changes(before, network) == []
    run(network, "set_junction_control", junctions=["2,2"], control="roundabout")
    run(network, "update_roads", roads=["1,1-2,1"], lanes_per_direction=3, direction="reverse")
    run(network, "move_junctions", junctions=["4,3"], dx=20, dy=0)
    run(network, "add_junction", x=900, y=900)
    run(network, "delete_objects", ids=["3,1-4,1"])
    run(network, "set_end_flows", end="1E", supply_vph=600)
    changes = tools.describe_changes(before, network)
    assert "2,2: signalised → roundabout" in changes
    assert "Road 1,1-2,1: lanes/direction 2 → 3, direction two-way → one-way, now runs 2,1 → 1,1" in changes
    assert "Moved 4,3 by (+20, +0) m" in changes and "Added junction N1" in changes
    assert "Deleted road 3,1-4,1" in changes and "1E: supply 0 → 600 veh/h" in changes
    assert not any("length" in line for line in changes)  # moved ends explain their roads' lengths
    assert tools.describe_changes(before, generate_network(2, 2))[0].startswith("Generated a new network")
    assert tools.describe_changes(before, network, limit=2)[-1] == f"...and {len(changes) - 2} more changes"


def _flows(network, supply: dict, demand: dict):
    for end, value in supply.items():
        network.approach_nodes[end].supply_vph = value
    for end, value in demand.items():
        network.approach_nodes[end].demand_vph = value


def test_a_gravity_model_generates_demand_balanced_to_the_ends():
    network = generate_network(2, 2)  # ends 1W, 2W, 1E, 2E, 1N, 2N, 1S, 2S, all two-way
    ends = sorted(network.approach_nodes)
    pairs = [(o, d) for o in ends for d in ends if o != d]
    try:
        tools.demand_plan(network, pairs, {"demand": "hourly"})
    except ValueError as exc:
        assert "set_end_flows" in str(exc)  # nothing to balance to: says what to give
    else:
        raise AssertionError("no totals should be refused")
    _flows(network, {"1W": 600, "2W": 300, "1N": 300}, {"1E": 400, "2E": 400, "1S": 400})
    plan, info = tools.demand_plan(network, pairs, {"demand": "hourly"})
    rows, columns = {}, {}
    for (o, d), value in plan.items():
        rows[o] = rows.get(o, 0) + value
        columns[d] = columns.get(d, 0) + value
    assert set(rows) == {"1W", "2W", "1N"} and set(columns) == {"1E", "2E", "1S"}  # only ends with flows
    for end, want in {"1W": 600, "2W": 300, "1N": 300}.items():
        assert abs(rows[end] - want) <= 3, (end, rows[end])  # balanced to each origin's supply...
    for end in ("1E", "2E", "1S"):
        assert abs(columns[end] - 400) <= 3, (end, columns[end])  # ...and each destination's demand
    assert plan[("1W", "1E")] > plan[("1W", "2E")]  # 1E is straight along 1W's row, 2E a block further
    assert info["total_asked"] == 1200 and abs(info["total_made"] - 1200) <= 5 and "Furness" in info["method"]
    flat, flat_info = tools.demand_plan(network, pairs, {"demand": "hourly", "distance_decay_per_km": 0})
    assert abs(flat[("1W", "1E")] - flat[("1W", "2E")]) <= 1  # distance ignored: the same share
    steep = tools.demand_plan(network, pairs, {"demand": "hourly", "distance_decay_per_km": 3})[1]["mean_trip_km"]
    target = (steep + flat_info["mean_trip_km"]) / 2  # Hyman: the decay fitted to a mean trip length
    fitted, info = tools.demand_plan(network, pairs, {"demand": "hourly", "mean_trip_km": target})
    assert abs(info["mean_trip_km"] - target) < 0.02 and 0 < info["distance_decay_per_km"] < 3 and "Hyman" in info["method"]
    assert "Hyman" not in tools.demand_plan(network, pairs, {"demand": "hourly", "mean_trip_km": 0})[1]["method"]  # filler
    for args, message in (({"demand": "hourly", "mean_trip_km": 99}, "mean_trip_km must be"),
                          ({"demand": "hourly", "mean_trip_km": target, "distance_decay_per_km": 1}, "not both")):
        try:
            tools.demand_plan(network, pairs, args)
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError(args)
    daily, info = tools.demand_plan(network, pairs, {"demand": "daily"})
    assert info["total_asked"] == 1200 * 24 and "× 24" in info["note"]
    _flows(network, dict.fromkeys(ends, 0.0), dict.fromkeys(ends, 0.0))
    equal, info = tools.demand_plan(network, pairs, {"demand": "hourly", "total": 560, "distance_decay_per_km": 0})
    assert len(equal) == 56 and set(equal.values()) == {10}  # a total shared equally: 560 over 56 pairs
    network.approach_roads["1E"].direction = "one-way"  # 1E now only leads out of the network (inner -> end)
    network.reverse_road("1E")  # ...and now only in: nothing can reach it
    plan, info = tools.demand_plan(network, pairs, {"demand": "hourly", "total": 560})
    assert not any(d == "1E" for _, d in plan) and "2W->1E" in info["no_route"]
    _flows(network, dict.fromkeys(ends, 0.0), dict.fromkeys(ends, 0.0))
    _flows(network, {"2W": 50}, {"2W": 50})  # flows at one end only: every trip would start and end there
    for args, message in (({"demand": "hourly", "total": 100}, "trips leave only 2W"),
                          ({"demand": "hourly"}, "trips leave only 2W"),
                          ({"demand": "hourly", "mean_trip_km": 1}, "no trip can be made")):
        try:
            tools.demand_plan(network, pairs, args)
        except ValueError as exc:
            assert message in str(exc) and "Nothing was changed" in str(exc), str(exc)
        else:
            raise AssertionError("a plan with no demand should be refused, not applied as an empty matrix")
    _flows(network, dict.fromkeys(ends, 0.0), dict.fromkeys(ends, 0.0))
    try:
        tools.demand_plan(network, pairs, {"demand": "hourly", "total": 10})  # under 1 a pair
    except ValueError as exc:
        assert "rounds to 0" in str(exc)
    else:
        raise AssertionError("a total under 1 a pair should be refused")
    plan, info = tools.demand_plan(network, pairs, {"demand": "hourly", "total": 30})
    assert plan and info["pairs_rounded_to_0"] > 0  # the pairs a small total leaves at 0 are said, not hidden


def _x_alone(test):
    """Run `test` with demand sized by the junctions' X alone (no queue storage bound: its own test is in
    test_experiments)."""
    def run():
        kept, tools.STORAGE_LIMIT = tools.STORAGE_LIMIT, 1e9
        try:
            test()
        finally:
            tools.STORAGE_LIMIT = kept
    run.__name__, run.__doc__ = test.__name__, test.__doc__
    return run


@_x_alone
def test_demand_is_sized_to_a_degree_of_saturation():
    network = generate_network(3, 3)  # signalised junctions
    ends = sorted(network.approach_nodes)
    pairs = [(o, d) for o in ends for d in ends if o != d]
    busy, info = tools.demand_plan(network, pairs, {"demand": "hourly", "degree_of_saturation": 0.8})
    assert abs(max(info["degree_of_saturation"].values()) - 0.8) <= 0.02 and info["degree_of_saturation_target"] == 0.8
    near, info = tools.demand_plan(network, pairs, {"demand": "hourly", "degree_of_saturation": 0.95})
    assert sum(near.values()) > sum(busy.values())  # more saturated: more demand
    _, info = tools.demand_plan(network, pairs, {"demand": "hourly", "total": 500})
    assert max(info["degree_of_saturation"].values()) < 0.8  # a total reports what it does to the signals too
    peak = [0.02] * 7 + [0.12, 0.1] + [0.64 / 15] * 15  # 07:00 the busiest hour
    daily, info = tools.demand_plan(network, pairs, {"demand": "daily", "degree_of_saturation": 0.8},
                                    shares=dict.fromkeys(pairs, peak))
    assert info["design_hour"] == "07:00-08:00" and abs(max(info["degree_of_saturation"].values()) - 0.8) <= 0.02
    assert abs(sum(daily.values()) * 0.12 - sum(busy.values())) <= 0.02 * sum(busy.values())  # its peak hour = the hourly
    for args, message in (({"demand": "hourly", "degree_of_saturation": 0.8, "total": 100}, "not both"),
                          ({"demand": "hourly", "degree_of_saturation": 3}, "0.1 to 1.2")):
        try:
            tools.demand_plan(network, pairs, args)
        except ValueError as exc:
            assert message in str(exc), (message, str(exc))
        else:
            raise AssertionError(f"{args} should have been refused")
    for control in ("roundabout", "uncontrolled"):  # no signals: their entries' capacity sizes it
        for node in network.nodes.values():
            node.control = control
        _, info = tools.demand_plan(network, pairs, {"demand": "hourly", "degree_of_saturation": 0.8})
        assert abs(max(info["degree_of_saturation"].values()) - 0.8) <= 0.02
        assert set(info["junction_controls"].values()) == {control}


@_x_alone
def test_demand_already_entered_is_scaled():
    network = generate_network(3, 3)
    values = {("1W", "3E"): 400, ("1N", "1S"): 300, ("2W", "2E"): 200}
    pairs = [("1W", "3E"), ("1N", "1S")]
    plan, info = tools.scale_plan(network, values, pairs, {"demand": "hourly", "factor": 1.5})
    assert plan == {("1W", "3E"): 600, ("1N", "1S"): 450} and info["total_before"] == 700 and info["total_after"] == 1050
    plan, info = tools.scale_plan(network, values, pairs, {"demand": "hourly", "degree_of_saturation": 0.8})
    assert abs(plan[("1W", "3E")] / plan[("1N", "1S")] - 4 / 3) < 0.01 and abs(max(info["degree_of_saturation"].values()) - 0.8) <= 0.02
    assert ("2W", "2E") not in plan  # out of scope: kept, though it still loads the junctions
    for args, message in (({"demand": "hourly"}, "one of them"), ({"demand": "hourly", "factor": 2, "degree_of_saturation": 0.8}, "one of them"),
                          ({"demand": "hourly", "factor": 0}, "0.01 to 100")):
        try:
            tools.scale_plan(network, values, pairs, args)
        except ValueError as exc:
            assert message in str(exc), (message, str(exc))
        else:
            raise AssertionError(f"{args} should have been refused")
    try:
        tools.scale_plan(network, values, [("3E", "1W")], {"demand": "hourly", "factor": 2})
    except ValueError as exc:
        assert "no hourly demand" in str(exc)
    else:
        raise AssertionError("nothing to scale should be refused")


def test_institutions_are_deployed_at_random_on_clear_ground():
    from traffic_simulator.institutions import footprint_problem, links

    network = generate_network(3, 4)
    assert "kinds must be among" in refused(network, "deploy_institutions", count=2, kinds=["Castle"])
    assert "1 to 50" in refused(network, "deploy_institutions", count=0)
    result = run(network, "deploy_institutions", count=6, seed=7)
    assert len(network.institutions) == 6 and result["seed"] == 7
    for item in network.institutions.values():
        others = {key: other for key, other in network.institutions.items() if key != item.id}
        alone = generate_network(3, 4)
        alone.institutions = others
        assert footprint_problem(alone, item.x, item.y, item.area_m2) is None  # off roads, junctions, each other
        assert links(network, item.x, item.y, item.radius_m)  # its circle reaches a junction or end
    again = generate_network(3, 4)
    run(again, "deploy_institutions", count=6, seed=7)
    assert again.institutions == network.institutions  # the same seed, the same layout
    schools = generate_network(3, 4)
    run(schools, "deploy_institutions", count=3, kinds=["school"], seed=1)
    assert {item.kind for item in schools.institutions.values()} == {"School"}
    listed = run(network, "list_institutions")
    assert listed["count"] == 6 and all(row["door"] and len(row["catchment"]) >= 3 for row in listed["institutions"])
    small = generate_network(1, 1)  # one junction and its four ends: room for a few hospitals, not 50
    run(small, "deploy_institutions", count=2, kinds=["Hospital"], seed=3)
    before = dict(small.institutions)
    assert "found clear ground" in refused(small, "deploy_institutions", count=50, kinds=["Hospital"], seed=3)
    assert small.institutions == before  # all or nothing
    lines = tools.describe_changes(generate_network(3, 4), network)
    assert any(line.startswith("Placed institution IN1") for line in lines)
    run(network, "delete_objects", ids=["IN1"])
    assert "IN1" not in network.institutions and run(network, "network_summary")["institutions"] == 5
    args = {"count": 2}
    tools.prepare(network, "deploy_institutions", args)
    assert isinstance(args["seed"], int)  # fixed before the check, so the change approved is the one made
    assert "at random" in tools.describe_call("deploy_institutions", {"count": 2})


def test_each_request_is_offered_the_tools_its_words_point_to():
    def offered(request, used=()):
        return {spec["name"] for spec in tools.tools_for(request, used)}

    # the requests guided mode got wrong (Research Log, 2026-10-04)
    city = offered("create a nice city replacing the existing grid. Keep some roads, demand should be made with the "
                   "institution block. The city should be around 2000m x 1500m")
    assert {"generate_grid", "deploy_institutions", "delete_objects", "generate_demand"} <= city
    assert not {"place_map", "network_from_background"} & city  # a made-up city: not the map's
    busy = offered("Create a demand for busy city in hourly basis")
    assert "generate_demand" in busy and "add_bus_stop" not in busy and "place_map" not in busy  # busy: not a bus
    assert {"place_map", "network_from_background"} <= offered("Build Dhanmondi in Dhaka and give it heavy traffic")
    assert {"diagnose_signal", "junction_los", "run_simulation"} <= offered("why is junction 2,2 so slow?")
    assert "run_experiment" in offered("does a third lane on 2,2-3,2 cut delay?")
    chat = offered("hi")
    assert chat == set(tools.CORE_TOOLS) and len(chat) < len(tools.TOOLS) // 5
    assert "schedule_emergency" in offered("hi", used={"schedule_emergency"})  # a follow-up keeps them
    assert "optimise_signals" in offered("run optimise_signals")  # a tool named outright
    have = tools.tools_for("hi")
    found = tools.expand_tools({"need": "bus stops"}, have)
    assert {row["name"] for row in found["added"]} >= {"add_bus_stop", "update_bus_stop"}
    assert {"add_bus_stop", "update_bus_stop"} <= {spec["name"] for spec in have}  # now the model can call them
    assert "Nothing new" in tools.expand_tools({"need": "bus stops"}, have)["note"]
    assert "error" in tools.expand_tools({}, have)
    # fewer, smaller specs (the 2026-10-05 stress test): the whole-study tool only for a request with several parts,
    # the demand models only when their words are used, the signal tools not for a junction alone
    assert "build_scenario" in offered("make a 6 by 5 grid, signals everywhere, then run it with busy traffic")
    assert "build_scenario" not in offered("widen road 2,2-3,2 and delete junction 1,1")
    assert not {"set_signal_timing", "optimise_signals"} & offered("delete junction 1,1")
    assert "choose_modes" not in offered("give it busy traffic") and "choose_modes" in offered("choose a mode split")
    assert {"update_institution", "scale_demand"} <= offered("increase the population")
    assert "read_source" in offered("how do I save my API key?")
    assert "generate_demand" in offered("genrate demnd for the netwrk at 0.8 degree of saturtion")


def test_a_grid_of_impossible_blocks_is_refused():
    network = generate_network(4, 4)
    for spacing in (3, 50000):  # the stress test's "50000 x 50000 m with 3 m blocks"
        assert "spacing_m must be 20-2000 m" in refused(network, "generate_grid", horizontal=3, vertical=3,
                                                        spacing_m=spacing)
    assert len(network.nodes) == 16  # left as it was
    assert run(network, "generate_grid", horizontal=3, vertical=3, spacing_m=250)["junctions"] == 9


def test_an_invented_tool_is_refused_with_the_nearest_real_ones():
    try:
        tools.normalise("teleport_vehicles", {})
    except tools.ToolError as exc:
        assert "can't do that" in str(exc) and "find_tools" in str(exc)
    else:
        raise AssertionError("an invented tool must be refused")
    try:
        tools.normalise("list_junction", {})
    except tools.ToolError as exc:
        assert "Named alike: list_junctions" in str(exc) and "can't do that" in str(exc)  # alike isn't the same
    else:
        raise AssertionError("a misspelt tool must be refused")


def test_a_signal_method_other_than_the_one_asked_for_is_caught():
    # qwen3.5 4B, asked for SCOOT, thought "SCOOT" and sent scats
    said = tools.contradicts("set_signal_timing", {"junctions": ["2,2"], "method": "scats"}, "switch every signal to SCOOT")
    assert said and "scoot" in said and "'scats'" in said
    assert tools.contradicts("set_signal_timing", {"method": "scoot"}, "switch every signal to SCOOT") is None
    assert tools.contradicts("set_signal_timing", {"method": "webster"}, "make the signals better") is None  # none named
    assert tools.contradicts("set_signal_timing", {"method": "webster"}, "from scoot back to webster") is None
    assert tools.contradicts("set_signal_timing", {"method": "max_pressure"}, "use max pressure control") is None
    assert tools.contradicts("generate_grid", {"horizontal": 2, "vertical": 2, "signal_method": "fixed"},
                             "a grid with actuated signals")
    assert tools.contradicts("add_bus_stop", {"road": "1,1-2,1", "direction": "forward"}, "on both sides") is None


def test_an_invented_grid_for_a_named_place_is_caught():
    grid = {"horizontal": 6, "vertical": 5}  # llama3.2 3B, asked for Dhanmondi's roads, placed the map then built this
    asked = "Build the road network of Dhanmondi, Dhaka, Bangladesh by selecting it from the map."
    assert "network_from_background" in tools.contradicts("generate_grid", grid, asked)
    for fine in ("Build me a 3x2 grid, run it 5 minutes", "Make a grid like the streets of Dhanmondi, Dhaka",
                 "Create a new network with 4 columns and 3 rows"):  # a grid asked for, or no place named
        assert tools.contradicts("generate_grid", grid, fine) is None, fine
    assert tools.contradicts("network_from_background", {}, asked) is None


def test_an_incident_given_beside_a_runs_arguments_is_that_incident():
    from traffic_simulator import experiments

    assert tools.INCIDENT_FIELDS == experiments.INCIDENT_KEYS  # the same fields, kept in two modules
    args = {"duration_min": 30, "road": "2,2-3,2", "lanes": 1, "at_min": 5, "for_min": 10}  # llama3.2 3B
    assert "one incident" in tools.fold_incident("run_simulation", args)
    assert args == {"duration_min": 30, "incidents": [{"road": "2,2-3,2", "lanes": 1, "at_min": 5, "for_min": 10}]}
    given = {"duration_min": 30, "incidents": [{"road": "1,1-2,1"}], "lanes": 1}
    assert tools.fold_incident("run_simulation", given) is None and "lanes" in given  # incidents given: left alone


def test_ids_are_read_however_a_small_model_lists_them():
    network = generate_network(4, 4)
    tools.run_tool(network, "update_roads", {"roads": "all", "speed_limit_kmh": 40})
    assert all(road.speed_limit_kmh == 40 for road in network.all_roads())  # "all": every road
    done = tools.run_tool(network, "set_junction_control", {"junctions": "2,2; 3,3", "control": "roundabout"})
    assert done["junctions"] == ["2,2", "3,3"] and "2,2: signalised → roundabout" in done["changed"]
    assert tools.run_tool(network, "delete_objects", {"ids": "['4,4-4,3']"})["deleted"] == ["4,4-4,3"]  # Python's list
    timed = tools.run_tool(network, "set_signal_timing", {"junctions": "all", "method": "fixed", "green_times_s": [30, 20]})
    assert "2,2" not in timed["timed"] and "1,1" in timed["timed"]  # "all" signals: the roundabouts aren't
    for name, args, why in (("set_junction_control", {"junctions": ["9,9"], "control": "roundabout"}, "Unknown id '9,9'"),
                            ("add_road", {"from_junction": "1W", "to_junction": "2,2"}, "is a boundary end, not a junction"),
                            ("set_end_flows", {"end": "2,3", "supply_vph": 100}, "is a junction, not a boundary end"),
                            ("set_signal_timing", {"junctions": ["2,2", "3,3"]}, "2,2, 3,3 are not signalised"),
                            ("set_signal_timing", {"junctions": ["1,1"], "method": "webster", "green_times_s": [30, 20]},
                             "only used by method fixed"),
                            ("set_signal_timing", {"junctions": ["1,1"], "phases": 4, "green_times_s": [30, 20]},
                             "one green a phase"),
                            ("update_institution", {"id": "IN9"}, "Unknown id 'IN9'")):
        try:
            tools.run_tool(network, name, args)
        except tools.ToolError as exc:
            assert why in str(exc), (name, exc)
        else:
            raise AssertionError((name, args))
    widened = tools.run_tool(network, "update_roads", {"roads": ["1,2", "2,2"], "lanes_per_direction": 3})
    assert widened["read_as"] == {"1,2, 2,2": "1,2-2,2"} and network.road("1,2-2,2").lanes_per_direction == 3
    asked = "Delete road 1,3-2,3"  # llama3.2 3B deleted its two junctions (and every road at them) instead
    assert "Call again with ids [\"1,3-2,3\"]" in tools.contradicts("delete_objects", {"ids": ["1,3", "2,3"]}, asked)
    assert tools.contradicts("delete_objects", {"ids": ["1,3", "2,3"]}, "Delete junctions 1,3 and 2,3") is None
    greens = tools.run_tool(network, "set_signal_timing", {"junctions": ["1,1"], "green_times_s": [25, 35]})
    assert greens["timed"]["1,1"]["method"] == "fixed"  # greens given alone mean a fixed plan
    assert tools.run_tool(network, "generate_grid", {"horizontal": 5, "vertical": 6})["generated"].startswith(
        "6 x 5 grid (6 columns, 5 rows)")


def test_ends_copied_from_an_example_mean_every_end():
    network = generate_network(2, 2)  # ends 1W, 1E, 1N...; 99W is none of them
    asked = "Set the traffic demand for the peak hour."  # llama3.2 3B sent origins ["1W"] on a network without one
    args = {"origins": ["99W"], "degree_of_saturation": 0.85}
    note = tools.repair_ends(network, "generate_demand", args, asked)
    assert "every end" in note and args == {"degree_of_saturation": 0.85}
    assert tools.repair_ends(network, "generate_demand", {"origins": "None", "pairs": "[]"}, asked) is None  # left out, no end
    for name, kept, request in (("generate_demand", {"origins": ["99W"]}, "demand from 99W please"),  # the user named it
                                ("set_od_demand", {"pairs": ["1W->99W"]}, asked),  # one real end: the tool checks it
                                ("run_simulation", {"origins": ["99W"]}, asked)):  # a tool without ends
        before = dict(kept)
        assert tools.repair_ends(network, name, kept, request) is None and kept == before, name


def test_a_road_named_from_its_other_end_is_that_road():
    network = generate_network(4, 4)
    assert "2,3-2,2" in network.roads and "2,2-2,3" not in network.roads
    done = tools.run_tool(network, "update_roads", {"roads": ["2,2-2,3"], "lanes_per_direction": 3})
    assert done["read_as"] == {"2,2-2,3": "2,3-2,2"} and network.roads["2,3-2,2"].lanes_per_direction == 3
    assert tools.explain_error("Unknown id '2,2-2,3'.", network)["did_you_mean"][0] == "2,3-2,2"
    # "add a lane" as people say it (qwen3.5 4B set lanes_per_direction 2, the count the road already had)
    tools.run_tool(network, "update_roads", {"roads": ["2,3-2,2", "1,1-2,1"], "add_lanes": 1})
    assert network.roads["2,3-2,2"].lanes_per_direction == 4 and network.roads["1,1-2,1"].lanes_per_direction == 3
    for bad in ({"add_lanes": -9}, {"add_lanes": 1, "lanes_per_direction": 2}):
        try:
            tools.run_tool(network, "update_roads", {"roads": ["1,1-2,1"], **bad})
        except tools.ToolError:
            assert network.roads["1,1-2,1"].lanes_per_direction == 3
        else:
            raise AssertionError(bad)
    try:  # per-road objects where ids go (qwen3.5 4B did), said plainly; nothing changed
        tools.run_tool(network, "update_roads", {"roads": [{"road": "2,1-3,1", "speed_limit_kmh": 30}]})
    except tools.ToolError as exc:
        assert "not objects" in str(exc) and network.roads["2,1-3,1"].speed_limit_kmh == 50
    else:
        raise AssertionError("objects aren't ids")
    try:
        tools.run_tool(network, "describe", {"id": "9,9-9,8"})
    except tools.ToolError as exc:
        assert "Unknown id" in str(exc)
    else:
        raise AssertionError("no such road either way")


def test_lists_sent_as_broken_json_text_are_repaired_or_refused_precisely():
    # qwen3.5 4B sent run_experiment's scenarios as text one } short, then retried blind 7 times on a vague error
    short = '[{"changes":[],"name":"as built"},{"changes":[{"tool":"set_junction_control","args":{"junctions":["2,2"]}}]'
    found = tools.normalise("run_experiment", {"scenarios": short, "seeds": [1], "duration_min": 5})
    assert found["scenarios"][1]["changes"][0]["tool"] == "set_junction_control"
    assert tools.normalise("run_experiment", {"scenarios": '[{"name": "a"},]', "seeds": [1],
                                              "duration_min": 5})["scenarios"] == [{"name": "a"}]
    try:
        tools.normalise("run_experiment", {"scenarios": '[{"name" "a"}]', "seeds": [1], "duration_min": 5})
    except tools.ToolError as exc:
        assert "isn't valid JSON" in str(exc) and "character 9" in str(exc) and "not a string" in str(exc)
    else:
        raise AssertionError("unreadable JSON is refused, saying where")
    assert tools.normalise("describe", {"id": "2,2"}) == {"id": "2,2"}  # plain text stays text
    # a choice misspelt one clear way is that choice (qwen3.5 4B wrote "scot"); an unclear one is refused
    assert tools.normalise("set_signal_timing", {"junctions": ["2,2"], "method": "scot"})["method"] == "scoot"
    try:
        tools.normalise("set_signal_timing", {"junctions": ["2,2"], "method": "s"})
    except tools.ToolError as exc:
        assert "must be one of" in str(exc)
    else:
        raise AssertionError("an unclear choice is refused")


def test_update_roads_sets_and_removes_a_turn_pocket():
    network = generate_network(3, 4)
    result = run(network, "update_roads", roads=["1,2-2,2"], pocket_junction="2,2", pocket_side="offside",
                 pocket_length_m=60)
    assert result["updated"][0]["turn_pockets"] == {"2,2": {"lanes": 1, "length_m": 60.0, "side": "offside"}}
    assert any("turn pockets" in line for line in result["changed"])
    assert "turn pocket at → 2,2" in tools.describe_call("update_roads", {"roads": ["1,2-2,2"], "pocket_junction": "2,2"})
    before = network.roads["1,2-2,2"].pockets
    assert "15 to 150 m" in refused(network, "update_roads", roads=["1,2-2,2"], pocket_junction="2,2", pocket_length_m=500)
    assert "not at 1W" in refused(network, "update_roads", roads=["1W"], pocket_junction="1W")
    assert "pocket_junction" in refused(network, "update_roads", roads=["1,2-2,2"], pocket_lanes=2)
    assert network.roads["1,2-2,2"].pockets == before  # refused: as it was
    run(network, "update_roads", roads=["1,2-2,2"], pocket_junction="1,2", pocket_lanes=2)  # a kerb pocket, 50 m
    run(network, "update_roads", roads=["1,2-2,2"], direction="one-way")  # its traffic leaves 1,2 now: that one goes
    assert network.roads["1,2-2,2"].pockets == before
    run(network, "update_roads", roads=["1,2-2,2"], pocket_junction="2,2", pocket_lanes=0)
    assert network.roads["1,2-2,2"].pockets == {}


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
