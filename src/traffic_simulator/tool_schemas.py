"""What every assistant tool takes and says, as the model sees it (no Qt).

The TOOLS JSON-schema list, the name sets built from it and tool_specs, apart from the tools themselves, so the
one list the model sees can be edited without touching them.
"""

from __future__ import annotations

from . import scenario
from .daily_profile import ARRIVAL_PATTERNS
from .institutions import KINDS
from .models import BUS_STOP_DIRECTIONS, CROSSING_KINDS, METRO_MIN_SPACING_M, SIGNAL_METHODS
from .vehicle import VEHICLE_CLASSES

CONTROLS = ("signalised", "roundabout", "uncontrolled")


# --- Tool definitions (JSON Schema parameters) -----------------------------------------

def _object(properties: dict | None = None, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties or {}, "required": list(required)}


_IDS = {"type": "array", "items": {"type": "string"}}
_PAIRS = {  # which OD pairs a demand tool works on
    "pairs": {"type": "array", "items": {"type": "string"},
              "description": "OD pairs as origin->destination boundary ends, e.g. [\"1W->1E\", \"2N->3S\"]."},
    "origins": {"type": "array", "items": {"type": "string"},
                "description": "Instead of pairs: end names like \"1W\" (not pairs); every pair from "
                               "them, to all destinations unless destinations is given too."},
    "destinations": {"type": "array", "items": {"type": "string"},
                     "description": "Instead of pairs: end names like \"1E\" (not pairs); every pair to them."},
}
# The kind of demand and how arrivals are spread: never how busy (llama3.2 sent demand "peak", arrival_pattern "heavy")
_DEMAND_KIND = {"type": "string", "enum": ["hourly", "daily"],
                "description": "The kind: hourly (veh/h) or daily (trips/day), not how busy (that's degree_of_saturation)."}
_ARRIVALS = {"type": "string", "enum": ARRIVAL_PATTERNS,
             "description": "How arrivals are spread in time, not how busy (that's degree_of_saturation)."}
_SATURATION = ("Instead of a total or factor (the way to say how busy: light 0.4, medium 0.65, busy, peak or rush hour "
               "0.85): size the demand so the most loaded junction (signalised, roundabout "
               "or priority) runs at this flow/capacity, e.g. 0.8 (busy), 0.95 (near capacity); 0.1-1.2. The "
               "capacities are measured on the simulated drivers; the demand is then run headless for 45 simulated "
               "minutes (a few minutes; more if it must be backed off) and, if vehicles pile up, cut to the largest "
               "share that holds (checked_in_a_run says).")
_CROSSINGS = {"type": "string", "enum": ["roundabout", "overpass"],
              "description": "Only if the new road crosses existing roads: a roundabout junction at each "
                             "crossing (both roads split there) or an overpass bridging over them."}

_SCENARIO = {"type": "object", "properties": {
    "name": {"type": "string"},
    "changes": {"type": "array", "description": "Edit tools applied to a copy of the network, in order, e.g. "
                "[{\"tool\": \"update_roads\", \"args\": {\"roads\": [\"1,1-2,1\"], \"lanes_per_direction\": 3}}].",
                "items": {"type": "object", "required": ["tool"], "properties": {
                    "tool": {"type": "string"}, "args": {"type": "object"}}}},
    "demand_scale": {"type": "number", "description": "Every pair's demand × this (0.05-10), e.g. 1.2 for 20 % growth."},
    "extensions": {"type": "array", "items": {"type": "string"}, "description": "Extensions ticked in it, e.g. "
                   "[\"Transit Signal Priority (TSP)\"]."},
    "incidents": {"type": "array", "description": "Lanes closed for a while (an incident, a work zone): "
                  "[{\"road\": \"1,1-2,1\", \"lanes\": 1 (kerbside first; all of them blocks the road), \"at_min\": 5, "
                  "\"for_min\": 15, \"length_m\": 50, \"direction\": \"both\" | \"forward\" | \"back\", "
                  "\"speed_kmh\": 30 (a work zone's limit)}].", "items": {"type": "object"}},
    "weather": {"type": "string", "enum": ["clear", "light rain", "light snow", "heavy rain", "heavy snow"],
                "description": "Lower speeds and capacity (HCM 2000 Ch 22): heavy rain about 14 % less, heavy snow 30 %."},
    "random_incidents": {"type": "object", "description": "{\"per_hour\": 2, \"for_min\": 15, \"lanes\": 1}: incidents "
                         "drawn for each seed (the same on a seed for every scenario)."},
    "assign_routes": {"type": "object", "description": "{\"iterations\": 4, \"until_gap\": 0.02}: route choice found "
                      "for this scenario first (successive averages toward equilibrium), so a design is judged after "
                      "drivers re-route (and a new road can make things worse: Braess)."},
    "meters": {"type": "array", "items": {"type": "object"}, "description": "Ramp meters on on-ramps (a road ending "
               "at a merge): [{\"road\": id, \"target_occupancy_percent\": 20}] (ALINEA: the rate follows the main "
               "road's occupancy past the merge) or {\"road\": id, \"rate_vph\": 600} (fixed), 240-1800 veh/h."},
    "lateral": {"type": "boolean", "description": "Lateral position (sublane): motorcycles, mopeds and bicycles "
                "share lanes and filter past queues to the stop line (weak lane discipline). Default false."}},
             "required": ["name"]}
_CONDITIONS = {key: _SCENARIO["properties"][key] for key in ("incidents", "weather", "meters", "lateral")}
_MEASURE_FROM = {"measure_from_min": {"type": "number", "description": "Warm-up left out of the figures: measure from "
                                      "this minute (default 0; analyse_run's MSER-5 warm-up suggests one)."}}
FOOTPATH_SIDES = {"both": ("left", "right"), "left": ("left",), "right": ("right",), "none": ()}  # update_roads
CYCLE_LANE_SIDES = ("both", "forward", "backward", "none")  # update_roads: forward rides from -> to, to the to-node

TOOLS = [
    # Read: run straight away.
    {"name": "network_summary", "edits": False, "parameters": _object(),
     "description": "Overview of the open network: defaults, junctions by control type, road counts, "
                    "boundary ends with their set_end_flows weights (generate_demand's inputs, not the traffic a Run "
                    "sends: read_od_matrix has that), and the extent in metres. Call this first."},
    {"name": "list_roads", "edits": False,
     "description": "Roads with their settings. 'one-way' traffic runs from -> to. Filter on big networks.",
     "parameters": _object({"node": {"type": "string", "description": "Only roads touching this junction or end."},
                            "direction": {"type": "string", "enum": ["one-way", "two-way"]}})},
    {"name": "list_junctions", "edits": False,
     "description": "Junctions with position (metres), control type and number of roads.",
     "parameters": _object({"control": {"type": "string", "enum": list(CONTROLS)}})},
    {"name": "describe", "edits": False,
     "description": "Everything about one junction, boundary end or road: its roads, roundabout design "
                    "(NCHRP 672), signal settings, curve radius and safe curve speed, crossings.",
     "parameters": _object({"id": {"type": "string"}}, ("id",))},
    {"name": "analyse_network", "edits": False, "parameters": _object(),
     "description": "Design check: validity, disconnected parts, junctions traffic can't reach or leave "
                    "(one-way roads considered), dead ends, very short roads, curves posted faster than "
                    "their radius allows, roundabouts too big for their roads."},
    {"name": "read_source", "edits": False,
     "description": "Read the app's own source, tests or docs. Empty path lists the readable files. Use find "
                    "first (words that must all be on a line; in one file, or every readable file when path is "
                    "empty): it gives line numbers, then read just those lines with start_line and end_line.",
     "parameters": _object({"path": {"type": "string", "description": "e.g. src/traffic_simulator/roundabouts.py"},
                            "find": {"type": "string", "description": "e.g. 'buffer index'"},
                            "start_line": {"type": "integer"}, "end_line": {"type": "integer"}})},
    {"name": "get_selection", "edits": False, "parameters": _object(),
     "description": "What the user has selected on the map right now."},
    {"name": "show_on_map", "edits": False,
     "description": "Select a junction, end or road on the map and centre the view on it.",
     "parameters": _object({"id": {"type": "string"}}, ("id",))},
    {"name": "read_traffic", "edits": False,
     "description": "The live traffic run as it is right now (it keeps running while you read): simulated "
                    "time, vehicles on the road, spawned and arrived counts, every signal's state (green, "
                    "amber, red per arriving road), and each vehicle's id, type, position (x_m, y_m; y "
                    "points down), heading (degrees, 0 = east, clockwise on screen), speed (km/h), road and "
                    "lane or junction turn, metres along it, origin and destination. Read it again to see "
                    "how things change. Filter by road id; limit caps the vehicles listed (default 100). Only the "
                    "user's live Run: run_simulation's headless runs are read with read_results or analyse_run.",
     "parameters": _object({"road": {"type": "string", "description": "a road or junction id"},
                            "limit": {"type": "integer"}})},
    # Demand: the OD matrix, worked through the Demand Generator's own controls (ai_mode.py
    # hands these to od_matrix.ODCellPanel.command), so the user sees the values arrive there.
    {"name": "list_institutions", "edits": False, "parameters": _object(),
     "description": "The institutions on the map (Demand Generator > Institutions): each one's id, kind, centre "
                    "(m), area, circle of influence (its catchment), people, employees, shift, its mode shares (people % "
                    "by mode; else its car share), its door (nearest junction), the junctions and ends its people come "
                    "from, and its people a day each way. A Run spawns their vehicle trips (catchment to door and "
                    "back) beside the OD matrix's."},
    {"name": "list_vehicle_types", "edits": False, "parameters": _object(),
     "description": "The vehicle types (Mode Behavior), in order: each one's name, vehicle_class (what it is, whatever "
                    "it's called: a bus calls at bus stops), timetabled (may it have a headway; "
                    "a car or bike never), headway (a timetable, e.g. a bus every 10 min), occupancy, size, top speed, acceleration, braking, standstill gap, reaction "
                    "time, imperfection, speed deviation and PCU (passenger car units; kept, not used in any calculation "
                    "yet); the default split between them; the kinds create_vehicle_types can make. desired_max_kmh "
                    "and startup_delay_s are read-only."},
    {"name": "create_vehicle_types", "edits": True,
     "description": "Add vehicle types (up to 8 in all), each made as a SUMO vehicle class with SUMO's defaults for "
                    f"it (size, speeds, acceleration, braking, gap): kind one of {', '.join(VEHICLE_CLASSES)}. "
                    "headway_min puts it on a timetable (e.g. a tram every 8 min); occupancy (people a vehicle) "
                    "overrides the kind's assumed one. A new type starts with 0 % of every OD pair's trips: give "
                    "it a share with set_od_demand's mode_split. Names must be new.",
     "parameters": _object({"types": {"type": "array", "items": {
         "type": "object", "required": ["name", "kind"],
         "properties": {"name": {"type": "string"}, "kind": {"type": "string", "enum": list(VEHICLE_CLASSES)},
                        "headway_min": {"type": "integer"}, "occupancy": {"type": "number"}}}}}, ("types",))},
    {"name": "update_vehicle_type", "edits": True,
     "description": "Change an existing vehicle type: new_name, vehicle_class (what it is: a bus calls at bus stops; "
                    "its figures stay), timetabled (true lets it have a headway; false clears "
                    "it), headway_min (0-120; a timetable; only a timetabled type), occupancy (people a "
                    "vehicle), length_m, width_m, max_speed_kmh, accel (m/s², 0.1-10), decel (comfortable braking, "
                    "m/s², 0.5-10), emergency_decel (the hardest it can brake, m/s², 0.5-15, no less than decel), "
                    "min_gap_m, tau_s (reaction time, s), "
                    "sigma (imperfection 0-1), speed_dev, pcu (passenger car units, 0.1-10), filters (in a lateral run "
                    "it passes queues within its lane, if narrow). Give only what changes.",
     "parameters": _object({"name": {"type": "string"}, "new_name": {"type": "string"}, "timetabled": {"type": "boolean"},
                            "vehicle_class": {"type": "string", "enum": list(VEHICLE_CLASSES)},
                            "headway_min": {"type": "integer"},
                            "occupancy": {"type": "number"}, "length_m": {"type": "number"}, "width_m": {"type": "number"},
                            "max_speed_kmh": {"type": "number"}, "accel": {"type": "number"}, "decel": {"type": "number"},
                            "emergency_decel": {"type": "number"}, "min_gap_m": {"type": "number"}, "tau_s": {"type": "number"}, "sigma": {"type": "number"},
                            "speed_dev": {"type": "number"}, "pcu": {"type": "number"},
                            "filters": {"type": "boolean"}}, ("name",))},
    {"name": "read_od_matrix", "edits": False, "parameters": _object(),
     "description": "The Demand Generator's OD matrix: its origin and destination boundary ends, which "
                    "demand it shows (hourly veh/h or daily trips/day: a Run uses the kind shown), every pair's "
                    "hourly_vph and daily_trips (pairs with none are left out; pairs_with_data_count 0 means the "
                    "matrix is empty), what most pairs share under usual (arrival pattern, split between the "
                    "vehicle_types, daily curve, hourly demand's peak_hour_factor and quarters: each 15 minutes' "
                    "share of the hour) and any pair that differs under different; total_hourly_vph and "
                    "total_daily_trips: the whole matrix. Read it before changing demand."},
    {"name": "set_od_demand", "edits": True,
     "description": "Enter demand in the OD matrix the way the user does in the Demand Generator: select the "
                    "pairs, type the value, pick the arrival pattern, set each vehicle type's share on the "
                    "mode slider. demand 'hourly' = veh/h, 'daily' = trips per day (default: the kind shown). "
                    "Give only what changes; a pattern or split alone needs pairs that already have that "
                    "demand. Use only end names read_od_matrix lists. A Run uses the kind the matrix shows, "
                    "and entering the other kind switches it (runs_use in the result).",
     "parameters": _object({**_PAIRS, "demand": _DEMAND_KIND,
                            "value": {"type": "number", "description": "veh/h (hourly) or trips per day (daily) "
                                      "for EACH pair named, not a total shared out (that's generate_demand total); "
                                      "no pairs, origins or destinations = every pair."},
                            "arrival_pattern": _ARRIVALS,
                            "peak_hour_factor": {"type": "number", "description": "Hourly demand only, 0.25-1 "
                                                 "(HCM PHF = V / 4·V15): the hour's second 15 minutes run at "
                                                 "veh/h ÷ PHF, the other three share the rest. 1 = even."},
                            "quarters": {"type": "array", "items": {"type": "number"}, "description":
                                         "Hourly demand only, instead of peak_hour_factor: the hour's four "
                                         "15-minute counts (or shares), e.g. [200, 300, 250, 250]."},
                            "mode_split": {"type": "array", "description": "Percent of trips per vehicle type "
                                           "(names from read_od_matrix's vehicle_types), adding up to 100; types "
                                           "left out get 0, e.g. [{\"mode\": \"Bus\", \"percent\": 20}, "
                                           "{\"mode\": \"Car\", \"percent\": 80}].",
                                           "items": {"type": "object", "required": ["mode", "percent"],
                                                     "properties": {"mode": {"type": "string"},
                                                                    "percent": {"type": "number"}}}},
                            "drop_offs": {"type": "array", "description": "The drop-off slider's stops: trips of a "
                                          "type ending part way, at a junction on the pair's route (or a share of "
                                          "the trip 0-1), whole % of that type's trips; the rest reach the "
                                          "destination. Replaces the types named; [] clears. Up to 20 pairs, e.g. "
                                          "[{\"mode\": \"Bus\", \"at\": \"N12\", \"percent\": 30}].",
                                          "items": {"type": "object", "required": ["mode", "at", "percent"],
                                                    "properties": {"mode": {"type": "string"},
                                                                   "at": {"type": "string"},
                                                                   "percent": {"type": "integer"}}}}})},
    {"name": "shape_daily_curve", "edits": True,
     "description": "Shape how the pairs' daily trips spread over 24 hours, in the Demand Generator's curve "
                    "editor. Give exactly one of: points [[hour, level], ...] with hours 0-24 and levels 0-1 "
                    "(1 = the busiest time), joined by a smooth curve that never overshoots, e.g. a commuter "
                    "day [[0,0.05],[6,0.3],[8,1],[10,0.5],[13,0.6],[17,0.9],[20,0.4],[24,0.05]]; polynomial "
                    "[a0, a1, ..., an] (degree 2-6) for level = a0 + a1*t + ... + an*t^n with t = hour / 24, "
                    "below zero counting as zero, e.g. [0, 4, -4] peaks at noon; or preset 'weekday' (morning "
                    "and evening peaks) or 'flat'. Only the shape matters: the hours share the day's trips in "
                    "proportion to the curve. Set the pairs' trips per day with set_od_demand. The matrix then "
                    "shows daily demand, so a Run uses daily demand (runs_use in the result).",
     "parameters": _object({**_PAIRS,
                            "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                            "polynomial": {"type": "array", "items": {"type": "number"}},
                            "preset": {"type": "string", "enum": ["weekday", "flat"]}})},
    {"name": "generate_demand", "edits": True,
     "description": "Fill the OD matrix by a gravity model, through the Demand Generator's controls: the trips "
                    "leaving each origin end and arriving at each destination end are shared out in proportion to "
                    "their supply and demand from set_end_flows (with none set anywhere: equally), sized to `total` "
                    "(else the ends' own veh/h). A trip can't start and end at one end, so flows at a single end "
                    "give nothing (refused). The trips are shared out by the shortest route "
                    "along the roads, weighted exp(-distance_decay_per_km x km) (default 0.5, an assumption: give "
                    "mean_trip_km to fit it instead), then balanced to "
                    "those totals (Furness). Pairs with no route get none. It replaces the demand of the kind "
                    "given in the pairs covered: every pair unless pairs, origins or destinations narrow it. "
                    "arrival_pattern and mode_split, as in set_od_demand, go to all of them. Returns the degree of "
                    "saturation (flow/capacity) of the most loaded junctions (signals as Webster times them, "
                    "roundabout entries, priority junctions' give-way movements; daily demand at its busiest hour). "
                    "To change demand already entered, keeping its pattern, use scale_demand.",
     "parameters": _object({**_PAIRS, "demand": _DEMAND_KIND,
                            "total": {"type": "number", "description": "veh/h (hourly) or trips per day (daily), "
                                                                       "when the ends' flows shouldn't set it."},
                            "degree_of_saturation": {"type": "number", "description": _SATURATION},
                            "distance_decay_per_km": {"type": "number", "description": "0 = distance doesn't "
                                                                                      "matter; 0.5 by default."},
                            "mean_trip_km": {"type": "number", "description": "Instead of distance_decay_per_km: "
                                             "fit the decay so the mean trip is this long (from a survey or "
                                             "counts; Hyman's calibration). The result gives mean_trip_km either way."},
                            "arrival_pattern": _ARRIVALS,
                            "mode_split": {"type": "array", "description": "As in set_od_demand.",
                                           "items": {"type": "object", "required": ["mode", "percent"],
                                                     "properties": {"mode": {"type": "string"},
                                                                    "percent": {"type": "number"}}}}})},
    {"name": "equilibrate_demand", "edits": True,
     "description": "Elastic demand (induced or suppressed traffic) after a change to the network: each OD pair's hourly "
                    "demand moves towards demand × (trip time now / trip time before)^elasticity, by headless runs "
                    "(iterations 1-6, default 3, of duration_min 5-240, default 30) until it settles. reference: the "
                    "run (R1...) of the network before the change. elasticity -2 to below 0, -0.5 by default (usual: "
                    "-0.3 to -0.8). Hourly demand only. A faster "
                    "network then carries more trips: test the design again with it.",
     "parameters": _object({"reference": {"type": "string"}, "elasticity": {"type": "number"},
                            "iterations": {"type": "integer"}, "duration_min": {"type": "number"}}, ("reference",))},
    {"name": "scale_demand", "edits": True,
     "description": "Scale the demand already in the OD matrix (the kind it shows: hourly or daily), keeping its "
                    "pattern between pairs: by `factor` (1.2 = 20 % more), or to `degree_of_saturation`. pairs, "
                    "origins or destinations narrow it; the rest stay as they are. Returns the factor used and the "
                    "most loaded junctions' degree of saturation after.",
     "parameters": _object({**_PAIRS, "factor": {"type": "number"},
                            "degree_of_saturation": {"type": "number", "description": _SATURATION}})},
    # Network testing: runs, experiments and their results (the window runs them, headless).
    {"name": "run_simulation", "edits": True,
     "description": "Test the network as it is: one simulation, headless and as fast as the computer allows, of "
                    "duration_min simulated minutes (1-240) with a seed (the Seed box's if left out) and the demand "
                    "the Demand Generator shows. It records the KPIs, saves a results workbook and returns the "
                    "headline figures and a run id (R1, R2...). One run is one sample: to compare designs use "
                    "run_experiment with several seeds.",
     "parameters": _object({"duration_min": {"type": "number"}, "seed": {"type": "integer"}, **_CONDITIONS,
                            **_MEASURE_FROM},
                           ("duration_min",))},
    {"name": "run_experiment", "edits": True,
     "description": "Test network designs against each other: every scenario (the first is the baseline; a "
                    "scenario changes the network with edit tools on a copy, scales the demand to stress it, and/or "
                    "ticks extensions) runs with every seed for duration_min. Returns per figure the mean, standard "
                    "deviation and 95 % confidence interval, replications_needed (runs for the mean within "
                    "±precision_percent; over 20: unstable), and each scenario's difference from the baseline by a "
                    "paired t-test on the shared seeds (common random numbers) with its p-value, interval (95 %, "
                    "Bonferroni-widened for several scenarios: confidence says) and whether it is clear (the "
                    "interval leaves out zero). Figures include the travel time, planning time and buffer indexes "
                    f"(reliability) and the unfinished share of trips. Saves an experiment workbook. At most {6} scenarios, {20} seeds, {60} runs. Use "
                    "at least 3 seeds to tell designs apart; say the question being tested in `question`. The answer "
                    "leads with the objective and delay, trip time, throughput and unfinished trips (every figure is "
                    "in the workbook). For a scenario described in plain words (a place, a grid, a traffic level), "
                    "build_scenario.",
     "parameters": _object({"scenarios": {"type": "array", "items": _SCENARIO},
                            "seeds": {"type": "array", "items": {"type": "integer"}, "description": "e.g. [1, 2, 3, 4, 5]"},
                            "duration_min": {"type": "number"}, "question": {"type": "string"}, **_MEASURE_FROM,
                            "precision_percent": {"type": "number", "description": "For replications_needed: "
                                                  "the mean within ± this % (default 5)."},
                            "conditions": {"type": "array", "items": {"type": "object"}, "description": "2-6 conditions "
                                           "every scenario runs under (FHWA): [{\"name\": \"peak\", \"demand_scale\": "
                                           "1.2, \"weight\": 2}, {\"name\": \"rain\", \"weather\": \"heavy rain\"}, "
                                           "{\"name\": \"crash\", \"incidents\": [...]}]: each condition's comparison, "
                                           "its winner, every design's regret and the robust choice."},
                            "factors": {"type": "array", "items": {"type": "object"}, "description": "Instead of "
                                        "scenarios, a full factorial of 1-3 factors: [{\"name\": \"signals\", \"levels\": "
                                        "[{\"label\": \"webster\"}, {\"label\": \"transyt\", \"changes\": [...]}]}, "
                                        "{\"name\": \"tsp\", \"levels\": [{\"label\": \"off\"}, {\"label\": \"on\", "
                                        "\"extensions\": [\"TSP\"]}]}]; with 2 factors, a two-way ANOVA (interaction)."},
                            "objective": {"type": "string", "description": "The figure conditions and factors are judged "
                                          "on (default delay_veh_h), e.g. people_per_h, mean_trip_s, co2_kg."}},
                           ("seeds", "duration_min"))},
    {"name": "read_results", "edits": False,
     "description": "Measured results: with no id, the runs and experiments of this session (ids, what they "
                    "were, headline figures, workbooks) and the latest run; with a run id (R1) its figures in full "
                    "(per road and per vehicle type); with an experiment id (E1) its comparison.",
     "parameters": _object({"id": {"type": "string"}})},
    {"name": "compare_runs", "edits": False,
     "description": "Two or more runs (ids from read_results) side by side: each headline figure and its change "
                    "from the first. Single runs are single samples: for a verdict, use run_experiment.",
     "parameters": _object({"runs": _IDS}, ("runs",))},
    {"name": "analyse_run", "edits": False,
     "description": "Statistics of one run (an id from read_results; the latest run if left out): travel-time "
                    "reliability (trip time mean, SD, CV, median, 85th and 95th percentiles; travel time, planning time "
                    "and buffer indexes, network-wide and the least reliable OD pairs), each origin end's arrivals "
                    "per minute against Poisson (index of dispersion, chi-square p: random, regular or bunched), and "
                    "each road's 95th-percentile queue against its length (spillback where it reaches it).",
     "parameters": _object({"id": {"type": "string"}})},
    {"name": "check_calibration", "edits": False,
     "description": "Validate a run (id; the latest if left out) against field data, such as traffic counts the "
                    "user gives. `counts` [{\"road\": id, "
                    "\"vph\": n}] (both directions of a two-way road together) beside the run's measured flows: each "
                    "count's GEH, the share under 5 (the target is 85 %), RMSE and %RMSE, MAPE, the slope of counted on "
                    "modelled (1 = no bias, above 1: the run is low) and R². `profiles` (FHWA Toolbox III 2019): "
                    "[{\"what\": \"flow\" | \"speed\" | \"trip_time\", \"road\": id (flow, speed), \"interval_min\": 15, "
                    "\"days\": [[one value an interval, as many intervals as the run has], ...3 or more days]}]: the "
                    "run's profile against the representative day's variation envelopes, Criteria I-IV (calibrated "
                    "when all hold). To fit the demand to counts, use calibrate_demand, then run again.",
     "parameters": _object({"id": {"type": "string"}, "counts": {"type": "array", "items": {
         "type": "object", "required": ["road", "vph"], "properties": {"road": {"type": "string"},
                                                                     "vph": {"type": "number"}}}},
                            "profiles": {"type": "array", "items": {"type": "object"}}})},
    {"name": "read_detectors", "edits": False,
     "description": "Detector data of a run (id; the latest if left out): at the middle of each link (one direction "
                    "of a road, '>' from its from-node, '<' back), flow, time-mean and space-mean speed, 15th/50th/85th "
                    "percentile speeds, headways (mean, cv, shortest, share under 3 s), occupancy, density and counts a "
                    "minute against Poisson; and at signalised stop lines, the measured saturation flow and start-up "
                    "lost time. The busiest links unless `road`.",
     "parameters": _object({"id": {"type": "string"}, "road": {"type": "string"}})},
    {"name": "analyse_safety", "edits": False,
     "description": "Safety surrogate for a run (id; the latest if left out): rear-end conflicts (time to collision "
                    "under 1.5 s), severe ones (TTC under 1 s or braking over 3.35 m/s² needed), per road with the rate "
                    "per million vehicle-km, a safety performance function across roads (Poisson or negative binomial "
                    "on flow and length) and the roads ranked by their Empirical Bayes excess (blackspots); also any "
                    "collisions (same lane or crossing, by road or junction) and emergency braking. ttc_s sets "
                    "the threshold (0.5-3, default 1.5).",
     "parameters": _object({"id": {"type": "string"}, "ttc_s": {"type": "number"},
                            "speed_change_kmh": {"type": "array", "items": {"type": "number"}, "description": "[before, "
                                                 "after] mean speeds: the change in crashes, injury and fatal crashes "
                                                 "they predict (Nilsson's power model)."}})},
    {"name": "forecast", "edits": False,
     "description": "Forecast a run's series (id; the latest if left out) by Holt's exponential smoothing, fitted to "
                    "it: `what` throughput, mean_speed, on_road, queued, trip_time, or a road's flow or speed (give "
                    "road); interval_min (default 1), horizon (intervals ahead, 1-60, default 10). Returns the forecasts with "
                    "rough 95 % intervals and the hold-out accuracy against the naive forecast.",
     "parameters": _object({"id": {"type": "string"}, "what": {"type": "string", "enum": [
         "throughput", "mean_speed", "on_road", "queued", "trip_time", "flow", "speed"]}, "road": {"type": "string"},
                            "interval_min": {"type": "integer"}, "horizon": {"type": "integer"}}, ("what",))},
    {"name": "critical_links", "edits": False,
     "description": "Which roads the network can least afford to lose (vulnerability): each road on a used route is "
                    "closed in turn and the demand shown re-routed at free flow: the extra vehicle-hours an hour and "
                    "the demand cut off, ranked; and the single points of failure (bridge roads and cut junctions: "
                    "what each cuts off). area {\"x\", \"y\", \"radius_m\"} closes every road touching that circle "
                    "at once instead (a flood, a fire). Static; check the worst with run_experiment (incidents).",
     "parameters": _object({"limit": {"type": "integer"}, "area": {"type": "object"}})},
    {"name": "benefit_cost", "edits": False,
     "description": "Appraise a scenario of an experiment (id) against its baseline in money: person-hours of delay, "
                    "vehicle-km and car CO2 saved per simulated hour, valued (value_of_time_per_h, cost_per_veh_km, "
                    "carbon_per_tonne), over hours_per_year and years (default 30); NPV and benefit-cost ratio at "
                    "discount_rates_percent (default 3.5, 4, 7) against capital_cost (and annual_cost); the BCR's "
                    "range over the seeds and whether the decision flips with the rate. Money in whole currency "
                    "units, not cents (defaults: 15 an hour of time, 0.15 a vehicle-km, 100 a tonne of CO2, 500 "
                    "hours a year).",
     "parameters": _object({"experiment": {"type": "string"}, "scenario": {"type": "string"},
                            "capital_cost": {"type": "number"}, "annual_cost": {"type": "number"},
                            "value_of_time_per_h": {"type": "number"}, "cost_per_veh_km": {"type": "number"},
                            "carbon_per_tonne": {"type": "number"}, "hours_per_year": {"type": "number"},
                            "years": {"type": "integer"},
                            "discount_rates_percent": {"type": "array", "items": {"type": "number"}}},
                           ("experiment", "capital_cost"))},
    {"name": "design_audit", "edits": False,
     "description": "Check the network's design against the AASHTO Green Book and a road safety checklist: sight "
                    "distance on curves and at junctions without signals (against the placed buildings), turns too "
                    "tight for the largest vehicle type, junction legs meeting under 75 degrees, arterial junctions "
                    "under 250 m apart, lane balance at merges and diverges, weaving segments under 750 m (HCM 2000 "
                    "Ch 24: type and limits; ramps closing with no auxiliary lane); each finding with the rule.",
     "parameters": _object()},
    {"name": "accessibility_report", "edits": False,
     "description": "What people can reach (Levine; Hansen): from each junction, jobs, school places, hospitals and "
                    "shops within budgets_min (default [15, 30]) and potential access to jobs (exp(-beta_per_min x "
                    "minutes), default 0.1); for the residents (Residential buildings) the means, the least served "
                    "and how evenly it's shared (Gini). Free-flow times, or a run's measured times (id). close_road: "
                    "the access lost with it closed.",
     "parameters": _object({"id": {"type": "string"}, "budgets_min": {"type": "array", "items": {"type": "number"}},
                            "beta_per_min": {"type": "number"}, "close_road": {"type": "string"}})},
    {"name": "regress", "edits": False,
     "description": "Least-squares regression with t-tests, R² and F: `y` on `x` (names) over `data` (a list of "
                    "objects) or over an experiment's runs (`experiment` id: each run's figures, by their key like "
                    "delay_veh_h or their label like Delay, plus demand_scale). `log` names take their natural log "
                    "(e.g. a volume-delay or "
                    "elasticity fit).",
     "parameters": _object({"y": {"type": "string"}, "x": {"type": "array", "items": {"type": "string"}},
                            "data": {"type": "array", "items": {"type": "object"}}, "experiment": {"type": "string"},
                            "log": {"type": "array", "items": {"type": "string"}}}, ("y", "x"))},
    {"name": "junction_los", "edits": False,
     "description": "Level of service per junction and approach for a run (id; the latest if left out): control delay "
                    "(each vehicle's time on the approach and through the junction less that at free flow), graded A-F "
                    "by the HCM 2000 (signals Exhibit 16-2; give-way, stop and roundabout entries 17-2), signal "
                    "approaches' v/c, 95th-percentile queues against their road, and LOS per 15-minute period.",
     "parameters": _object({"id": {"type": "string"}})},
    {"name": "weaving_los", "edits": False,
     "description": "Freeway weaving segments (a merge followed within 750 m by a diverge) by the HCM 2000 Ch 24: each "
                    "one's type, length and lanes, its four movements' pc/h from the demand on the routes a Run takes, "
                    "weaving and nonweaving speeds, density, LOS A-F and capacity with what bounds it; with a run (id), "
                    "the simulated speed and density on it too. Ramps with no auxiliary lane are listed as ramp "
                    "junctions.",
     "parameters": _object({"id": {"type": "string"}})},
    {"name": "metro_report", "edits": False,
     "description": "The MRT lines' loads at the design hour (the hour most ride): the MRT's own OD matrix (station "
                    "to station, M1->M2) and the buildings' MRT riders between their stations, on their line (no "
                    "transfers); each line's riders, "
                    "passenger-km, busiest stop-to-stop segment and its load each way against capacity (HCM 2000 Eq "
                    "27-28: 60 / headway trains x carriages x passengers a carriage x PHF 0.75), over capacity "
                    "flagged; boardings and "
                    "alightings a stop; riders no line serves.",
     "parameters": _object()},
    {"name": "diagnose_signal", "edits": False,
     "description": "Why a signal performs as it does in a run (id; the latest if left out), each signalised junction "
                    "or the one given: flow ratios, critical v/c and Webster's cycle for the demand, per approach v/c, "
                    "arrivals on green and arrival type (progression), the HCM delay against the simulated delay, "
                    "spillback; then the likely causes ranked, each with the tool that fixes it.",
     "parameters": _object({"id": {"type": "string"}, "junction": {"type": "string"}})},
    {"name": "check_run_health", "edits": False,
     "description": "Can a run (id; the latest if left out) be trusted? Gridlock, vehicles that couldn't enter, OD pairs "
                    "with no route, most trips unfinished at the end, a warm-up of half the run, signal approaches over "
                    "capacity; each problem with what to do (FHWA Toolbox III error checking). Check before concluding.",
     "parameters": _object({"id": {"type": "string"}})},
    {"name": "preflight_check", "edits": True,
     "description": "Check the network before trusting tests (FHWA Toolbox III): a run at a tenth of the demand listing "
                    "links driven far slower than free flow with no signal to explain it (a fault in the network), and "
                    "a run at half the demand checked for gridlock and blocked entries. duration_min each (5-60, "
                    "default 15).",
     "parameters": _object({"duration_min": {"type": "number"}, "seed": {"type": "integer"}})},
    {"name": "resilience", "edits": False,
     "description": "A disrupted run against the same seed's undisrupted run (both ids): minute by minute, when the "
                    "disruption starts, the biggest drop, minutes to recover, performance kept and extra vehicle-hours "
                    "(the resilience triangle). measure: throughput (default), speed or on_road. Make the two with "
                    "run_simulation on one seed: once as it is, once with incidents.",
     "parameters": _object({"baseline": {"type": "string"}, "disrupted": {"type": "string"},
                            "measure": {"type": "string", "enum": ["throughput", "speed", "on_road"]}},
                           ("baseline", "disrupted"))},
    {"name": "emissions_report", "edits": False,
     "description": "Fuel and CO2 of a run's cars and vans (id; the latest if left out) by Biggs and Akcelik's power "
                    "model from each vehicle's speed and acceleration: totals, per type, per vehicle-km and person-km, "
                    "and the roads burning most. Buses, lorries and bikes are left out.",
     "parameters": _object({"id": {"type": "string"}})},
    {"name": "power_analysis", "edits": False,
     "description": "How many seeds a comparison needs (paired t-test). From an experiment (id) and a figure (as in "
                    "its comparison) for one scenario against the baseline, or from sd (of the per-seed differences) "
                    "and difference: the seeds needed for 80 % and 90 % power, the smallest difference the "
                    "experiment's seeds (or `seeds`: a number of seeds, not a list) can find, and a power curve.",
     "parameters": _object({"experiment": {"type": "string"}, "figure": {"type": "string"},
                            "scenario": {"type": "string"}, "difference": {"type": "number"}, "sd": {"type": "number"},
                            "power": {"type": "number"}, "seeds": {"type": "integer"}})},
    {"name": "test_agent_compliance", "edits": True,
     "description": "Test an agent-decided extension (TSP, DEVL or DBL, as its tab chooses the model) on a fixed battery: "
                    "the network as it is, 30 % more demand, heavy rain and a lane closed on the longest road (when it "
                    "has 2+ lanes; the result lists the conditions run), each on `seeds` (default "
                    "1, 2) for duration_min (default 10); with DEVL, emergency vehicles every 4 minutes. Every run is "
                    "checked: no signal arm starved, no emergency vehicle held over 30 s, no gridlock, no agent "
                    "errors. Returns the pass rate and each failure with its evidence.",
     "parameters": _object({"extension": {"type": "string"}, "seeds": {"type": "array", "items": {"type": "integer"}},
                            "duration_min": {"type": "number"}}, ("extension",))},
    {"name": "read_decisions", "edits": False,
     "description": "The agent's decisions in a run (id; the latest if left out), TSP's, DEVL's and DBL's: when asked, the "
                    "junction, who decided, the answer, latency, the outcome (served, not needed, blocked: a hindsight "
                    "check), the telemetry it read and the person-delay estimate. Filter by junction, from_min, to_min; "
                    "limit rows (default 20).",
     "parameters": _object({"id": {"type": "string"}, "junction": {"type": "string"}, "from_min": {"type": "number"},
                            "to_min": {"type": "number"}, "limit": {"type": "integer"}})},
    {"name": "decision_sensitivity", "edits": True,
     "description": "What the TSP agent relies on: a sample (default 8) of a run's TSP decisions re-asked to the model "
                    "deciding now, each with one fact changed: no_passengers, exit_full, no_cross_queues, "
                    "arrives_on_green, no_estimate, route_blocked; how often each flips the answer, and how often the same question "
                    "asked again keeps it. It asks the model several times a decision.",
     "parameters": _object({"id": {"type": "string"}, "sample": {"type": "integer"},
                            "perturbations": {"type": "array", "items": {"type": "string"}}})},
    {"name": "parameter_sensitivity", "edits": True,
     "description": "How much the results depend on the model's calibration parameters: the current network and demand "
                    "run as it is and with each parameter 10 % lower and higher (tau_s: drivers' reaction time; "
                    "critical_gap_s: give-way gap; ring_timegap_s: roundabout entry gap), on the same seeds; each "
                    "figure's elasticity (percent change per percent change of the parameter) with its 95 % interval. "
                    "Several runs per seed (1 + 2 per parameter).",
     "parameters": _object({"parameters": {"type": "array", "items": {"type": "string",
                                                                      "enum": ["tau_s", "critical_gap_s", "ring_timegap_s"]}},
                            "seeds": {"type": "array", "items": {"type": "integer"}},
                            "duration_min": {"type": "number"}, "measure_from_min": {"type": "number"},
                            "step": {"type": "number", "description": "the change, 0.05 to 0.3 (default 0.1)"}})},
    {"name": "rank_scenarios", "edits": False,
     "description": "Rank every scenario of an experiment (id) on one figure (as in its comparison), all pairs, not "
                    "just against the baseline: randomized-block ANOVA and Friedman's rank test (no normality "
                    "needed), every pair's paired difference (Bonferroni) with Wilcoxon's p, and group letters: "
                    "scenarios sharing a letter aren't told apart.",
     "parameters": _object({"experiment": {"type": "string"}, "figure": {"type": "string"}}, ("experiment", "figure"))},
    {"name": "network_indices", "edits": False, "parameters": _object(),
     "description": "Graph indices of the road network (Geography of Transport Systems): vertices, edges, components, "
                    "cyclomatic number, alpha, beta, gamma, the diameter in links, degrees, the detour index between "
                    "boundary ends (route km over straight km) and the most and least accessible junctions (Shimbel)."},
    {"name": "cluster_conditions", "edits": False,
     "description": "Group observed days into travel conditions by k-means (FHWA Toolbox III 2019): `days` [{\"label\": "
                    "\"Mon 3 Mar\", \"values\": {\"volume\": 5200, \"rain_mm\": 0, \"incidents\": 1}}, ...], each attribute "
                    "normalised 0-1; k chosen by the within/between variance ratio unless given. Each cluster's size, "
                    "weight (its share of days), centroid and representative day, to test as weighted scenarios.",
     "parameters": _object({"days": {"type": "array", "items": {"type": "object"}}, "k": {"type": "integer"},
                            "seed": {"type": "integer"}}, ("days",))},
    {"name": "build_scenario", "edits": True,
     "description": "Build and test what the user describes in one go (the Scenario Builder): fill in only what "
                    "they say; the app adds stated defaults, plans the steps (network from a place or a grid, junction "
                    "control, signal timing, buses, demand sized to a traffic level and checked in a run, mode choice, "
                    "route choice, a baseline run with its statistics, the reports asked for, and each what-if as a "
                    "paired experiment against the network as built), shows the user the plan to apply once, runs it "
                    "as one undo step and returns a plain report with the measured numbers. Prefer it whenever the user "
                    "describes a scenario to build or changes to compare in plain words. Its what-ifs are only the "
                    "listed changes (an empty where means the busiest, or all); for exact roads, junctions or any "
                    "other edit, use run_experiment.",
     "parameters": scenario.SCHEMA},
    {"name": "assign_routes", "edits": True,
     "description": "Route choice for every run that follows (it is saved with the network): each OD pair gets up "
                    "to `routes` alternative routes (default 3) and each vehicle draws one by path-size logit on link "
                    "times, theta_per_min (default 0.5: higher, more to the fastest). iterations (0-10, default 4): "
                    "headless runs of duration_min, each measuring the link times and averaging them in (the method of "
                    "successive averages, toward stochastic user equilibrium); 0 uses free-flow times. off: true "
                    "puts every vehicle back on the fastest route; until_gap (e.g. 0.02) stops once the relative gap (share of flow off its logit share) is "
                    "that low (settled). Returns each iteration's relative gap and the busiest pairs' route shares.",
     "parameters": _object({"iterations": {"type": "integer"}, "duration_min": {"type": "number"},
                            "seed": {"type": "integer"}, "theta_per_min": {"type": "number"},
                            "routes": {"type": "integer"}, "off": {"type": "boolean"},
                            "until_gap": {"type": "number"}})},
    {"name": "optimise_signals", "edits": True,
     "description": "Search fixed-time signal settings for `junctions` (signalised) by a genetic algorithm, each "
                    "candidate's cycle and green split scored by headless runs on the same seeds (default 1-3): "
                    "objective delay (default), trip_delay, throughput or conflicts; population (4-10, default 6) x "
                    "generations (1-5, default 3), at most 120 runs. Starts from the plans the junctions now run. "
                    "Returns the best greens per junction against the current plan (paired over the seeds) and the "
                    "set_signal_timing to apply; the open network is not changed.",
     "parameters": _object({"junctions": _IDS, "seeds": {"type": "array", "items": {"type": "integer"}},
                            "duration_min": {"type": "number"}, "population": {"type": "integer"},
                            "generations": {"type": "integer"},
                            "objective": {"type": "string", "enum": ["delay", "trip_delay", "throughput", "conflicts"]}},
                           ("junctions", "duration_min"))},
    {"name": "set_extension", "edits": True,
     "description": "Tick or untick an extension in the Extensions tab by name (e.g. \"Transit Signal Priority "
                    "(TSP)\", \"Dynamic Emergency Vehicle Lane (DEVL)\") for the runs that follow; `settings` fills fields of its "
                    "panel by label, for an extension that has one. The result gives the extension's own "
                    "description: one marked placeholder does nothing yet.",
     "parameters": _object({"name": {"type": "string"}, "on": {"type": "boolean"},
                            "settings": {"type": "object"}}, ("name", "on"))},
    {"name": "read_extensions", "edits": False,
     "description": "Every extension in the Extensions tab: whether it is ticked, what it does, and each setting "
                    "set_extension can fill, by label, with its value and what it can be (e.g. the DEVL tab's "
                    "Decided by and Models, Batch Run's Seeds); and the emergency vehicle schedules.",
     "parameters": _object()},
    {"name": "schedule_emergency", "edits": True,
     "description": "Replace the Dispatch Emergency Vehicle tab's schedules: each sends an emergency vehicle from "
                    "one place to another (a boundary end like 1W or a junction like 2,1, with a route between "
                    "them), every every_min minutes from the start (0: once, at once) or once after_min minutes "
                    "in (a schedule has one of the two). Batch Run's Test DEVL dispatches them in every run. "
                    "dispatch_now: also send them into the live run (a run must be going).",
     "parameters": _object({"schedules": {"type": "array", "items": {
                                "type": "object", "required": ["from", "to"],
                                "properties": {"from": {"type": "string"}, "to": {"type": "string"},
                                               "every_min": {"type": "integer", "description": "0-120; 0: once"},
                                               "after_min": {"type": "integer", "description": "0-120"}}}},
                            "dispatch_now": {"type": "boolean"}}, ("schedules",))},
    {"name": "start_batch", "edits": True,
     "description": "Start Batch Run, which tests the AI controllers: every seed (a list, like [1, 2, 3, 4]) runs "
                    "duration_min simulated minutes, headless, every combination of the tested controllers' models, "
                    "each compared with the default configuration (the network's own signal plans, no controller "
                    "acting; compare_default false leaves it out). test_tsp: the TSP tab's models deciding bus "
                    "priority; test_dbl: the DBL tab's models deciding the bus lanes; test_devl: DEVL's emergency "
                    "vehicle schedules dispatched in each run, the DEVL tab's models deciding the lanes. A tested "
                    "controller needs a model picked in its tab. Pick the models "
                    "in each controller's tab, and the pace, runs at once and times each in Batch Run's own tab, with "
                    "set_extension first (read_extensions shows them). Saves each run's workbook and a batch "
                    "workbook comparing the models; it takes as long as the Batch Run tab says.",
     "parameters": _object({"seeds": {"type": "array", "items": {"type": "integer"}, "description": "e.g. [1, 2, 3, 4]"},
                            "duration_min": {"type": "integer"},
                            "test_tsp": {"type": "boolean"}, "test_dbl": {"type": "boolean"},
                            "test_devl": {"type": "boolean"},
                            "compare_default": {"type": "boolean", "description": "default true"}},
                           ("seeds", "duration_min"))},
    {"name": "replay_run", "edits": True,
     "description": "Run a batch run's workbook (path) again from its telemetry feed, each decision as recorded, "
                    "no model asked, and say whether it came out the same.",
     "parameters": _object({"path": {"type": "string"}}, ("path",))},
    {"name": "calibrate_demand", "edits": True,
     "description": "Fit the hourly OD matrix to counted flows: `counts` [{\"road\": id, \"vph\": n}] (both "
                    "directions of a two-way road together). Each pair keeps its shortest route; the flows are "
                    "scaled so the modelled road flows meet the counts (ME2's multiplicative balancing, Van Zuylen and "
                    "Willumsen 1980), starting from the demand entered. Returns each count before and after with its "
                    "GEH (under 5 is a good fit). Works the Demand Generator's controls; one undo step.",
     "parameters": _object({"counts": {"type": "array", "items": {"type": "object", "required": ["road", "vph"],
                                                                  "properties": {"road": {"type": "string"},
                                                                                 "vph": {"type": "number"}}}},
                            "iterations": {"type": "integer"}}, ("counts",))},
    {"name": "choose_modes", "edits": True,
     "description": "Set each OD pair's split between vehicle types by mode choice (multinomial logit) instead of a "
                    "fixed split: per pair, each alternative's utility = constant + in_vehicle_per_min x in-vehicle "
                    "minutes + out_of_vehicle_per_min x (out_of_vehicle_min + half its headway) + cost x (cost + "
                    "cost_per_km x route km). Coefficients default to HCM 2000's (-0.025, -0.050 a minute, -0.005 a "
                    "cent: give costs in cents). In-vehicle minutes come from from_run's measured trips of that pair "
                    "and type (a run id, e.g. R1) where it has them, else free flow along the shortest route. The "
                    "logit shares are people; each type's vehicles are its people over its occupancy, and timetabled "
                    "types keep their timetable. A pair's people stay; its vehicles change with the split. Road pairs "
                    "never take the MRT (it has its own OD matrix). nests make it a nested logit. Also returns "
                    "the value of time, each mode's mean direct elasticities and each pair's logsum (a change's "
                    "benefit is the change in logsum over -cost, cents a trip). Works the Demand Generator's controls.",
     "parameters": _object({**_PAIRS,
                            "alternatives": {"type": "array", "description": "The vehicle types to choose between, "
                                             "e.g. [{\"mode\": \"Car\", \"cost_per_km\": 25}, {\"mode\": \"Bus\", "
                                             "\"cost\": 200, \"out_of_vehicle_min\": 5, \"constant\": -0.5}].",
                                             "items": {"type": "object", "required": ["mode"], "properties": {
                                                 "mode": {"type": "string"}, "constant": {"type": "number"},
                                                 "cost": {"type": "number"}, "cost_per_km": {"type": "number"},
                                                 "out_of_vehicle_min": {"type": "number"}}}},
                            "coefficients": {"type": "object", "description": "Any of in_vehicle_per_min, "
                                             "out_of_vehicle_per_min, cost (each 0 or below)."},
                            "nests": {"type": "array", "description": "Nested logit: groups of alternatives that "
                                      "substitute for each other, e.g. [{\"name\": \"public\", \"modes\": [\"Bus\", "
                                      "\"Tram\"], \"scale\": 0.5}] (scale 0-1; 1 is plain logit).",
                                      "items": {"type": "object"}},
                            "from_run": {"type": "string", "description": "A run id whose trip times to use."}},
                           ("alternatives",))},
    # The planner's own record, for studying how it plans (no change to the project).
    {"name": "find_tools", "edits": False,
     "description": "More tools: you are offered the ones this request most likely needs. Say what else you need "
                    "(e.g. \"bus stops\", \"safety\", \"emergency vehicles\", \"signal timing\") and the matching "
                    "tools are added, each with what it does; then call them.",
     "parameters": _object({"need": {"type": "string"}}, ("need",))},
    {"name": "note_plan", "edits": False,
     "description": "Write down, before testing, what you plan and why: the goal, your hypothesis, the design "
                    "changes you'll test and what you expect to see. It changes nothing; it goes into the planning "
                    "journal kept with each experiment's workbook, beside your tool calls and the user's decisions, "
                    "so your planning can be studied. Note again after results, with what you conclude.",
     "parameters": _object({"goal": {"type": "string"}, "hypothesis": {"type": "string"},
                            "plan": {"type": "string"}, "expected": {"type": "string"},
                            "conclusion": {"type": "string"}})},
    # Edit: each runs only after the user approves it.
    {"name": "set_signal_timing", "edits": True,
     "description": "Set how signalised junctions are timed: method one of " + ", ".join(SIGNAL_METHODS) + " "
                    "(webster: each junction alone from its demand; transyt, synchro: coordinated offline; scats, "
                    "scoot: adaptive from detectors; actuated: each green extends while vehicles keep coming, 7-50 s; "
                    "max_pressure: the phase with the most vehicles waiting, less those queued past it, goes next; "
                    "fixed: the green_times_s given; manual: the windows given), phases 2 or 4, green_times_s "
                    "(one a phase, for fixed), windows (manual: each arm, N/E/S/W or its road id, -> [first, last] "
                    "second of the cycle it goes, its last amber_s seconds amber, red the rest; opposite arms may share "
                    "a window to go together, others go in turn; an arm left out stays red), amber_s (3-6, manual; "
                    "3 by default), cycle_min_s and cycle_max_s (20-240, the coordinated methods), "
                    "target_saturation_percent (50-110, SCATS and SCOOT), stop_weight_s (TRANSYT and SCOOT). Give "
                    "only what changes. The junctions must be signalised (set_junction_control).",
     "parameters": _object({"junctions": _IDS, "method": {"type": "string", "enum": list(SIGNAL_METHODS)},
                            "phases": {"type": "integer", "description": "2 or 4"},
                            "green_times_s": {"type": "array", "items": {"type": "number"},
                                              "description": "seconds, one a phase; given alone they mean method fixed"},
                            "windows": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "number"}},
                                        "description": "arm -> [first, last] s, e.g. {\"N\": [1, 120], \"E\": [121, 150]}; "
                                                       "given alone they mean method manual"},
                            "amber_s": {"type": "number"},
                            "cycle_min_s": {"type": "number"}, "cycle_max_s": {"type": "number"},
                            "target_saturation_percent": {"type": "number"}, "stop_weight_s": {"type": "number"}},
                           ("junctions",))},
    {"name": "place_map", "edits": True,
     "description": "Put OpenStreetMap under the network: a `place` found by name (Nominatim; the first match), or "
                    "lat and lon; rotation_deg turns the map. That place goes under the network's centre with a work "
                    "zone round it, width_m x height_m (100-20000; one alone a square; 1200 x 1200 by default), the "
                    "area the view keeps to; then network_from_background builds that zone's roads.",
     "parameters": _object({"place": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"},
                            "rotation_deg": {"type": "number"}, "width_m": {"type": "number"},
                            "height_m": {"type": "number"}})},
    {"name": "add_bus_stop", "edits": True,
     "description": "Add a bus stop (a bay beside the kerb) on a road: direction forward (from -> to), backward or "
                    "both; dwell_s how long buses stand (20 by default). With passengers (boarding_per_h arriving to "
                    "board in each direction, alighting_percent of a bus's load getting off) each bus stands as long "
                    "as its passengers take (HCM 2000 Eq 27-2) instead. It goes where the road has room.",
     "parameters": _object({"road": {"type": "string"},
                            "direction": {"type": "string", "enum": list(BUS_STOP_DIRECTIONS)},
                            "dwell_s": {"type": "number"}, "boarding_per_h": {"type": "number"},
                            "alighting_percent": {"type": "number"}}, ("road",))},
    {"name": "update_bus_stop", "edits": True,
     "description": "Move a bus stop along its road (distance_m from the road's from-node), change the traffic it "
                    "serves, its dwell time, or its passengers (boarding_per_h, alighting_percent; both 0: none).",
     "parameters": _object({"stop": {"type": "string"}, "distance_m": {"type": "number"},
                            "direction": {"type": "string", "enum": list(BUS_STOP_DIRECTIONS)},
                            "dwell_s": {"type": "number"}, "boarding_per_h": {"type": "number"},
                            "alighting_percent": {"type": "number"}}, ("stop",))},
    {"name": "update_institution", "edits": True,
     "description": "Change a placed institution's figures: people, employees, shift_start_h and shift_end_h (hours "
                    "after midnight), car_share (percent; replaces a mode split: Car that share, the rest walk), "
                    "radius_m (its circle of influence: the catchment its people come from).",
     "parameters": _object({"id": {"type": "string"}, "people": {"type": "integer"}, "employees": {"type": "integer"},
                            "shift_start_h": {"type": "number"}, "shift_end_h": {"type": "number"},
                            "car_share": {"type": "number"}, "radius_m": {"type": "number"}}, ("id",))},
    {"name": "set_junction_control", "edits": True,
     "description": "Make junctions signalised, roundabouts (sized from their roads) or uncontrolled. Slip "
                    "roads past a new roundabout move out along their roads, clear of its circle.",
     "parameters": _object({"junctions": _IDS, "control": {"type": "string", "enum": list(CONTROLS)}},
                           ("junctions", "control"))},
    {"name": "update_roads", "edits": True,
     "description": "Change road settings; give only what changes. add_lanes 1 adds a lane each way to each road "
                    "(\"add a lane\"), -1 removes one; lanes_per_direction sets a number instead. direction 'one-way' "
                    "keeps traffic from -> to, 'reverse' makes it one-way the other way. speed_limit_kmh 5-130. length_m "
                    "moves the road's to-junction along it (that junction's other roads stretch too). "
                    "pocket_junction (an end its traffic arrives at) sets a turn pocket on that approach: "
                    "pocket_lanes 1-2 (0 removes it), pocket_length_m 15-150, pocket_side 'kerb' (kerb turns) or "
                    "'offside' (offside turns, U-turns). footpaths 'both', 'left', 'right' (from -> to) or 'none'; "
                    "cycle_lanes 'both' (a bikes-only lane each way), 'forward' (from -> to only), 'backward' (to -> "
                    "from only) or 'none'. Only a bicycle vehicle type rides one (create_vehicle_types).",
     "parameters": _object({"roads": _IDS, "lanes_per_direction": {"type": "integer"},
                            "add_lanes": {"type": "integer", "description": "Lanes to add each way (negative: remove)."},
                            "speed_limit_kmh": {"type": "number"},
                            "direction": {"type": "string", "enum": ["two-way", "one-way", "reverse"]},
                            "lane_changing": {"type": "boolean"},
                            "road_type": {"type": "string", "description": "A label only: changes no behaviour."},
                            "length_m": {"type": "number"}, "pocket_junction": {"type": "string"},
                            "pocket_lanes": {"type": "integer"}, "pocket_length_m": {"type": "number"},
                            "pocket_side": {"type": "string", "enum": ["kerb", "offside"]},
                            "footpaths": {"type": "string", "enum": list(FOOTPATH_SIDES)},
                            "cycle_lanes": {"type": "string", "enum": list(CYCLE_LANE_SIDES)}}, ("roads",))},
    {"name": "add_crossing", "edits": True,
     "description": "Put a pedestrian crossing across a road with footpaths on both sides, at_m from its from-junction "
                    "(the middle if not given), at least 20 m from either junction and beyond its corners: kind 'zebra' (walkers have right of "
                    "way: traffic stops for them) or 'overbridge' (over the traffic, stairs to climb). Without one, "
                    "walkers cross a junction's arm in a gap in its traffic.",
     "parameters": _object({"road": {"type": "string"}, "at_m": {"type": "number"},
                            "kind": {"type": "string", "enum": list(CROSSING_KINDS)}}, ("road",))},
    {"name": "add_path", "edits": True,
     "description": "Add a footpath of its own (through a park, a square), off the roads: from one junction or boundary "
                    "end to another, through bends [[x, y], ...] in metres. Walkers use it with the roads' footpaths.",
     "parameters": _object({"start": {"type": "string"}, "end": {"type": "string"},
                            "bends": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}}},
                           ("start", "end"))},
    {"name": "set_pedestrian_demand", "edits": True,
     "description": "Set people walking between boundary ends (pairs like '1W->1E') or between deploy points "
                    "('S1->S2'; add_spawn_points): people_per_hour (hourly demand) or people_per_day (daily demand), "
                    "one of them; 0 removes a pair. Daily: shape the pairs' day with points, polynomial or preset as "
                    "shape_daily_curve takes them (else a new pair's day is the weekday's, a pair's own kept). A run "
                    "uses whichever the Demand Generator's Hourly/Daily switch shows, as for vehicles. They walk the "
                    "footpaths and paths; institutions' walkers come on top.",
     "parameters": _object({"pairs": _IDS, "people_per_hour": {"type": "number"}, "people_per_day": {"type": "number"},
                            "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                            "polynomial": {"type": "array", "items": {"type": "number"}},
                            "preset": {"type": "string", "enum": ["weekday", "flat"]}}, ("pairs",))},
    {"name": "add_spawn_points", "edits": True,
     "description": "Place pedestrian deploy points S1, S2... at points [[x, y], ...] metres, anywhere near the "
                    "network: walkers go straight from one to its nearest footpath and from the footpath nearest their "
                    "destination straight to it. Then set_pedestrian_demand between them ('S1->S2').",
     "parameters": _object({"points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}}},
                           ("points",))},
    {"name": "pedestrian_summary", "edits": False,
     "description": "The network for pedestrians: roads with footpaths (both sides, one, none) and cycle lanes, paths, "
                    "crossings, the pedestrian demand, and the pairs no footpath or path joins (nobody can walk them).",
     "parameters": _object({})},
    {"name": "add_junction", "edits": True,
     "description": "Add an off-grid junction (named N1, N2, ...) at (x, y) metres: x grows east, y grows SOUTH "
                    "(down the screen). Connect it with add_road.",
     "parameters": _object({"x": {"type": "number"}, "y": {"type": "number"}}, ("x", "y"))},
    {"name": "add_road", "edits": True,
     "description": "Connect two junctions (ids like '2,2' or 'N12', not boundary ends) with a new two-way road. "
                    "A road out to the edge is add_boundary_end; a turning lane cutting a corner is add_slip_road.",
     "parameters": _object({"from_junction": {"type": "string"}, "to_junction": {"type": "string"},
                            "crossings": _CROSSINGS}, ("from_junction", "to_junction"))},
    {"name": "add_boundary_end", "edits": True,
     "description": "Add a boundary end (where traffic enters and leaves) at (x, y) metres joined to a junction "
                    "by a new road; x grows east, y grows SOUTH (down the screen). Named by side and position, "
                    "e.g. 1.5E.",
     "parameters": _object({"from_junction": {"type": "string"}, "x": {"type": "number"},
                            "y": {"type": "number"}, "crossings": _CROSSINGS}, ("from_junction", "x", "y"))},
    {"name": "move_junctions", "edits": True,
     "description": "Move junctions or ends by (dx, dy) metres; only their roads stretch. x grows east, y grows "
                    "SOUTH (down the screen): 50 m north is dy -50.",
     "parameters": _object({"junctions": _IDS, "dx": {"type": "number"}, "dy": {"type": "number"}},
                           ("junctions", "dx", "dy"))},
    {"name": "add_metro_line", "edits": True,
     "description": "Build an MRT line (as Edit Network > MRT does) through stations at points [[x, y], ...] "
                    f"metres, in order, 2 or more, each at least {METRO_MIN_SPACING_M:g} m from the last: underground or "
                    "raised, apart from "
                    "the roads. Each leg curves from its station; a turn too sharp draws the station in, so the result "
                    "gives where each one went. A leg crossing another line is refused unless merge is true: then "
                    "it ends at an interchange station there, on both lines, and the next point carries on. Service "
                    "(assumed if not given): headway_min 5 (1-60) between trains each way, speed_kmh 35 (10-120, "
                    "dwell included), cars_per_train 5 (1-12), car_capacity 200 (20-400 passengers), fare_cents 100. "
                    "Then set_metro_demand gives it riders; institutions' MRT riders come on top.",
     "parameters": _object({"stations": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                            "merge": {"type": "boolean"}, "headway_min": {"type": "number"},
                            "speed_kmh": {"type": "number"}, "cars_per_train": {"type": "integer"},
                            "car_capacity": {"type": "integer"}, "fare_cents": {"type": "number"}}, ("stations",))},
    {"name": "update_metro_line", "edits": True,
     "description": "Change an MRT line's service (ranges as add_metro_line's); give only what changes. Its stations "
                    "and track stay as built.",
     "parameters": _object({"line": {"type": "string"}, "headway_min": {"type": "number"},
                            "speed_kmh": {"type": "number"}, "cars_per_train": {"type": "integer"},
                            "car_capacity": {"type": "integer"}, "fare_cents": {"type": "number"}}, ("line",))},
    {"name": "set_metro_demand", "edits": True,
     "description": "Set MRT passengers between stations (pairs like 'M1->M2', joined by one line or one change at "
                    "an interchange), the Demand Generator's MRT matrix: passengers_per_hour (hourly demand) or "
                    "passengers_per_day (daily demand), one of them; 0 removes a pair. Daily: shape the pairs' day "
                    "with points, polynomial or preset as shape_daily_curve takes them. metro_report then gives the "
                    "lines' loads against capacity.",
     "parameters": _object({"pairs": _IDS, "passengers_per_hour": {"type": "number"},
                            "passengers_per_day": {"type": "number"},
                            "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                            "polynomial": {"type": "array", "items": {"type": "number"}},
                            "preset": {"type": "string", "enum": ["weekday", "flat"]}}, ("pairs",))},
    {"name": "delete_objects", "edits": True,
     "description": "Delete roads, junctions (with their roads), boundary ends, bus stops, institutions, paths, "
                    "crossings, deploy points or MRT lines (ML1: with its stations and their demand; an interchange "
                    "stays on its other line).",
     "parameters": _object({"ids": _IDS}, ("ids",))},
    {"name": "build_interchange", "edits": True,
     "description": "Build a grade-separated interchange. trumpet: 3 junctions (two on the freeway, stem "
                    "= the terminating road's); diamond or spui: 4 junctions (freeway = the pair the "
                    "freeway runs through). replace removes the roads it supersedes.",
     "parameters": _object({"kind": {"type": "string", "enum": ["trumpet", "diamond", "spui"]},
                            "junctions": _IDS, "stem": {"type": "string"}, "freeway": _IDS,
                            "replace": {"type": "boolean"}, "freeway_lanes": {"type": "integer"}},
                           ("kind", "junctions"))},
    {"name": "add_slip_road", "edits": True,
     "description": "Add a one-way slip road past a junction for the kerb-side turn (left when driving on the "
                    "left) between 2 roads meeting there, in either order. Both roads are split 40 m or more "
                    "from the junction; only the kerbside lane enters and leaves it.",
     "parameters": _object({"roads": _IDS, "single_lane": {"type": "boolean", "description": "One lane, not two."}},
                           ("roads",))},
    {"name": "set_end_flows", "edits": True,
     "description": "Set a boundary end's supply (entering) and/or demand (leaving) in veh/h, one end a call. "
                    "Only generate_demand's gravity model (and the Summary) reads them: a Run spawns traffic from "
                    "the OD matrix alone, so fill it after (generate_demand or set_od_demand).",
     "parameters": _object({"end": {"type": "string"}, "supply_vph": {"type": "number"},
                            "demand_vph": {"type": "number"}}, ("end",))},
    {"name": "place_institution", "edits": True,
     "description": "Place one institution on clear ground: near a junction or end (the nearest clear spot to it) or at "
                    "x, y (m). Its circle of influence radius_m is the catchment its people come from (default: just "
                    "enough to reach 4 junctions or ends); its door is its nearest junction. Other figures default to "
                    "its kind's usual ones. A Run spawns its trips; set its mode split with institution_mode_split.",
     "parameters": _object({"kind": {"type": "string", "enum": list(KINDS)}, "near": {"type": "string"},
                            "x": {"type": "number"}, "y": {"type": "number"}, "radius_m": {"type": "number"},
                            "area_m2": {"type": "number"}, "people": {"type": "integer"},
                            "employees": {"type": "integer"}, "shift_start_h": {"type": "number"},
                            "shift_end_h": {"type": "number"}, "car_share": {"type": "number"}}, ("kind",))},
    {"name": "institution_mode_split", "edits": True,
     "description": "Each institution's mode split by multinomial logit (the Institutions panel's Mode split): its "
                    "people choose between Walk and the vehicle types for the trips from each junction or end in its "
                    "circle to its door (route km). U = constant + in_vehicle_per_min x in-vehicle minutes (free flow "
                    "at the type's speed and the limits) + out_of_vehicle_per_min x (out_of_vehicle_min + half the "
                    "headway; walking: its minutes at walk_kmh) + cost x (cost + cost_per_km x km), averaged over the "
                    "catchment. observed_percent (e.g. {\"Car\": 30}) calibrates the constants until the model gives "
                    "those shares (the others keep their relative odds). Defaults are stated in the result. Sets "
                    "mode_shares; a Run's vehicles are people x share / occupancy (walkers none, bus riders on the buses). "
                    "With MRT lines, the MRT stations of its door station's line (the nearest within 800 m) inside "
                    "its circle are places its people come from too, as the junctions are: there they choose \"MRT\" "
                    "(the ride, half the headway, the walk on; cost the line's fare plus any given) or Walk; riders add "
                    "no vehicles.",
     "parameters": _object({"institutions": _IDS,
                            "alternatives": {"type": "array", "description": "The modes to choose between: Walk and "
                                             "vehicle types, e.g. [{\"mode\": \"Walk\"}, {\"mode\": \"Car\", "
                                             "\"cost_per_km\": 15, \"out_of_vehicle_min\": 3}, {\"mode\": \"Bus\", "
                                             "\"cost\": 100}]. Default: Walk and every type.",
                                             "items": {"type": "object", "required": ["mode"], "properties": {
                                                 "mode": {"type": "string"}, "constant": {"type": "number"},
                                                 "cost": {"type": "number"}, "cost_per_km": {"type": "number"},
                                                 "out_of_vehicle_min": {"type": "number"}}}},
                            "observed_percent": {"type": "object", "description": "Known shares to calibrate to, "
                                                 "percent by mode, e.g. {\"Car\": 30, \"Walk\": 45}."},
                            "coefficients": {"type": "object", "description": "Any of in_vehicle_per_min, "
                                             "out_of_vehicle_per_min, cost (each 0 or below)."},
                            "walk_kmh": {"type": "number", "description": "Walking speed, default 5."}})},
    {"name": "deploy_institutions", "edits": True,
     "description": "Place institutions at random on clear ground within the network's extent (never on a road, "
                    "a junction or another building), each with its kind's usual figures and a circle of "
                    "influence (the catchment its people come from) reaching its 4 nearest junctions or ends (or radius_m). kinds: any of "
                    f"{', '.join(KINDS)} (all if left out). The same seed gives the same layout.",
     "parameters": _object({"count": {"type": "integer", "description": "1 to 50"},
                            "kinds": {"type": "array", "items": {"type": "string", "enum": list(KINDS)}},
                            "seed": {"type": "integer"}, "radius_m": {"type": "number"}}, ("count",))},
    {"name": "network_from_background", "edits": True,
     "description": "Replace the whole network (undoable) with the roads of the background. Map: OpenStreetMap's "
                    "road data (not the picture) for the map's work zone (or width_m x height_m round its centre, "
                    "200-4000; a bigger zone is read 4000 m a side), with lanes, speed limits, one-way streets, roundabouts and "
                    "bridges as mapped, and junctions signalised where OSM has a junction signal (a pedestrian "
                    "crossing's, an emergency or a flashing signal leaves a priority junction; OSM holds no signal "
                    "timings, so each starts on Webster); road_classes 'main' (motorways to secondary roads) or 'all' (down to "
                    "residential streets, the default). Image: the pixels of road_colour ('#rrggbb'; ask the "
                    "user if you don't know it) within colour_tolerance (default 40), traced to centre lines at "
                    "resolution_m metres a pixel (default 2); lanes from their width. Roads leaving the area "
                    "become boundary ends. Only roads a car can drive between the ends are kept (one-way traps and "
                    "cut-off parts left out: streets_left_out), so every OD pair has a route. Institutions still on "
                    "clear ground stay. Walking (the map), only when the user asks for it: "
                    "footpaths 'tagged' (OSM's sidewalk tags and ways, with its zebras, footbridges and paths), 'all' "
                    "(every road both sides, and those) or 'none' (the default: no walkways, crossings or paths). "
                    "Cycle lanes as mapped.",
     "parameters": _object({"road_classes": {"type": "string", "enum": ["main", "all"]},
                            "footpaths": {"type": "string", "enum": ["tagged", "all", "none"]},
                            "main_lanes": {"type": "array", "items": {"type": "integer"}, "description":
                                           "[fewest, most] lanes per direction, 1-6, for main roads (motorway to "
                                           "tertiary): OSM's lanes, or its class default, held in that range. "
                                           "Only when the user gives lanes."},
                            "local_lanes": {"type": "array", "items": {"type": "integer"}, "description":
                                            "The same for local streets (unclassified, residential)."},
                            "width_m": {"type": "number"}, "height_m": {"type": "number"},
                            "road_colour": {"type": "string"}, "colour_tolerance": {"type": "number"},
                            "resolution_m": {"type": "number"}})},
    {"name": "generate_grid", "edits": True,
     "description": "Replace the whole network with a new abstract grid (undoable); for a real place's roads use "
                    "place_map then network_from_background instead. \"6 by 5\" or \"6x5\" is 6 columns "
                    "(vertical 6) and 5 rows (horizontal 5); its size is (vertical - 1) x spacing_m wide and "
                    "(horizontal - 1) x spacing_m tall; for a size in metres give width_m and height_m instead. "
                    "signal_method: how its signals are timed (the network's "
                    "default; demand retimes them at Run). Omitted settings keep the current ones.",
     "parameters": _object({"horizontal": {"type": "integer", "description": "Rows: roads running east-west, 1 to 20."},
                            "vertical": {"type": "integer", "description": "Columns: roads running north-south, 1 to 20."},
                            "spacing_m": {"type": "number", "minimum": 20, "maximum": 2000,
                                          "description": "Metres between neighbouring roads."},
                            "driving_side": {"type": "string", "enum": ["left", "right"]},
                            "speed_limit_kmh": {"type": "number"}, "lanes_per_direction": {"type": "integer"},
                            "lane_changing": {"type": "boolean"},
                            "signal_method": {"type": "string", "enum": list(SIGNAL_METHODS)},
                            "width_m": {"type": "number", "description": "A size in metres instead of horizontal and "
                                        "vertical: the rows and columns are worked out (blocks widened to fit 20)."},
                            "height_m": {"type": "number"},
                            "footpaths": {"type": "boolean", "description": "Pedestrian walkways (footpaths) on both "
                                          "sides of every road; false or omitted: none. Ask the user if not said."}})},
]
EDIT_TOOLS = {tool["name"] for tool in TOOLS if tool["edits"]}
OD_TOOLS = {"read_od_matrix", "set_od_demand", "shape_daily_curve", "generate_demand", "scale_demand", "calibrate_demand",
            "equilibrate_demand",
            "choose_modes"}  # the Demand Generator runs them
RUN_TOOLS = {"run_simulation", "run_experiment", "read_results", "compare_runs", "set_extension", "read_extensions",
             "junction_los", "weaving_los", "metro_report", "diagnose_signal", "check_run_health", "preflight_check", "resilience",
             "emissions_report",
             "power_analysis", "rank_scenarios", "test_agent_compliance", "read_decisions", "decision_sensitivity",
             "parameter_sensitivity",
             "design_audit", "accessibility_report", "benefit_cost",
             "schedule_emergency", "start_batch", "replay_run", "analyse_run",
             "check_calibration", "read_detectors", "analyse_safety", "forecast", "critical_links", "regress",
             "assign_routes", "optimise_signals", "build_scenario", "institution_mode_split"}  # runs: the window
SCENARIO_TOOLS = {"set_signal_timing", "set_junction_control", "update_roads", "add_junction", "add_road",
                  "add_boundary_end", "move_junctions", "delete_objects", "build_interchange", "add_slip_road",
                  "set_end_flows", "add_bus_stop", "update_bus_stop", "update_institution",
                  "add_crossing", "set_pedestrian_demand",
                  "add_metro_line", "update_metro_line", "set_metro_demand"}  # what an experiment's scenario may change
TYPE_TOOLS = {"list_vehicle_types", "create_vehicle_types", "update_vehicle_type"}  # Mode Behavior's types: the window
UI_TOOLS = {"get_selection", "show_on_map", "read_traffic", "note_plan", "find_tools", *OD_TOOLS, *TYPE_TOOLS, *RUN_TOOLS}  # need the window


def tool_specs() -> list[dict]:
    """Name, description and parameters of every tool, for the model."""
    return [{key: tool[key] for key in ("name", "description", "parameters")} for tool in TOOLS]
