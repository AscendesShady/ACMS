"""Tools the System 2 assistant calls on the live network (no Qt).

Read tools describe the network. Edit tools change it through the same model
methods the editor uses; the chat (ai_mode.py) runs them only after the user
approves each one, and a failed edit is rolled back completely. Network design
only: nothing here simulates vehicles or signals.

Three tools build things wholesale, still only through these checked paths: generate_demand fills
the OD matrix by a gravity model (demand_plan, applied by the Demand Generator's controls),
deploy_institutions places buildings at random on clear ground (institutions.place), and
network_from_background replaces the network with the background's roads (background_network.py).
Their slow or random part happens first in `prepare`, off the UI thread, so the check and the
change the user approves agree.
"""

from __future__ import annotations

import ast
import difflib
import heapq
import json
import random
import re
import time
from collections import defaultdict
from copy import deepcopy
from math import ceil, cos, exp, hypot, isfinite, log, radians, sin, sqrt
from pathlib import Path

from . import background_network
from .daily_profile import ARRIVAL_PATTERNS
from .design_checks import analyse_network, network_indices, roads_at, route_lengths, tightest
from .geometry import distance_to_segment, polyline_length
from .institutions import KINDS, WALK, catchment, door, footprint_problem, hourly_people, links, place, shares
from .models import CROSSING_KINDS, MAX_WALK_DEMAND
from .metro import METRO, METRO_WALK_M, WALK_KMH, door as metro_door, places as metro_places, ride as metro_ride
from .interchanges import (build_diamond, build_slip_road, build_trumpet, curve_speed_kmh, fit_slip_roads, guess_stem,
                           pair_legs)
from . import map_tiles
from .map_tiles import Frame, mercator
from .models import BUS_STOP_DIRECTIONS, MAX_BOARDING_PER_H, SIGNAL_METHODS, Network, Road
from .network_generator import generate_network
from .roundabouts import MAX_ENTRY_LANES, design_roundabout
from .lane_graph import Graph
from . import scenario, stats
from .stats import geh
from .vehicle import VEHICLE_CLASSES

ROOT = Path(__file__).resolve().parents[2]
LIST_LIMIT = 60  # rows per list; filters narrow bigger networks (150 rows of roads were ~25k characters a call)
READ_LINES = 150  # lines per read_source call at most...
READ_CHARS = 6000  # ...and characters: sources.md's table rows are long (150 of its lines were 63k characters)
FIND_HITS = 25  # matching lines a read_source find returns
CONTROLS = ("signalised", "roundabout", "uncontrolled")
# The background image's road pixels, supplied by the window (background.BackgroundPainter.road_mask), so this
# module stays free of Qt: (background, colour, tolerance, metres a pixel) -> (width, height, mask, affine, metres).
image_mask = None
_PREPARED: dict = {}  # slow inputs by what they depend on: OSM road data, an image's traced roads (made, value)


class ToolError(Exception):
    """Why a tool couldn't run; the model gets this text back and can correct itself."""


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
                                                                    "percent": {"type": "number"}}}}})},
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
     "description": "The metro lines' loads at the design hour (the hour most ride): the metro's own OD matrix (station "
                    "to station, M1->M2) and the buildings' metro riders between their stations, on their line (no "
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
     "description": "Test an agent-decided extension (TSP or DEVL, as its tab chooses the model) on a fixed battery: "
                    "the network as it is, 30 % more demand, heavy rain and a lane closed on the longest road (when it "
                    "has 2+ lanes; the result lists the conditions run), each on `seeds` (default "
                    "1, 2) for duration_min (default 10); with DEVL, emergency vehicles every 4 minutes. Every run is "
                    "checked: no signal arm starved, no emergency vehicle held over 30 s, no gridlock, no agent "
                    "errors. Returns the pass rate and each failure with its evidence.",
     "parameters": _object({"extension": {"type": "string"}, "seeds": {"type": "array", "items": {"type": "integer"}},
                            "duration_min": {"type": "number"}}, ("extension",))},
    {"name": "read_decisions", "edits": False,
     "description": "The agent's decisions in a run (id; the latest if left out), TSP's and DEVL's: when asked, the "
                    "junction, who decided, the answer, latency, the outcome (served, not needed, blocked: a hindsight "
                    "check), the telemetry it read and the person-delay estimate. Filter by junction, from_min, to_min; "
                    "limit rows (default 20).",
     "parameters": _object({"id": {"type": "string"}, "junction": {"type": "string"}, "from_min": {"type": "number"},
                            "to_min": {"type": "number"}, "limit": {"type": "integer"}})},
    {"name": "decision_sensitivity", "edits": True,
     "description": "What the TSP agent relies on: a sample (default 8) of a run's TSP decisions re-asked to the model "
                    "deciding now, each with one fact changed: no_passengers, exit_full, no_cross_queues, "
                    "arrives_on_green, no_estimate; how often each flips the answer, and how often the same question "
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
     "description": "Start Batch Run: every seed (a list, like [1, 2, 3, 4]) runs duration_min simulated "
                    "minutes, headless, against the baselines (never, always preempt). test_devl: the emergency vehicle schedules "
                    "dispatched in each run, the DEVL tab's models deciding the lanes; test_tsp: the TSP tab's "
                    "models deciding bus priority. Pick those models, the pace, runs at once and times each with "
                    "set_extension first (read_extensions shows them). Saves each run's workbook and a batch "
                    "workbook comparing the models; it takes as long as the Batch Run tab says.",
     "parameters": _object({"seeds": {"type": "array", "items": {"type": "integer"}, "description": "e.g. [1, 2, 3, 4]"},
                            "duration_min": {"type": "integer"},
                            "test_tsp": {"type": "boolean"}, "test_devl": {"type": "boolean"}},
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
                    "never take the metro (it has its own OD matrix). nests make it a nested logit. Also returns "
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
                    "fixed: the green_times_s given), phases 2 or 4, green_times_s "
                    "(one a phase, for fixed), cycle_min_s and cycle_max_s (20-240, the coordinated methods), "
                    "target_saturation_percent (50-110, SCATS and SCOOT), stop_weight_s (TRANSYT and SCOOT). Give "
                    "only what changes. The junctions must be signalised (set_junction_control).",
     "parameters": _object({"junctions": _IDS, "method": {"type": "string", "enum": list(SIGNAL_METHODS)},
                            "phases": {"type": "integer", "description": "2 or 4"},
                            "green_times_s": {"type": "array", "items": {"type": "number"},
                                              "description": "seconds, one a phase; given alone they mean method fixed"},
                            "cycle_min_s": {"type": "number"}, "cycle_max_s": {"type": "number"},
                            "target_saturation_percent": {"type": "number"}, "stop_weight_s": {"type": "number"}},
                           ("junctions",))},
    {"name": "place_map", "edits": True,
     "description": "Put OpenStreetMap under the network: a `place` found by name (Nominatim; the first match), or "
                    "lat and lon; rotation_deg turns the map. That place goes under the network's centre; then "
                    "network_from_background can build its roads.",
     "parameters": _object({"place": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"},
                            "rotation_deg": {"type": "number"}})},
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
                    "cycle_lanes 'both' (a bikes-only lane each way) or 'none'.",
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
                            "cycle_lanes": {"type": "string", "enum": ["both", "none"]}}, ("roads",))},
    {"name": "add_crossing", "edits": True,
     "description": "Put a pedestrian crossing across a road with footpaths on both sides, at_m from its from-junction "
                    "(the middle if not given), at least 20 m from either junction: kind 'zebra' (walkers have right of "
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
     "description": "Set people walking an hour between boundary ends (pairs like '1W->1E') or between spawn "
                    "points ('S1->S2'; add_spawn_points); 0 removes a pair. They walk the footpaths and paths in a run; "
                    "institutions' walkers come on top.",
     "parameters": _object({"pairs": _IDS, "people_per_hour": {"type": "number"}}, ("pairs", "people_per_hour"))},
    {"name": "add_spawn_points", "edits": True,
     "description": "Place pedestrian spawn points S1, S2... at points [[x, y], ...] metres, anywhere near the "
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
    {"name": "delete_objects", "edits": True,
     "description": "Delete roads, junctions (with their roads), boundary ends, bus stops or institutions.",
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
                    "With metro lines, the metro stations of its door station's line (the nearest within 800 m) inside "
                    "its circle are places its people come from too, as the junctions are: there they choose \"Metro\" "
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
                    "road data (not the picture) for width_m x height_m round the network's centre (default "
                    "1200 x 1200, at most 4000), with lanes, speed limits, one-way streets, roundabouts and "
                    "bridges as mapped, and junctions signalised where OSM has a junction signal (a pedestrian "
                    "crossing's, an emergency or a flashing signal leaves a priority junction; OSM holds no signal "
                    "timings, so each starts on Webster); road_classes 'main' (motorways to secondary roads) or 'all' (down to "
                    "residential streets, the default). Image: the pixels of road_colour ('#rrggbb'; ask the "
                    "user if you don't know it) within colour_tolerance (default 40), traced to centre lines at "
                    "resolution_m metres a pixel (default 2); lanes from their width. Roads leaving the area "
                    "become boundary ends. Institutions still on clear ground stay. Walking (the map): footpaths "
                    "'tagged' (as OSM's sidewalk tags and sidewalk ways say; the default), 'all' (every road both "
                    "sides) or 'none'; zebra crossings, footbridges (overbridges), paths and cycle lanes as mapped.",
     "parameters": _object({"road_classes": {"type": "string", "enum": ["main", "all"]},
                            "footpaths": {"type": "string", "enum": ["tagged", "all", "none"]},
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
                            "height_m": {"type": "number"}})},
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
                  "add_crossing", "set_pedestrian_demand"}  # what an experiment's scenario may change (a zebra: what if)
TYPE_TOOLS = {"list_vehicle_types", "create_vehicle_types", "update_vehicle_type"}  # Mode Behavior's types: the window
UI_TOOLS = {"get_selection", "show_on_map", "read_traffic", "note_plan", "find_tools", *OD_TOOLS, *TYPE_TOOLS, *RUN_TOOLS}  # need the window


def tool_specs() -> list[dict]:
    """Name, description and parameters of every tool, for the model."""
    return [{key: tool[key] for key in ("name", "description", "parameters")} for tool in TOOLS]


# --- Which tools a request is offered -----------------------------------------------------------
# A small model picks worse among many tools, and each tool's description costs prompt tokens (all of them: about
# 16k). Offering only the tools a request needs raised local models' success at function calling and cut their time
# (Paramanayakam et al., "Less is More: Optimizing Function Calling for LLM Execution on Edge Devices", DATE 2025).
# So each request gets the core tools, the groups its words point to and the tools the conversation has used, and
# find_tools adds any other mid-answer.
FIND_TOOLS = "find_tools"
CORE_TOOLS = ("network_summary", "describe", "list_roads", "list_junctions", "show_on_map", "get_selection",
              "note_plan", FIND_TOOLS)
TOOL_GROUPS = {  # name: (words that point to it, as regex stems; its tools)
    "build": (r"build|city|cities|town|grid|network|replac|new\b|block|layout|dense|road|street|lane|"
              r"junction|intersection|roundabout|interchange|slip|pocket|delete|remov|move|connect|\d+\s*m\b|\d+\s*km\b",
              ("generate_grid", "add_junction", "add_road", "update_roads", "delete_objects", "move_junctions",
               "add_boundary_end", "set_junction_control", "build_interchange", "add_slip_road")),
    "place": (r"\bmap\b|openstreetmap|\bosm\b|background|image|picture|real\b|district|neighbo|suburb",
              ("place_map", "network_from_background")),
    "metro": (r"metro|rail|subway|underground|\btube\b|train|station|transit|ridership|crowd",
              ("metro_report", "choose_modes", "institution_mode_split")),
    "freeway": (r"weav|freeway|motorway|highway|ramp|merge|diverg|auxiliary|interchange",
                ("weaving_los", "design_audit", "build_interchange", "add_slip_road")),
    "demand": (r"dema?nd|traffic|trip|\bod\b|matrix|flow|bus(?:y|ier|iest)|heav|light\b|quiet|peak|rush|satur|veh|"
               r"hourly|volume|gravity|car\b|cars\b|more\b|less\b|fewer|increas|decreas|double|halve|half|grow|populat",
               ("read_od_matrix", "generate_demand", "set_od_demand", "scale_demand", "set_end_flows")),
    # the demand models: each spec is long (choose_modes 2.5k characters), so only words about them offer them
    "demand_models": (r"\bmodes?\b|mode split|logit|share|fare|calibrat|\bcounts?\b|elastic|induced|equilibr|daily|"
                      r"\bday\b|curve|profile|24\s*h|weekday",
                      ("shape_daily_curve", "calibrate_demand", "choose_modes", "equilibrate_demand")),
    # a whole study in one call; offered when a request has several parts (STUDY_GROUPS) or says it is one
    "study": (r"stud(?:y|ies)|scenario builder|end.to.end|from scratch", ("build_scenario",)),
    "institutions": (r"institution|instituition|building|school|hospital|office|shop|home|house|resident|people|"
                     r"populat|dense|land use|employ|catchment|block",
                     ("list_institutions", "deploy_institutions", "place_institution", "update_institution",
                      "institution_mode_split")),
    "run": (r"simulat|\brun|test|result|perform|throughput|delay|speed|queue|slow|congest|jam|how (?:well|bad|good)|"
            r"peak|busiest|worst|live",
            ("run_simulation", "read_results", "analyse_run", "check_run_health", "read_traffic", "compare_runs")),
    "signals": (r"signal|traffic light|timing|phase|cycle|green|webster|transyt|synchro|scats|scoot|actuat|pressure|"
                r"slow|\blos\b|level of service|optimi",
                ("set_signal_timing", "set_junction_control", "junction_los", "diagnose_signal", "optimise_signals")),
    "experiment": (r"compar|what if|would|does|\bif\b|scenario|experiment|versus|\bvs\b|better|worse|cut|reduc|"
                   r"improv|effect|seed|alternativ|option|worth|cost|benefit|rank|power|robust|weather|rain|incident",
                   ("note_plan", "run_experiment", "read_results", "compare_runs", "rank_scenarios", "power_analysis",
                    "benefit_cost", "assign_routes", "preflight_check")),
    "analysis": (r"analy|safe|crash|conflict|detector|forecast|predict|calibrat|\bcounts?\b|validat|observed|regress|emission|"
                 r"co2|fuel|resilien|"
                 r"incident|critical|vulnerab|index|indices|design|sight|audit|access|reach|equity|gini|why|reliab|"
                 r"sensitiv|elastic|parameter|"
                 r"route|cluster",
                 ("analyse_network", "analyse_run", "read_detectors", "analyse_safety", "forecast", "check_calibration",
                  "regress", "emissions_report", "resilience", "junction_los", "diagnose_signal", "critical_links",
                  "network_indices", "design_audit", "accessibility_report", "cluster_conditions", "assign_routes",
                  "parameter_sensitivity")),
    "pedestrians": (r"pedestri|walk|footpath|sidewalk|pavement|zebra|crossing|overbridge|footbridge|cycle lane|"
                    r"bike lane|cyclist|\bpath",
                    ("pedestrian_summary", "add_crossing", "add_path", "set_pedestrian_demand", "add_spawn_points",
                     "update_roads",
                     "delete_objects")),
    "transit": (r"\bbus(?:es)?\b|bus stop|\bstops?\b|transit|passenger|vehicle type|truck|lorry|bike|bicycle|taxi|van\b|fleet|headway|tram",
                ("add_bus_stop", "update_bus_stop", "list_vehicle_types", "create_vehicle_types",
                 "update_vehicle_type")),
    "extensions": (r"extension|\btsp\b|priority|devl|emergenc|ambulance|fire|police|batch|replay|agent|decision|"
                   r"dispatch|research log|complian|sensitiv",
                   ("read_extensions", "set_extension", "schedule_emergency", "start_batch", "replay_run",
                    "test_agent_compliance", "read_decisions", "decision_sensitivity")),
    "docs": (r"\bcode\b|source|function|module|formula|implement|where does|how (?:do|can) i\b|how to\b|readme|"
             r"setup|set up|install|api key|\bkeys?\b|button|menu|shortcut|undo", ("read_source",)),
}
# Capitalised words that aren't place names (a request naming a place gets the map's tools)
_NOT_PLACES = {"Webster", "Synchro", "Transyt", "Scats", "Scoot", "Krauss", "Poisson", "Excel", "Ollama", "Grok",
               "Gemini", "Laya", "Jev", "Research", "Log", "Batch", "Run", "Apply", "Skip", "Dispatch", "Summary",
               "Demand", "Generator", "Mode", "Behavior", "Extensions", "Institutions", "Junction", "Road", "Can",
               "Please", "Also", "Then", "And", "But", "The", "What", "How", "Why", "Which", "Create", "Build", "Make"}


def _places_named(request: str) -> bool:
    """Whether the request names somewhere: a capitalised word that doesn't start a sentence and isn't the app's."""
    for match in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,})", request.strip()):
        if match.group(1) not in _NOT_PLACES:
            return True
    return False


STUDY_GROUPS = {"build", "place", "demand", "run", "signals", "experiment"}  # 3 or more of them: a whole study


def tool_groups(text: str) -> list[str]:
    """The groups whose words the text uses (TOOL_GROUPS), "place" when it names somewhere, and "study" when it
    touches three or more of STUDY_GROUPS or both builds and tests (build it, run it: build_scenario's kind)."""
    lowered = text.lower()
    found = [name for name, (pattern, _) in TOOL_GROUPS.items() if re.search(pattern, lowered)]
    if "place" not in found and _places_named(text):
        found.append("place")
    builds, tests = {"build", "place"} & set(found), {"run", "experiment"} & set(found)
    if "study" not in found and (len(STUDY_GROUPS.intersection(found)) >= 3 or builds and tests):
        found.append("study")
    return found


def tools_for(request: str, used=()) -> list[dict]:
    """The tool specs a request is offered: CORE_TOOLS, the tools of the groups its words point to, any tool it
    names, and `used` (tools the conversation has called, so a follow-up keeps them), in TOOLS order."""
    lowered = request.lower()
    wanted = set(CORE_TOOLS) | set(used)
    for group in tool_groups(request):
        wanted |= set(TOOL_GROUPS[group][1])
    wanted |= {tool["name"] for tool in TOOLS if tool["name"] in lowered or tool["name"].replace("_", " ") in lowered}
    return [spec for spec in tool_specs() if spec["name"] in wanted]


def contradicts(name: str, args: dict, request: str) -> str | None:
    """Why a call goes against the user's own words, or None: a signal method (an argument named method or
    *_method) the request doesn't name while it names another (qwen3.5 4B, asked for SCOOT, thought SCOOT and sent
    scats). Only methods: their names are alike and none is an everyday word. And an invented grid for a request that
    names a real place and no grid (llama3.2 3B, asked for Dhanmondi's roads, placed the map, then built a 5 x 6 grid)."""
    tool = next((tool for tool in TOOLS if tool["name"] == name), None)
    if tool is None or not isinstance(args, dict):
        return None
    words = request.lower().replace("-", " ")
    if name == "delete_objects":  # a road the user named, its two junctions deleted instead (with every road at them)
        ids = [str(item).strip() for item in _ids(args.get("ids") or [])]
        for road in re.findall(r"[\w.]+,[\w.]+-[\w.]+,[\w.]+|N\d+-N\d+", request):
            ends = road.split("-", 1) if road.startswith("N") else re.split(r"-(?=[\w.]+,)", road, maxsplit=1)
            rest = request.replace(road, " ")  # a junction the user names apart from the road is theirs to delete
            named = [end for end in ends if re.search(rf"(?<![\w.,-]){re.escape(end)}(?![\w.,-])", rest)]
            if road not in ids and len(ends) == 2 and set(ends) <= set(ids) and not named:
                return (f"The user asked to delete road {road}, but this deletes its junctions {ends[0]} and {ends[1]} "
                        f"(and every road at them). Call again with ids [\"{road}\"].")
    if name == "generate_grid" and _places_named(request) and "grid" not in words:
        return ("The user asked for a real place's roads, not an invented grid: generate_grid would put a made-up grid "
                "in its place. Use place_map (if the map isn't there yet), then network_from_background; or tell the "
                "user why that can't be done.")
    for key, spec in tool["parameters"].get("properties", {}).items():
        value = args.get(key)
        if not (key == "method" or key.endswith("_method")) or "enum" not in spec or not isinstance(value, str):
            continue
        named = [c for c in spec["enum"] if re.search(rf"\b{re.escape(str(c).replace('_', ' '))}\b", words.replace("_", " "))]
        if named and value.lower() not in [str(c).lower() for c in named]:
            return (f"The user asked for {', '.join(map(str, named))}, but this call sets {key} {value!r}. Call again "
                    f"with what they asked for, or say why not.")
    return None


ENDS_ARGS = ("pairs", "origins", "destinations")  # the demand tools' end names


# One incident's fields: experiments.INCIDENT_KEYS (experiments imports from here, never here from it)
INCIDENT_FIELDS = ("road", "lanes", "at_min", "for_min", "from_m", "length_m", "direction", "speed_kmh")


def fold_incident(name: str, args) -> str | None:
    """An incident's fields sent beside run_simulation's own arguments instead of in `incidents` (llama3.2 3B, asked
    to close a lane for 10 minutes, sent lanes, at_min, for_min… at the top and was refused): made the one incident
    they describe. Changes `args`; returns a note for the result, or None."""
    if name != "run_simulation" or not isinstance(args, dict) or args.get("incidents") not in (None, "", [], "[]") \
            or not any(key in args for key in ("lanes", "at_min", "for_min")):
        return None
    incident = {key: args.pop(key) for key in INCIDENT_FIELDS if key in args}
    args["incidents"] = [incident]
    return f"the incident fields given beside run_simulation's were read as one incident: {incident}"


def drop_strays(name: str, args) -> str | None:
    """Drop arguments another tool takes and `name` doesn't (llama3.2 3B sent set_od_demand's peak_hour_factor to
    generate_demand on every try, though each refusal named its owner): the call runs on what it does take.
    Changes `args`; returns a note for the result, or None."""
    tool = next((tool for tool in TOOLS if tool["name"] == name), None)
    if tool is None or not isinstance(args, dict):
        return None
    properties = tool["parameters"]["properties"]
    strays = {key: [t["name"] for t in TOOLS if key in t["parameters"]["properties"]][:2] for key in list(args)
              if key not in properties and _parameter_for(str(key), properties) is None}
    strays = {key: owners for key, owners in strays.items() if owners}
    for key in strays:
        args.pop(key)
    return "; ".join(f"{key} is {' or '.join(owners)}'s, not {name}'s, so it was left out"
                     for key, owners in strays.items()) or None


def repair_ends(network: Network, name: str, args, request: str) -> str | None:
    """Drop end names that are neither in the network nor in the user's request, when every one given is such:
    the model copied them from an example ("1W" on an OpenStreetMap network whose ends are 2N, 9.5E...; llama3.2 3B,
    asked for the peak hour), so it meant every end. Changes `args`; returns a note for the result, or None (a
    real end, or one the user named, is left for the tool to check)."""
    if not isinstance(args, dict) or not any(args.get(key) for key in ENDS_ARGS) or \
            not any(key in tool["parameters"]["properties"] for tool in TOOLS if tool["name"] == name for key in ENDS_ARGS):
        return None
    given = set()
    for key in ENDS_ARGS:
        value = args.get(key)
        items = value if isinstance(value, list) else [value] if value else []
        for item in items:
            given.update(part.strip() for part in re.split(r"\s*(?:-+>*|=+>|→|>+|\bto\b|,)\s*", str(item))
                         if part.strip(" []") and part.strip().lower() not in _NOTHING)  # "None": left out, no end
    words = request.lower()
    if not given or any(end in network.approach_nodes or end.lower() in words for end in given):
        return None
    dropped = {key: args.pop(key) for key in ENDS_ARGS if key in args}
    return (f"{', '.join(sorted(given))} {'is' if len(given) == 1 else 'are'} no end of this network and the user "
            f"named none, so every end was used "
            f"(dropped {', '.join(dropped)}). Its ends: {', '.join(sorted(network.approach_nodes)[:20])}…")


def expand_tools(args: dict, offered: list[dict]) -> dict:
    """find_tools: the tools matching `need` (by TOOL_GROUPS' words, then by words of their names and descriptions)
    that aren't offered yet, added to `offered` (the list the model is sent, so it can call them next)."""
    need = str((args or {}).get("need") or "").strip()
    if not need:
        return {"error": "Say what you need, e.g. need: \"bus stops\" or \"safety of a run\"."}
    have = {spec["name"] for spec in offered}
    names = {name for group in tool_groups(need) for name in TOOL_GROUPS[group][1]}
    if not names:
        words = {w for w in re.findall(r"[a-z]{4,}", need.lower())}
        names = {tool["name"] for tool in TOOLS
                 if words & set(re.findall(r"[a-z]{4,}", (tool["name"].replace("_", " ") + " " + tool["description"]).lower()))}
    added = [spec for spec in tool_specs() if spec["name"] in names - have]
    offered.extend(added)
    if not added:
        return {"found": sorted(names & have), "note": "Nothing new: the tools for that are already yours to call."
                if names else "No tool matches. Groups: " + ", ".join(TOOL_GROUPS) + "."}
    return {"added": [{"name": spec["name"], "does": spec["description"].split(". ")[0]} for spec in added],
            "note": "Call them now as you need them."}


# --- Helpers -----------------------------------------------------------------------------

def _ids(value) -> list[str]:
    """A list of IDs; small models sometimes send the list as JSON text, or as "2,2; 3,3" (never split on commas:
    ids hold them)."""
    if isinstance(value, str) and value.strip().startswith("["):
        value = _parsed_json(value.strip())  # JSON, repaired, or Python's ['3,3']
    if isinstance(value, str) and ";" in value:
        value = [part.strip() for part in value.split(";") if part.strip()]
    items = value if isinstance(value, list) else [value]
    if any(isinstance(item, dict) for item in items):  # qwen3.5 4B sent [{"road": ..., "speed_limit_kmh": 30}, ...]
        raise ValueError("Ids are text like '2,2' or '1,1-2,1', not objects: give the ids alone, the settings as "
                         "the tool's own arguments, and call the tool once for each different setting.")
    return [str(item) for item in items]


def _parsed_json(value):
    """A list or object a small model sent as JSON text (repaired if it can be: _repaired_json), else the text."""
    try:
        return _repaired_json(value)
    except ValueError:
        return value


def _repaired_json(text: str):
    """JSON text as a small model writes it: as is, else with trailing commas dropped and the brackets it left open
    closed (qwen3.5 4B sent a run_experiment's scenarios one } short). ValueError (json's, with its position) when
    even that can't be read."""
    try:
        return json.loads(text)
    except ValueError as exc:
        first = exc
    stack, quoted, escaped = [], False, False
    for char in text:
        if quoted:
            escaped, quoted = (False, quoted) if escaped else (char == "\\", char != '"')
        elif char == '"':
            quoted = True
        elif char in "[{":
            stack.append("]" if char == "[" else "}")
        elif char in "]}" and stack and stack[-1] == char:
            stack.pop()
    fixed = re.sub(r",\s*([\]}])", r"\1", text.rstrip().rstrip(",") + "".join(reversed(stack)))
    try:
        return json.loads(fixed)
    except ValueError:
        pass
    try:  # Python's own spelling, "['3,3']" (llama3.2 3B): literals only, nothing run
        found = ast.literal_eval(text.strip())
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        raise first from None
    try:  # JSON's kinds only: '[{"name" "a"}]' is Python's list of a set, not what was meant
        json.dumps(found)
    except (TypeError, ValueError):
        raise first from None
    if not isinstance(found, (list, dict)):
        raise first
    return found


def _int(value) -> int:
    return int(float(value))  # "3", 3.0 and 3 all mean 3


def _bool(value) -> bool:
    return value.strip().lower() in ("true", "yes", "1") if isinstance(value, str) else bool(value)


def _road_info(network: Network, road: Road) -> dict:
    info = {"id": road.id, "from": road.from_node, "to": road.to_node, "length_m": round(road.length_m, 1),
            "lanes_per_direction": road.lanes_per_direction, "direction": road.direction,
            "speed_limit_kmh": road.speed_limit_kmh, "lane_changing": road.lane_changing}
    if road.road_type != "urban":
        info["road_type"] = road.road_type
    if road.overpass:
        info["overpass"] = True
    if road.single_lane:
        info["single_lane"] = True
    if road.pockets:
        info["turn_pockets"] = road.pockets
    if road.cycle_lanes:
        info["cycle_lanes_to"] = road.cycle_lanes  # bikes only, for the traffic arriving at these ends
    if road.footpaths:
        info["footpaths"] = road.footpaths  # its sides, from from_node to to_node
    crossings = [{"id": c.id, "kind": c.kind, "at_m": round(c.distance_m)} for c in network.pedestrian_crossings.values()
                 if c.road == road.id]
    if crossings:
        info["pedestrian_crossings"] = crossings
    if road.id in network.approach_roads:
        info["boundary_road"] = True
    if tightest(road):
        info["tightest_radius_m"] = round(1 / tightest(road), 1)
    return info


def _design(network: Network, node_id: str) -> dict:
    design = design_roundabout(network, node_id)
    return {"category": design.category, "entry_lanes": design.entry_lanes,
            "design_speed_kmh": design.design_speed_kmh,
            "inscribed_diameter_m": round(design.inscribed_diameter_m, 1),
            "circulatory_width_m": round(design.circulatory_width_m, 1),
            "island_diameter_m": round(design.island_diameter_m, 1),
            "circulating_speed_kmh": round(design.circulating_speed_kmh),
            "source": "NCHRP Report 672 (roundabouts.py)"}


def _limited(rows: list[dict], what: str) -> dict:
    result = {what: rows[:LIST_LIMIT], "count": len(rows)}
    if len(rows) > LIST_LIMIT:
        result["note"] = f"Showing {LIST_LIMIT} of {len(rows)}; filter to see the rest."
    return result


def edge_crossings(network: Network, start: str, target: str | None, x: float = 0.0, y: float = 0.0) -> list:
    """Roads a planned edge would cross (see Network.crossings); ValueError if it can't be built."""
    if start not in network.nodes:
        raise ValueError(f"New roads start from a junction; {start} isn't one.")
    begin = network.nodes[start]
    if target is None:
        if hypot(x - begin.x, y - begin.y) < 1.0:
            raise ValueError("Place the end away from its intersection.")
        finish = (x, y)
    else:
        problem = network.connection_problem(start, target)
        if problem:
            raise ValueError(problem)
        finish = (network.node(target).x, network.node(target).y)
    return network.crossings([(begin.x, begin.y), finish], {start} | ({target} if target else set()))


def build_edge(network: Network, start: str, target: str | None, x: float = 0.0, y: float = 0.0,
               crossing: str | None = None) -> tuple[Road, list[str]]:
    """Add a road from junction `start` to junction `target`, or to a new boundary end at (x, y).

    Roads it would cross: crossing="roundabout" puts a roundabout junction at each (both
    roads split there), "overpass" bridges over them; anything else refuses. Everything is
    checked before any road changes. Returns the new road (the last one, when roundabouts
    split it) and the roundabouts added.
    """
    crossings = edge_crossings(network, start, target, x, y)
    if crossings and crossing not in ("roundabout", "overpass"):
        roads = sorted({road_id for *_, road_id in crossings})
        raise ValueError(f"The new road would cross {', '.join(roads)}. Choose crossings='roundabout' "
                         "(a roundabout at each crossing) or 'overpass' (bridge over them).")
    if crossings and crossing == "roundabout":  # each roundabout's widest entry: the new road's or a crossed road's
        lanes = max(network.default_lanes_per_direction, *(network.road(road_id).lanes_each_way for *_, road_id in crossings))
        if lanes > MAX_ENTRY_LANES:
            raise ValueError(f"A roundabout with {lanes} lanes entering from one road is more than NCHRP Report 672 "
                             f"designs (up to {MAX_ENTRY_LANES}). Choose crossings='overpass'.")
    junctions = []
    if crossings and crossing == "roundabout":
        for _, cross_x, cross_y, road_id in crossings:
            junction = network.add_node(cross_x, cross_y)
            junction.control = "roundabout"
            network.split_road(road_id, junction.id)
            junctions.append(junction.id)
    chain = [start, *junctions]
    for a, b in zip(chain, chain[1:]):
        network.add_road(a, b)
    road = network.add_end(chain[-1], x, y) if target is None else network.add_road(chain[-1], target)
    road.overpass = bool(crossings) and crossing == "overpass"
    return road, junctions


# --- Read tools --------------------------------------------------------------------------

def network_summary(network: Network) -> dict:
    points = [(node.x, node.y) for node in network.all_nodes()]
    ends = []
    for end in network.approach_nodes.values():
        road = next(iter(roads_at(network, end.id)), None)
        traffic = "in and out" if road is None or road.direction == "two-way" else \
            "in" if road.from_node == end.id else "out"
        ends.append({"id": end.id, "traffic": traffic, "supply_vph": end.supply_vph, "demand_vph": end.demand_vph})
    return {
        "coordinates": "metres; x to the right, y downward; grid junction '1,1' is bottom-left",
        "driving_side": network.driving_side,
        "grid": f"{network.horizontal_corridors} horizontal x {network.vertical_corridors} vertical corridors, "
                f"{network.default_spacing:g} m apart",
        "defaults": {"speed_limit_kmh": network.default_speed_limit_kmh,
                     "lanes_per_direction": network.default_lanes_per_direction,
                     "lane_changing": network.default_lane_changing,
                     "signal_method": network.default_signal_method},
        "junctions": {control: [node.id for node in network.nodes.values() if node.control == control]
                      for control in CONTROLS},
        "roads": len(network.roads),
        "one_way_roads": sum(road.direction == "one-way" for road in network.all_roads()),
        "curved_roads": sum(bool(road.shape) for road in network.all_roads()),
        "boundary_ends": ends,
        "institutions": len(network.institutions),
        "extent_m": {"x": [round(min(x for x, _ in points)), round(max(x for x, _ in points))],
                     "y": [round(min(y for _, y in points)), round(max(y for _, y in points))]} if points else None,
    }


def list_roads(network: Network, node: str = "", direction: str = "") -> dict:
    roads = [road for road in network.all_roads()
             if (not node or node in (road.from_node, road.to_node)) and (not direction or road.direction == direction)]
    found = _limited([_road_info(network, road) for road in roads], "roads")
    if node or direction:  # filtered: how many in all
        found["filter"] = f"{len(roads)} of the network's {len(network.roads) + len(network.approach_roads)} roads"
    return found


def list_junctions(network: Network, control: str = "") -> dict:
    rows = [{"id": node.id, "x": round(node.x, 1), "y": round(node.y, 1), "control": node.control,
             "roads": len(roads_at(network, node.id))}
            for node in network.nodes.values() if not control or node.control == control]
    found = _limited(rows, "junctions")
    if control:  # filtered: how many in all, so "0 uncontrolled" isn't read as "0 junctions" (llama3.2 3B)
        found["filter"] = f"control {control}: {len(rows)} of the network's {len(network.nodes)} junctions"
    return found


def describe(network: Network, id: str) -> dict:  # noqa: A002 - the model's argument name
    if id in network.approach_nodes:  # a boundary end and its road share the name
        end, road = network.approach_nodes[id], network.approach_roads.get(id)
        return {"id": id, "kind": "boundary end", "x": round(end.x, 1), "y": round(end.y, 1),
                "supply_vph": end.supply_vph, "demand_vph": end.demand_vph,
                "road": _road_info(network, road) if road else None}
    if id in network.roads or id in network.approach_roads:
        road = network.road(id)
        info = _road_info(network, road)
        info["curved"] = bool(road.shape)
        if tightest(road):
            info["safe_curve_speed_kmh"] = curve_speed_kmh(1 / tightest(road))
        crossed = sorted({road_id for *_, road_id in
                          network.crossings(network.road_points(road), {road.from_node, road.to_node})})
        if crossed:
            info["crosses_without_junction"] = crossed
            info["note"] = "Grade separated: " + ("this road bridges over them." if road.overpass
                                                  else "roads marked overpass pass over.")
        return info
    node = network.nodes[id]
    roads = roads_at(network, id)
    info = {"id": id, "kind": "junction", "x": round(node.x, 1), "y": round(node.y, 1), "control": node.control,
            "grid_position": "off the grid" if node.row is None else network.node_label(node),
            "roads": [{"id": road.id, "to": road.to_node if road.from_node == id else road.from_node,
                       "traffic": "two-way" if road.direction == "two-way" else
                       ("one-way out" if road.from_node == id else "one-way in"),
                       "lanes_per_direction": road.lanes_per_direction,
                       "speed_limit_kmh": road.speed_limit_kmh} for road in roads]}
    if node.control == "roundabout":
        info["roundabout_design"] = _design(network, id)
    if node.control == "signalised":
        info["signal_settings"] = {"phases": node.signal_phases, "method": node.signal_method,
                                   "green_times_s": node.green_times_s,
                                   "cycle_limits_s": [node.cycle_min_s, node.cycle_max_s],
                                   "target_saturation": node.target_saturation, "stop_weight_s": node.stop_weight_s,
                                   "note": "Each Run times it by its method (the greens entered are used by "
                                           "'fixed' only; the cycle limits by the coordinated methods, the target "
                                           "saturation by SCATS and SCOOT, the stop weight by TRANSYT and SCOOT). "
                                           "read_traffic's signal_plans give the plan a run is using."}
    return info


def read_source(network: Network, path: str = "", start_line: int = 1, end_line: int = 0, find: str = "") -> dict:
    del network  # the source, not the network
    found = {*ROOT.glob("*.md"), *ROOT.glob("*.py"), *(ROOT / "src" / "traffic_simulator").glob("*.py"),
             *(ROOT / "tests").glob("*.py"), *(ROOT / "config").glob("*.json"),
             *(ROOT / "docs" / "architecture").glob("*.md")}
    files = sorted(file.relative_to(ROOT).as_posix() for file in found)
    target = (ROOT / path).resolve() if path else None
    if target is not None and target not in found:  # also refuses anything outside the project, e.g. "../secrets"
        raise ToolError(f"Can't read {path!r}. Readable files: {', '.join(files)}")
    if find:  # the lines holding every word, so a question reads a few lines rather than whole pages
        words = str(find).lower().split()
        hits = [{"path": name, "line": number, "text": line.strip()[:200]}
                for name in ([target.relative_to(ROOT).as_posix()] if target else files)
                for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1)
                if all(word in line.lower() for word in words)]
        return {"find": find, "hits": hits[:FIND_HITS], "more": max(len(hits) - FIND_HITS, 0),
                "next": "read_source with that path, start_line a few lines before the hit and end_line ~30 after"
                if hits else "No line has all those words: try fewer or other words."}
    if not path:
        return {"files": files}
    lines = target.read_text(encoding="utf-8").splitlines()
    start = max(1, _int(start_line or 1))
    end = min(len(lines), _int(end_line or 0) or len(lines), start + READ_LINES - 1)
    shown, used = [], 0
    for number, line in enumerate(lines[start - 1:end], start):  # whole lines up to READ_CHARS; a longer one is cut
        text = f"{number}: {line}"
        if shown and used + len(text) > READ_CHARS:
            break
        shown.append(text if len(text) <= READ_CHARS else text[:READ_CHARS] + " ... (line cut)")
        used += len(shown[-1]) + 1
    end = start + len(shown) - 1
    result = {"path": target.relative_to(ROOT).as_posix(), "lines": f"{start}-{end} of {len(lines)}",
              "text": "\n".join(shown)}
    if end < len(lines) and (not end_line or end < _int(end_line)):
        result["next_start_line"] = end + 1
    return result


# --- Edit tools --------------------------------------------------------------------------

def set_junction_control(network: Network, junctions, control: str) -> dict:
    ids = _ids(junctions)
    if control not in CONTROLS:
        raise ValueError(f"control must be one of {', '.join(CONTROLS)}.")
    for node_id in ids:
        network.nodes[_junction(network, node_id)].control = control
    result = {"junctions": ids, "control": control}  # what changed, before -> after: run_tool's `changed`
    moved = [slip for node_id in ids for slip in fit_slip_roads(network, node_id)]  # clear of a roundabout's circle
    if moved:
        result["slip_roads_moved_out"] = moved
    if control == "roundabout":
        result["designs"] = {node_id: _design(network, node_id) for node_id in ids}
    return result


def update_roads(network: Network, roads, lanes_per_direction=None, speed_limit_kmh=None, direction=None,
                 lane_changing=None, road_type=None, length_m=None, add_lanes=None, pocket_junction=None,
                 pocket_lanes=None, pocket_length_m=None, pocket_side=None, footpaths=None, cycle_lanes=None) -> dict:
    ids = _ids(roads)
    if footpaths is not None and footpaths not in FOOTPATH_SIDES:
        raise ValueError(f"footpaths must be {', '.join(map(repr, FOOTPATH_SIDES))}.")
    if cycle_lanes not in (None, "both", "none"):
        raise ValueError("cycle_lanes must be 'both' or 'none'.")
    for road_id in ids:
        network.road(road_id)  # KeyError for an unknown road, before anything changes
    if add_lanes is not None and lanes_per_direction is not None:
        raise ValueError("Give add_lanes or lanes_per_direction, not both.")
    if not pocket_junction:
        if (pocket_lanes is not None and _int(pocket_lanes) > 0) or pocket_length_m is not None:
            raise ValueError("Name pocket_junction: the end of the road the turn pocket is on the approach to.")
        pocket_lanes = pocket_side = None  # 0 lanes or a side with no pocket named: a small model's filler
    if direction not in (None, "two-way", "one-way", "reverse"):
        raise ValueError("direction must be 'two-way', 'one-way' or 'reverse'.")
    if speed_limit_kmh is not None and not 5 <= float(speed_limit_kmh) <= 130:  # as the incident zones' limits
        raise ValueError("speed_limit_kmh must be 5 to 130.")
    for road_id in ids:
        if length_m is not None:
            network.set_road_length(road_id, float(length_m))
        road = network.road(road_id)
        if add_lanes is not None:  # each road its own count plus this ("add a lane")
            lanes_per_direction = road.lanes_per_direction + _int(add_lanes)
        if lanes_per_direction is not None:
            if not 1 <= _int(lanes_per_direction) <= 12:
                raise ValueError("lanes_per_direction must be 1 to 12.")
            road.lanes_per_direction = _int(lanes_per_direction)
        if speed_limit_kmh is not None:
            road.speed_limit_kmh = float(speed_limit_kmh)
        if lane_changing is not None:
            road.lane_changing = _bool(lane_changing)
        if road_type:
            road.road_type = str(road_type)
        if direction == "reverse":
            network.reverse_road(road_id)  # a curve's points and curvature flip too
        if direction:
            road.direction = "two-way" if direction == "two-way" else "one-way"
            road.pockets = {end: pocket for end, pocket in road.pockets.items() if end in road.arrival_ends()}
        if footpaths is not None:
            road.footpaths = list(FOOTPATH_SIDES[footpaths])
        if cycle_lanes is not None:
            road.cycle_lanes = list(road.arrival_ends()) if cycle_lanes == "both" else []
        road.cycle_lanes = [end for end in road.cycle_lanes if end in road.arrival_ends()]
        road.single_lane = road.single_lane and road.direction == "one-way" and road.lanes_per_direction == 1
        if pocket_junction:  # Network.validate (run_tool) checks where it is and that it fits
            pocket = dict(road.pockets.get(str(pocket_junction), {"lanes": 1, "length_m": 50.0, "side": "kerb"}))
            pocket.update({key: cast(value) for key, cast, value in (("lanes", _int, pocket_lanes),
                           ("length_m", float, pocket_length_m), ("side", str, pocket_side)) if value is not None})
            road.pockets = {**road.pockets, str(pocket_junction): pocket}
            if not pocket["lanes"]:
                del road.pockets[str(pocket_junction)]
    return {"updated": [_road_info(network, network.road(road_id)) for road_id in ids]}


def add_junction(network: Network, x: float, y: float) -> dict:
    node = network.add_node(float(x), float(y))
    return {"added": node.id, "x": node.x, "y": node.y}


def _junction(network: Network, node_id: str) -> str:
    """`node_id` if it is a junction; a boundary end refused saying so; an unknown id a KeyError (run_tool says
    "Unknown id", explain_error the nearest ids)."""
    node_id = str(node_id)
    if node_id in network.nodes:
        return node_id
    if node_id in network.approach_nodes:
        raise ValueError(f"{node_id} is a boundary end, not a junction: junction ids are like '2,2' or 'N12' "
                         "(list_junctions); a road to the edge is add_boundary_end.")
    raise KeyError(node_id)


def add_road(network: Network, from_junction: str, to_junction: str, crossings: str | None = None) -> dict:
    _junction(network, from_junction), _junction(network, to_junction)
    road, roundabouts = build_edge(network, str(from_junction), str(to_junction), crossing=crossings)
    return {"added": road.id, "roundabouts_added": roundabouts, "overpass": road.overpass}


def add_boundary_end(network: Network, from_junction: str, x: float, y: float, crossings: str | None = None) -> dict:
    road, roundabouts = build_edge(network, str(from_junction), None, float(x), float(y), crossings)
    return {"added_end": road.id, "roundabouts_added": roundabouts, "overpass": road.overpass}


def move_junctions(network: Network, junctions, dx: float, dy: float) -> dict:
    ids = _ids(junctions)
    for node_id in ids:
        network.node(node_id)
    if not network.move_nodes(ids, float(dx), float(dy)):
        raise ValueError("That move would squash a road below 1 m; nothing moved.")
    return {"moved": {node_id: [round(network.node(node_id).x, 1), round(network.node(node_id).y, 1)]
                      for node_id in ids}}


def delete_objects(network: Network, ids) -> dict:
    ids = _ids(ids)
    roads = {**network.roads, **network.approach_roads}
    nodes = {**network.nodes, **network.approach_nodes}
    unknown = [item for item in ids if item not in roads and item not in nodes and item not in network.bus_stops
               and item not in network.institutions and item not in network.walkways
               and item not in network.pedestrian_crossings and item not in network.spawn_points]
    if unknown:  # run_tool tries a road named from its other end, else says "Unknown id" with the nearest
        raise KeyError(unknown[0])
    for stop_id in ids:  # bus stops and institutions first, then roads; a node then takes whatever roads it still has
        network.bus_stops.pop(stop_id, None)
        network.institutions.pop(stop_id, None)
        network.walkways.pop(stop_id, None)
        network.pedestrian_crossings.pop(stop_id, None)
        if stop_id in network.spawn_points:
            network.delete_spawn_point(stop_id)
    for road_id in ids:
        if road_id in network.roads or road_id in network.approach_roads:
            network.delete_road(road_id)
    for node_id in ids:
        if node_id in network.nodes or node_id in network.approach_nodes:
            network.delete_node(node_id)
    return {"deleted": ids}


def build_interchange(network: Network, kind: str, junctions, stem=None, freeway=None, replace=True,
                      freeway_lanes=2) -> dict:
    ids = _ids(junctions)
    for node_id in ids:
        network.node(node_id)
    if kind == "trumpet":
        if len(ids) != 3:
            raise ValueError("A trumpet needs 3 junctions: two on the freeway and the terminating road's.")
        stem = str(stem) if stem else guess_stem(network, ids)
        if stem not in ids:
            raise ValueError("stem must be one of the 3 junctions.")
        legs = tuple(node_id for node_id in ids if node_id != stem)
        name = build_trumpet(network, legs, stem, _bool(replace), _int(freeway_lanes))
    elif kind in ("diamond", "spui"):
        if len(ids) != 4:
            raise ValueError(f"A {kind} needs 4 junctions: two on the freeway and two on the crossroad.")
        if freeway:
            legs = tuple(_ids(freeway))
            cross = tuple(node_id for node_id in ids if node_id not in legs)
            if len(legs) != 2 or len(cross) != 2:
                raise ValueError("freeway must be 2 of the 4 junctions.")
        else:
            legs, cross = pair_legs(network, ids)
        name = build_diamond(network, legs, cross, _bool(replace), _int(freeway_lanes), single_point=kind == "spui")
    else:
        raise ValueError("kind must be trumpet, diamond or spui.")
    return {"built": name, "kind": kind, "freeway": list(legs),
            "new_nodes": [node_id for node_id in network.nodes if node_id.startswith(name + ".")]}


def add_slip_road(network: Network, roads, single_lane=False) -> dict:
    ids = _ids(roads)
    if len(ids) != 2:
        raise ValueError("A slip road needs 2 roads that meet at a junction.")
    slip = network.road(build_slip_road(network, *ids, _bool(single_lane)))
    return {"added": slip.id, "from": slip.from_node, "to": slip.to_node, "lanes": slip.lanes,
            "length_m": round(slip.length_m, 1), "speed_limit_kmh": slip.speed_limit_kmh}


def set_end_flows(network: Network, end: str, supply_vph=None, demand_vph=None) -> dict:
    if end not in network.approach_nodes:
        if end in network.nodes:
            raise ValueError(f"{end} is a junction, not a boundary end: ends are like '1W' or '3N' (network_summary).")
        raise KeyError(end)
    node = network.approach_nodes[end]
    if supply_vph is not None:
        node.supply_vph = float(supply_vph)
    if demand_vph is not None:
        node.demand_vph = float(demand_vph)
    return {"end": end, "supply_vph": node.supply_vph, "demand_vph": node.demand_vph}


GRID_SPACING_M = (20.0, 2000.0)  # generate_grid's block length: shorter than a junction, longer than an arterial's


def generate_grid(network: Network, horizontal=None, vertical=None, spacing_m=None, driving_side=None,
                  speed_limit_kmh=None, lanes_per_direction=None, lane_changing=None, signal_method=None,
                  width_m=None, height_m=None) -> dict:
    if width_m is not None or height_m is not None:  # a size in metres: the rows and columns that make it
        width, height = float(width_m or height_m), float(height_m or width_m)
        spacing = float(spacing_m or network.default_spacing)
        if max(width, height) / spacing > 19:  # at most 20 a side: wider blocks instead
            spacing = 10 * ceil(max(width, height) / 19 / 10)
        spacing_m = spacing
        vertical, horizontal = round(width / spacing) + 1, round(height / spacing) + 1
    if horizontal is None or vertical is None:
        raise ValueError("Give horizontal and vertical (rows and columns, 1 to 20), or width_m and height_m.")
    for name, count in (("horizontal", horizontal), ("vertical", vertical)):
        if _int(count) > 20:  # llama3.2 3B sent horizontal 1500, vertical 2000 for a 2000 x 1500 m city
            raise ValueError(f"{name} is a count of {'rows' if name == 'horizontal' else 'columns'} (1 to 20), not "
                             "metres: for a size in metres give width_m and height_m instead.")
    if spacing_m is not None and not GRID_SPACING_M[0] <= float(spacing_m) <= GRID_SPACING_M[1]:
        # the stress test's 50,000 x 50,000 m city of 3 x 3 junctions became 12.5 and 50 km blocks
        raise ToolError(f"spacing_m must be {GRID_SPACING_M[0]:g}-{GRID_SPACING_M[1]:g} m (city blocks are about "
                        "200-400 m). For a bigger area use more columns and rows, up to 20 each; tell the user the "
                        "size you can build.")
    fresh = generate_network(
        _int(horizontal), _int(vertical), float(spacing_m or network.default_spacing),
        driving_side or network.driving_side, float(speed_limit_kmh or network.default_speed_limit_kmh),
        _int(lanes_per_direction or network.default_lanes_per_direction),
        network.default_lane_changing if lane_changing is None else _bool(lane_changing),
        signal_method or network.default_signal_method)
    vars(network).update(vars(fresh))  # same object: the window keeps its reference
    return {"generated": f"{vertical} x {horizontal} grid ({vertical} columns, {horizontal} rows)",
            "junctions": len(network.nodes),
            "roads": len(network.roads), "boundary_ends": len(network.approach_nodes)}


def _clock(hours: float) -> str:
    return f"{int(hours):02d}:{round(hours % 1 * 60):02d}"


def list_institutions(network: Network) -> dict:
    rows = []
    for item in network.institutions.values():
        arrive, _ = hourly_people(item)
        rows.append({"id": item.id, "kind": item.kind, "x": round(item.x, 1), "y": round(item.y, 1),
                     "area_m2": item.area_m2, "radius_m": round(item.radius_m, 1), "people": item.people,
                     "employees": item.employees, "shift": f"{_clock(item.shift_start_h)}-{_clock(item.shift_end_h)}",
                     "mode_shares_percent": shares(item), "split_by": "logit" if item.mode_shares else "car share",
                     "door": door(network, item), "catchment": catchment(network, item),
                     "people_per_day_each_way": round(sum(arrive))})
    return _limited(rows, "institutions")


def _catchment_radius(network: Network, x: float, y: float) -> float:
    """A building's default circle: 10 m past its 4th nearest junction or end (its door and three places its
    people come from; a grid block's four corners)."""
    distances = sorted(hypot(node.x - x, node.y - y) for node in network.all_nodes())
    return distances[min(3, len(distances) - 1)] + 10 if distances else 0.0


def place_institution(network: Network, kind, near=None, x=None, y=None, radius_m=None, area_m2=None, people=None,
                      employees=None, shift_start_h=None, shift_end_h=None, car_share=None) -> dict:
    """One building on clear ground, at x, y or the nearest clear spot to a junction or end (`near`): spots on
    rings 10 m apart round it, 16 directions each, diagonals first (block interiors on a grid)."""
    names = {name.lower(): name for name in KINDS}
    kind = names.get(str(kind).strip().lower())
    if kind is None:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}.")
    area, *figures = KINDS[kind].defaults
    area = float(area_m2) if area_m2 is not None else float(area)
    given = [people, employees, shift_start_h, shift_end_h, car_share]
    figures = [value if value is not None else default for value, default in zip(given, figures)]
    if x is not None and y is not None:
        spot = (float(x), float(y))
    elif near is not None:
        node = next((n for n in network.all_nodes() if n.id == str(near)), None)
        if node is None:
            raise KeyError(str(near))
        angles = [45 + 90 * i for i in range(4)] + [90 * i for i in range(4)] + [22.5 + 45 * i for i in range(8)]
        spots = ((node.x + d * cos(radians(a)), node.y + d * sin(radians(a))) for d in range(int(sqrt(area) / 2) + 10, 1000, 10)
                 for a in angles)
        spot = next((s for s in spots if footprint_problem(network, s[0], s[1], area) is None), None)
        if spot is None:
            raise ValueError(f"No clear ground for a {area:g} m² {kind} within 1 km of {near}.")
    else:
        raise ValueError("Give near (a junction or end) or x and y.")
    if radius_m is None:
        radius_m = _catchment_radius(network, *spot)
    item = place(network, kind, spot[0], spot[1], area, max(float(radius_m), sqrt(area) / 2), int(figures[0]),
                 int(figures[1]), float(figures[2]), float(figures[3]), float(figures[4]))
    return {"placed": item.id, "kind": kind, "x": round(item.x), "y": round(item.y), "radius_m": round(item.radius_m),
            "door": door(network, item), "catchment": catchment(network, item),
            "note": "A Run spawns its trips from its catchment to its door and back; its split is its car share until "
                    "institution_mode_split sets one."}


def deploy_institutions(network: Network, count, kinds=None, seed=None, radius_m=None) -> dict:
    """`count` buildings at random spots of clear ground in the network's extent, all or none."""
    count = _int(count)
    if not 1 <= count <= 50:
        raise ValueError("count must be 1 to 50.")
    names = {name.lower(): name for name in KINDS}
    chosen = [names.get(str(kind).strip().lower()) for kind in (_ids(kinds) if kinds else list(KINDS))]
    if None in chosen:
        raise ValueError(f"kinds must be among {', '.join(KINDS)}.")
    nodes = list(network.all_nodes())
    if not nodes:
        raise ValueError("There is no network to place them in: build its roads first.")
    xs, ys = [node.x for node in nodes], [node.y for node in nodes]
    rng = random.Random(seed)
    placed, tries = [], 0
    while len(placed) < count and tries < 300 * count:
        tries += 1
        kind = rng.choice(chosen)
        area, people, employees, start, end, car = KINDS[kind].defaults
        x, y = rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys))
        if footprint_problem(network, x, y, area) is not None:
            continue
        reach = float(radius_m) if radius_m is not None else _catchment_radius(network, x, y)
        placed.append(place(network, kind, x, y, area, max(reach, sqrt(area) / 2), people, employees, start, end, car))
    if len(placed) < count:
        raise ValueError(f"Only {len(placed)} of {count} found clear ground in the network's extent: ask for fewer, "
                         "or smaller kinds.")
    return {"placed": [{"id": item.id, "kind": item.kind, "x": round(item.x), "y": round(item.y),
                        "radius_m": round(item.radius_m), "reaches": links(network, item.x, item.y, item.radius_m)}
                       for item in placed], "seed": seed,
            "note": "Each has its kind's usual figures; the user can see and delete them in Demand Generator > "
                    "Institutions. A Run spawns their trips; set their mode split with institution_mode_split."}


def _centre(network: Network) -> tuple[float, float]:
    """The middle of the network's junctions and ends (the map's anchor when there are none)."""
    nodes = list(network.all_nodes())
    if not nodes:
        return network.background.anchor_x, network.background.anchor_y
    xs, ys = [node.x for node in nodes], [node.y for node in nodes]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def _cached(key, make):
    """make()'s value, kept by key; a failure is kept a minute, so a check and its change don't ask twice."""
    found = _PREPARED.get(key)
    if found is not None:
        made, value = found
        if not isinstance(value, ValueError):
            return value
        if time.time() - made < 60:
            raise ValueError(str(value))
    try:
        value = make()
    except ValueError as exc:
        _PREPARED[key] = (time.time(), exc)
        raise
    while len(_PREPARED) >= 8:
        _PREPARED.pop(next(iter(_PREPARED)))
    _PREPARED[key] = (time.time(), value)
    return value


def _background_graph(network: Network, road_classes="all", width_m=1200.0, height_m=1200.0, road_colour=None,
                      colour_tolerance=40.0, resolution_m=2.0) -> tuple:
    """The background's roads as a background_network.Graph, and what they came from."""
    background = network.background
    if background.kind == "map":
        classes = background_network.LEVELS.get(str(road_classes))
        if classes is None:
            raise ValueError("road_classes must be 'main' or 'all'.")
        width, height = float(width_m), float(height_m)
        if not (200 <= width <= 4000 and 200 <= height <= 4000):
            raise ValueError("width_m and height_m must be 200 to 4000 m.")
        cx, cy = _centre(network)
        rect = (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)
        frame = Frame(background.lat, background.lon, background.anchor_x, background.anchor_y, background.map_rotation_deg)
        corners = [frame.to_wgs84(x, y) for x in (rect[0], rect[2]) for y in (rect[1], rect[3])]
        query = background_network.overpass_query(min(lat for lat, _ in corners), min(lon for _, lon in corners),
                                                  max(lat for lat, _ in corners), max(lon for _, lon in corners), classes)
        document = _cached(("osm", query), lambda: background_network.fetch_osm(query))
        graph = background_network.osm_graph(document, lambda lat, lon: frame.to_scene(*mercator(lat, lon)), rect)
        copy = f" (a copy kept from {document['cached_at']}: {document.get('cached_why', '')})"             if document.get("cached_at") else ""
        return graph, (f"OpenStreetMap road data, {road_classes} roads, {width:g} x {height:g} m round the network's "
                       f"centre{copy}")
    if background.kind == "image":
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(road_colour or "")):
            raise ValueError("Say the colour of the roads on the image: road_colour like '#ffffff'. Ask the user if "
                             "you don't know it.")
        tolerance, resolution = float(colour_tolerance), float(resolution_m)
        if not (0 <= tolerance <= 255 and 0.5 <= resolution <= 20):
            raise ValueError("colour_tolerance must be 0-255 and resolution_m 0.5-20 m.")
        if image_mask is None:
            raise ValueError("The background image can't be read here.")
        key = ("image", background.image, background.page, background.width_m, background.x_m, background.y_m,
               background.rotation_deg, str(road_colour).lower(), tolerance, resolution)

        def trace():
            width, height, mask, (a, b, c, d, e, f), metres = image_mask(background, str(road_colour), tolerance, resolution)
            return background_network.raster_graph(width, height, mask,
                                                   lambda px, py: (a * px + c * py + e, b * px + d * py + f), metres)

        return _cached(key, trace), f"the image's {road_colour} roads, traced at {resolution:g} m a pixel"
    raise ValueError("The background is a plain colour: call place_map with the place first (or the user opens "
                     "an image: Create Network > Own Network), then network_from_background.")


def network_from_background(network: Network, road_classes="all", width_m=1200.0, height_m=1200.0, road_colour=None,
                            colour_tolerance=40.0, resolution_m=2.0, footpaths="tagged") -> dict:
    graph, source = _background_graph(network, road_classes, width_m, height_m, road_colour, colour_tolerance,
                                      resolution_m)
    fresh = background_network.build(network, graph)
    walking = background_network.add_walking(fresh, graph, str(footpaths))  # what the map has for walkers
    kept = {key: item for key, item in network.institutions.items()
            if footprint_problem(fresh, item.x, item.y, item.area_m2) is None}
    dropped = sorted(set(network.institutions) - set(kept))
    fresh.institutions = kept
    vars(network).update(vars(fresh))  # same object: the window keeps its reference
    controls = [node.control for node in network.nodes.values()]
    result = {"source": source, "junctions": len(network.nodes), "signalised": controls.count("signalised"),
              "roundabouts": controls.count("roundabout"), "roads": len(network.roads),
              "boundary_ends": sorted(network.approach_nodes)[:60]}
    if graph.lanes_note:
        result["lanes"] = graph.lanes_note
    roads = list(network.all_roads())
    result["pedestrians"] = {"roads_with_footpaths": sum(bool(road.footpaths) for road in roads),
                             "roads_with_cycle_lanes": sum(bool(road.cycle_lanes) for road in roads), **walking,
                             **({"crossings_not_taken": graph.walking_skipped} if graph.walking_skipped else {})}
    if graph.skipped:  # a pedestrian crossing's signal, a flashing one...: its junction gives way instead
        result["signals_not_junction_signals"] = dict(sorted(graph.skipped.items()))
        result["signals_note"] = ("Signals of these kinds don't signalise a junction, so the junctions near them "
                                  "stay priority junctions. Any junction can still be made signalised (the Edit "
                                  "tab's Control, or set_junction_control) and given any timing method.")
    if dropped:
        result["institutions_removed"] = dropped  # a new road runs where they stood
    if network.background.kind == "image" and not network.background.reference:
        result["note"] = "The image's scale was never set (Set Scale), so its roads are only as true as its width."
    return result


def prepare(network: Network, name: str, args) -> None:
    """A tool call's slow or random part, done before its check, off the UI thread (ai_mode runs it on the
    model's thread): a random layout gets its seed, so the check and the change agree; the background's
    roads are fetched or traced into the cache. Problems are left for the tool itself to report."""
    if not isinstance(args, dict):
        return
    if name == "deploy_institutions" and args.get("seed") is None:
        args["seed"] = random.randrange(1_000_000)
    if name == "network_from_background":
        try:
            _background_graph(network, **{key: value for key, value in args.items() if key != "footpaths"})
        except (ValueError, TypeError):
            pass
    if name == "place_map" and args.get("place"):
        try:
            _places(args["place"])
        except ValueError:
            pass


SATURATION_RANGE = (0.1, 1.2)
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


def set_signal_timing(network: Network, junctions, method=None, phases=None, green_times_s=None, cycle_min_s=None,
                      cycle_max_s=None, target_saturation_percent=None, stop_weight_s=None) -> dict:
    ids = [_junction(network, node_id) for node_id in _ids(junctions)]
    unsignalised = [node_id for node_id in ids if network.nodes[node_id].control != "signalised"]
    if unsignalised:  # all of them at once, so one call can put them right
        raise ValueError(f"{', '.join(unsignalised)} {'is' if len(unsignalised) == 1 else 'are'} not signalised: "
                         "make them signalised first (set_junction_control), or leave them out.")
    if method is not None and method not in SIGNAL_METHODS:
        raise ValueError(f"method must be one of {', '.join(SIGNAL_METHODS)}.")
    if phases is not None and _int(phases) not in (2, 4):
        raise ValueError("phases must be 2 or 4.")
    greens = None if green_times_s is None else \
        [float(g) for g in (json.loads(green_times_s) if isinstance(green_times_s, str) else green_times_s)]
    note = "Each Run times them by their method, from its demand (fixed: the greens given)."
    if greens is not None:  # greens are only ever used by a fixed plan: given alone, they mean one
        if method not in (None, "fixed"):
            raise ValueError(f"green_times_s are only used by method fixed; {method} times the greens from demand. "
                             "Give method fixed with them, or leave them out.")
        for node_id in ids:
            count = _int(phases) if phases is not None else network.nodes[node_id].signal_phases
            if len(greens) != count:
                raise ValueError(f"green_times_s needs one green a phase: {node_id} has {count} phases, "
                                 f"{len(greens)} greens given" + (" (or give phases too)." if phases is None else "."))
        if method is None:
            method, note = "fixed", "Fixed time with the greens given (they are used only by a fixed plan)."
    for node_id in ids:
        node = network.nodes[node_id]
        if method is not None:
            node.signal_method = method
        if phases is not None:
            node.signal_phases = _int(phases)
            if len(node.green_times_s) != node.signal_phases:
                node.green_times_s = (node.green_times_s * 2)[:node.signal_phases]
        if greens is not None:
            node.green_times_s = list(greens)
        if cycle_min_s is not None:
            node.cycle_min_s = float(cycle_min_s)
        if cycle_max_s is not None:
            node.cycle_max_s = float(cycle_max_s)
        if target_saturation_percent is not None:
            node.target_saturation = float(target_saturation_percent) / 100
        if stop_weight_s is not None:
            node.stop_weight_s = float(stop_weight_s)
    network.validate()  # phases, greens, limits: refused (and rolled back) with the reason
    return {"timed": {node_id: {"method": network.nodes[node_id].signal_method, "phases": network.nodes[node_id].signal_phases,
                                "green_times_s": network.nodes[node_id].green_times_s,
                                "cycle_limits_s": [network.nodes[node_id].cycle_min_s, network.nodes[node_id].cycle_max_s],
                                "target_saturation_percent": round(network.nodes[node_id].target_saturation * 100)}
                      for node_id in ids},
            "note": note}


place_download = None  # stands in for the network in tests: url -> bytes
_last_search = [0.0]


def _places(query: str) -> list:
    """Places by name from Nominatim (its policy: one request a second, the app's User-Agent), cached."""
    query = " ".join(str(query).split())
    if not query:
        raise ValueError("Give a place name, or lat and lon.")

    def fetch():
        wait = _last_search[0] + map_tiles.SEARCH_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_search[0] = time.monotonic()
        url = map_tiles.SEARCH_URL.format(limit=map_tiles.SEARCH_LIMIT, query=__import__("urllib.parse").parse.quote(query))
        try:
            found = json.loads((place_download or map_tiles._download)(url))
        except OSError as exc:
            raise ValueError(f"Couldn't reach the place search ({exc}).") from exc
        places = [(str(p["display_name"]), float(p["lat"]), float(p["lon"])) for p in found if isinstance(p, dict)]
        return [p for p in places if abs(p[1]) <= map_tiles.MAX_LATITUDE]

    return _cached(("place", query.lower()), fetch)


def place_map(network: Network, place=None, lat=None, lon=None, rotation_deg=None) -> dict:
    if place:
        found = _places(place)
        if not found:
            raise ValueError(f"Nothing found for {place!r}: try a fuller name, or give lat and lon.")
        name, lat, lon = found[0]
    elif lat is None or lon is None:
        raise ValueError("Give a place, or lat and lon.")
    else:
        name = f"{float(lat):.6f}, {float(lon):.6f}"
    background = network.background
    background.kind, background.lat, background.lon = "map", float(lat), float(lon)
    background.anchor_x, background.anchor_y = _centre(network)
    if rotation_deg is not None:
        background.map_rotation_deg = float(rotation_deg)
    return {"placed": name, "lat": background.lat, "lon": background.lon,
            "note": "That place is under the network's centre. Next, to use its roads: network_from_background with no "
                    "arguments (it reads this map; it takes no place)."}


def _passengers_problem(boarding_per_h, alighting_percent) -> str | None:
    if boarding_per_h is not None and not 0 <= float(boarding_per_h) <= MAX_BOARDING_PER_H:
        return f"boarding_per_h must be 0 to {MAX_BOARDING_PER_H:g}."
    if alighting_percent is not None and not 0 <= float(alighting_percent) <= 100:
        return "alighting_percent must be 0 to 100."
    return None


def add_crossing(network: Network, road, at_m=None, kind="zebra") -> dict:
    road = network.road(str(road))  # KeyError for an unknown road
    at = network.road_geometry_length(road) / 2 if at_m is None else float(at_m)
    crossing = network.add_crossing(road.id, at, str(kind))  # refused (ValueError) with its reason
    return {"crossing": crossing.id, "kind": crossing.kind, "road": road.id, "at_m": round(at, 1)}


def add_path(network: Network, start, end, bends=None) -> dict:
    way = network.add_walkway(str(start), str(end), _parsed_json(bends) if isinstance(bends, str) else (bends or []))
    return {"path": way.id, "from": way.start, "to": way.end,
            "length_m": round(polyline_length(network.walkway_points(way)), 1)}


def set_pedestrian_demand(network: Network, pairs, people_per_hour) -> dict:
    people = float(people_per_hour)
    if not 0 <= people <= MAX_WALK_DEMAND:
        raise ValueError(f"people_per_hour must be 0 to {MAX_WALK_DEMAND:,}.")
    keys = []
    for pair in _ids(pairs):
        ends = [end.strip() for end in re.split(r"->|=>|>>| to |-", str(pair)) if end.strip()]
        among = next((places for places in (network.approach_nodes, network.spawn_points)
                      if len(ends) == 2 and all(end in places for end in ends)), None)
        if among is None or ends[0] == ends[1]:
            raise ValueError(f"{pair!r}: a pair is two different boundary ends, as '1W->1E' (the ends: "
                             f"{', '.join(sorted(network.approach_nodes))}), or two different spawn points, as "
                             f"'S1->S2' (the points: {', '.join(sorted(network.spawn_points)) or 'none yet'}).")
        keys.append((f"{ends[0]}->{ends[1]}", network.walk_demand if among is network.approach_nodes
                     else network.spawn_demand))
    for key, demand in keys:
        if people:
            demand[key] = people
        else:
            demand.pop(key, None)
    return {"pedestrians_per_hour": {key: people for key, _ in keys}}


def add_spawn_points(network: Network, points) -> dict:
    points = _parsed_json(points) if isinstance(points, str) else points
    if not isinstance(points, list) or not points or not all(isinstance(p, (list, tuple)) and len(p) == 2
                                                             for p in points):
        raise ValueError("points must be a list of [x, y] in metres.")
    placed = [network.add_spawn_point(float(x), float(y)).id for x, y in points]
    return {"spawn_points": placed}


def pedestrian_summary(network: Network) -> dict:
    from .walking import WalkGraph  # noqa: PLC0415 - walking builds on the network's own modules

    roads = list(network.all_roads())
    graph = WalkGraph(network)
    return {"roads_with_footpaths_both_sides": sum(len(road.footpaths) == 2 for road in roads),
            "roads_with_a_footpath_one_side": sum(len(road.footpaths) == 1 for road in roads),
            "roads_without_footpaths": sum(not road.footpaths for road in roads),
            "footpath_km": round(sum(len(road.footpaths) * network.road_geometry_length(road) for road in roads) / 1000, 2),
            "roads_with_cycle_lanes": sorted(road.id for road in roads if road.cycle_lanes),
            "paths": [{"id": way.id, "from": way.start, "to": way.end,
                       "length_m": round(polyline_length(network.walkway_points(way)))}
                      for way in network.walkways.values()],
            "crossings": [{"id": c.id, "kind": c.kind, "road": c.road, "at_m": round(c.distance_m)}
                          for c in network.pedestrian_crossings.values()],
            "pedestrians_per_hour": dict(network.walk_demand),
            "spawn_points": {key: [round(p.x), round(p.y)] for key, p in network.spawn_points.items()},
            "spawn_pedestrians_per_hour": dict(network.spawn_demand),
            "pairs_nobody_can_walk": [pair for pair in network.walk_demand if graph.route(*pair.split("->")) is None]
            + [pair for pair in network.spawn_demand if graph.route(*(
                (network.spawn_points[end].x, network.spawn_points[end].y) for end in pair.split("->"))) is None],
            "without_a_crossing": "walkers cross a junction's arm in a gap in its traffic (HCM 2000 Eq 18-17)"}


def add_bus_stop(network: Network, road, direction="both", dwell_s=None, boarding_per_h=None,
                 alighting_percent=None) -> dict:
    problem = _passengers_problem(boarding_per_h, alighting_percent)
    if problem:
        raise ValueError(problem)
    stop = network.add_bus_stop(str(road), str(direction))
    if dwell_s is not None:
        stop.dwell_s = float(dwell_s)
    if boarding_per_h is not None:
        stop.boarding_per_h = float(boarding_per_h)
    if alighting_percent is not None:
        stop.alighting_percent = float(alighting_percent)
    return {"added": stop.id, "road": stop.road, "distance_m": round(stop.distance_m, 1), "serves": stop.direction,
            "dwell_s": stop.dwell_s, "boarding_per_h": stop.boarding_per_h, "alighting_percent": stop.alighting_percent}


def update_bus_stop(network: Network, stop, distance_m=None, direction=None, dwell_s=None, boarding_per_h=None,
                    alighting_percent=None) -> dict:
    if stop not in network.bus_stops:
        raise ValueError(f"No bus stop called {stop}.")
    problem = _passengers_problem(boarding_per_h, alighting_percent)
    if problem:
        raise ValueError(problem)
    item = network.bus_stops[stop]
    if boarding_per_h is not None:
        item.boarding_per_h = float(boarding_per_h)
    if alighting_percent is not None:
        item.alighting_percent = float(alighting_percent)
    if direction is not None:
        if direction not in BUS_STOP_DIRECTIONS:
            raise ValueError(f"direction must be one of {', '.join(BUS_STOP_DIRECTIONS)}.")
        item.direction = direction
    if distance_m is not None:
        network.move_bus_stop(stop, float(distance_m))
    if dwell_s is not None:
        item.dwell_s = float(dwell_s)
    return {"stop": stop, "road": item.road, "distance_m": round(network.bus_stop_distance(item), 1),
            "serves": item.direction, "dwell_s": item.dwell_s, "boarding_per_h": item.boarding_per_h,
            "alighting_percent": item.alighting_percent}


def update_institution(network: Network, id, people=None, employees=None, shift_start_h=None, shift_end_h=None,  # noqa: A002
                       car_share=None, radius_m=None) -> dict:
    if id not in network.institutions:
        raise KeyError(id)
    item = network.institutions[id]
    if car_share is not None and not (1 <= float(car_share) <= 100 or float(car_share) == 0):
        raise ValueError(f"car_share is a percent, 0 to 100 ({float(car_share) * 100:g}, not {float(car_share):g}, "
                         "for that share)." if 0 < float(car_share) < 1 else "car_share is a percent, 0 to 100.")
    for key, value, low, high in (("shift_start_h", shift_start_h, 0, 24), ("shift_end_h", shift_end_h, 0, 24),
                                  ("radius_m", radius_m, 10, 5000)):
        if value is not None and not low <= float(value) <= high:
            raise ValueError(f"{key} must be {low} to {high}.")
    before = {key: getattr(item, key) for key in ("people", "employees", "shift_start_h", "shift_end_h", "car_share",
                                                  "radius_m")}
    if car_share is not None:
        item.mode_shares = {}  # a share typed in replaces the mode split
    for key, value in (("people", people), ("employees", employees)):
        if value is not None:
            setattr(item, key, _int(value))
    for key, value in (("shift_start_h", shift_start_h), ("shift_end_h", shift_end_h), ("car_share", car_share),
                       ("radius_m", radius_m)):
        if value is not None:
            setattr(item, key, float(value))
    changed = {key: f"{old:g} -> {getattr(item, key):g}" for key, old in before.items() if getattr(item, key) != old}
    return {"updated": id, "changed": changed or "nothing", "reaches": links(network, item.x, item.y, item.radius_m)}


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
    return {"mode": mode.name, "out_of_vehicle_min": 3.0, "cost_per_km": 15.0}


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
              METRO if by_metro and str(a.get("mode", "")).strip().lower() == METRO.lower() else
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
            ivt = sum(road.length_m / (min(top, road.speed_limit_kmh) / 3.6) for road in roads) / 60
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
                                  f"No station within {METRO_WALK_M:g} m of it: nobody comes by metro.")}
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


def _area_closure(network: Network, flows: dict, ahead: dict, area: dict) -> dict:
    """critical_links' area closure: the roads touching the circle closed together; extra veh-h and veh/h cut off."""
    try:
        x, y, radius = float(area["x"]), float(area["y"]), float(area["radius_m"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("area is {\"x\": metres, \"y\": metres, \"radius_m\": metres}.") from None
    if not 10 <= radius <= 5000:
        raise ValueError("area radius_m must be 10 to 5000.")
    closed = {road.id for road in network.all_roads()
              if any(hypot(px - x, py - y) <= radius for px, py in network.road_points(road))
              or any(distance_to_segment((x, y), a, b) <= radius
                     for a, b in zip(network.road_points(road), network.road_points(road)[1:]))}
    if not closed:
        raise ValueError("No road touches that circle.")

    def times(origin, shut):
        best, todo = {origin: 0.0}, [(0.0, origin)]
        while todo:
            spent, here = heapq.heappop(todo)
            if spent > best[here]:
                continue
            for there, step, road_id in ahead[here]:
                if road_id not in shut and spent + step < best.get(there, float("inf")):
                    best[there] = spent + step
                    heapq.heappush(todo, (spent + step, there))
        return best

    extra = lost = 0.0
    for origin in {o for o, _ in flows}:
        before, after = times(origin, set()), times(origin, closed)
        for (o, d), q in flows.items():
            if o != origin or d not in before:
                continue
            if d in after:
                extra += q * (after[d] - before[d]) / 3600
            else:
                lost += q
    return {"area": {"x": x, "y": y, "radius_m": radius}, "roads_closed": sorted(closed),
            "extra_veh_h_per_h": round(extra, 2), "cut_off_vph": round(lost),
            "note": "Every road touching the circle closed at once, the demand re-routed at free flow (static). Test it "
                    "in a run with incidents on those roads (run_experiment)."}


def cluster_conditions(network: Network, days, k=None, seed=1) -> dict:  # noqa: ARG001 - the tools' signature
    """Travel conditions from observed days (FHWA Toolbox III 2019, p26–31): attributes normalised 0–1 (eq. 1),
    k-means, k by the within/between variance ratio unless given; per cluster its size, weight, centroid in the
    attributes' units and its representative day (the member nearest the centroid)."""
    days = _parsed_json(days) if isinstance(days, str) else days
    if not isinstance(days, list) or len(days) < 4 or not all(isinstance(d, dict) and isinstance(d.get("values"), dict)
                                                              for d in days):
        raise ValueError("Give 4 or more days like {\"label\": \"Mon 3 Mar\", \"values\": {\"volume\": 5200, \"rain_mm\": 2}}.")
    features = sorted({key for day in days for key in day["values"]})
    if any(not isinstance(day["values"].get(f), (int, float)) for day in days for f in features):
        raise ValueError(f"Every day needs a number for each of {', '.join(features)}.")
    raw = [[float(day["values"][f]) for f in features] for day in days]
    low, high = [min(c) for c in zip(*raw)], [max(c) for c in zip(*raw)]
    points = [[(x - a) / (b - a) if b > a else 0.0 for x, a, b in zip(row, low, high)] for row in raw]
    seed = _int(seed)
    if k is None:
        k = stats.choose_k(points, seed)
    k = _int(k)
    if not 2 <= k < len(days):
        raise ValueError(f"k must be 2 to {len(days) - 1}.")
    labels, centroids, _ = stats.kmeans(points, k, seed)
    clusters = []
    for c in range(k):
        members = [i for i, label in enumerate(labels) if label == c]
        if not members:
            continue
        nearest = min(members, key=lambda i: sum((a - b) ** 2 for a, b in zip(points[i], centroids[c])))
        clusters.append({"size": len(members), "weight": round(len(members) / len(days), 3),
                         "centroid": {f: round(sum(raw[i][j] for i in members) / len(members), 2) for j, f in enumerate(features)},
                         "representative_day": str(days[nearest].get("label", nearest)),
                         "days": [str(days[i].get("label", i)) for i in members][:20]})
    return {"k": len(clusters), "attributes": features, "clusters": sorted(clusters, key=lambda c: -c["size"]),
            "note": "Test each cluster's representative day as a scenario (e.g. demand_scale from its volume against the "
                    "baseline's) and weight its results by weight (FHWA Toolbox III, p91)."}


def cut_elements(network: Network) -> dict:
    """The network's single points of failure (Tarjan's bridges and articulation points on the undirected road
    graph; Taylor, Vulnerability Analysis for Transportation, Ch 3): the roads and junctions whose loss splits it,
    and what each cuts off (the junctions and boundary ends left on the smaller side)."""
    around: dict = defaultdict(list)
    for road in network.all_roads():
        around[road.from_node].append((road.to_node, road.id))
        around[road.to_node].append((road.from_node, road.id))
    order, low, bridges, joints, counter = {}, {}, [], set(), [0]

    def visit(root: str) -> None:  # iterative depth-first search (no recursion limit on big networks)
        order[root] = low[root] = counter[0]
        counter[0] += 1
        stack, children = [(root, None, iter(around[root]))], {root: 0}
        while stack:
            node, via, edges = stack[-1]
            advanced = False
            for there, road in edges:
                if road == via:
                    continue
                if there not in order:
                    order[there] = low[there] = counter[0]
                    counter[0] += 1
                    children[node] = children.get(node, 0) + 1
                    stack.append((there, road, iter(around[there])))
                    advanced = True
                    break
                low[node] = min(low[node], order[there])
            if advanced:
                continue
            stack.pop()
            if stack:
                parent = stack[-1][0]
                low[parent] = min(low[parent], low[node])
                if low[node] > order[parent]:
                    bridges.append(via)
                if parent != root and low[node] >= order[parent]:
                    joints.add(parent)
        if children.get(root, 0) > 1:
            joints.add(root)

    for node in [node.id for node in network.all_nodes()]:
        if node not in order:
            visit(node)

    def cut_off(closed_road=None, closed_node=None) -> list[str]:
        nodes = [node.id for node in network.all_nodes() if node.id != closed_node]
        seen, parts = set(), []
        for start in nodes:
            if start in seen:
                continue
            part, todo = {start}, [start]
            while todo:
                for there, road in around[todo.pop()]:
                    if road != closed_road and there != closed_node and there not in part:
                        part.add(there)
                        todo.append(there)
            seen |= part
            parts.append(part)
        main = max(parts, key=len)  # all that is split away from the main part
        return sorted(node for part in parts if part is not main for node in part)

    real = [road for road in bridges if road not in network.approach_roads]  # an end's own road always is one
    junctions = []
    for node in sorted(joints):
        cut = cut_off(closed_node=node) if node in network.nodes else []
        if any(other in network.nodes for other in cut):  # more than its own boundary ends
            junctions.append({"junction": node, "cuts_off": cut[:12]})
    return {"bridge_roads": [{"road": road, "cuts_off": cut_off(closed_road=road)[:12]} for road in real],
            "cut_junctions": junctions,
            "note": "Bridge roads and cut junctions split the network when lost; a junction cutting off only its own "
                    "boundary ends isn't listed (every edge junction does)."}


def critical_links_plan(network: Network, flows: dict, limit: int = 10, area: dict | None = None) -> dict:
    """Vulnerability by closing each used road in turn (Taylor, Vulnerability Analysis for Transportation,
    p107–112, p204): each OD pair's free-flow time at the speed limits (one-way roads kept) with and without the
    road; the extra vehicle-hours an hour, Σ q·Δt, and the veh/h cut off. Then the single points of failure
    (cut_elements). `area` {"x", "y", "radius_m"}: every road touching that circle closed at once instead (a flood,
    a fire, an event: Taylor Ch 6, area-wide disruption)."""
    ahead = defaultdict(list)
    for road in network.all_roads():
        seconds = road.length_m / max(road.speed_limit_kmh, 1) * 3.6
        ahead[road.from_node].append((road.to_node, seconds, road.id))
        if road.direction == "two-way":
            ahead[road.to_node].append((road.from_node, seconds, road.id))

    def tree(origin, closed=None):
        best, via, todo = {origin: 0.0}, {}, [(0.0, origin)]
        while todo:
            spent, here = heapq.heappop(todo)
            if spent > best[here]:
                continue
            for there, step, road_id in ahead[here]:
                if road_id != closed and spent + step < best.get(there, float("inf")):
                    best[there], via[there] = spent + step, (here, road_id)
                    heapq.heappush(todo, (spent + step, there))
        return best, via

    flows = {pair: q for pair, q in flows.items() if q > 0}
    if not flows:
        raise ValueError("There's no demand to route: enter some first.")
    if area is not None:
        return _area_closure(network, flows, ahead, area)
    base, used = {}, defaultdict(list)
    for origin in {o for o, _ in flows}:
        best, via = tree(origin)
        for (o, d), q in flows.items():
            if o == origin and d in best:
                base[(o, d)] = best[d]
                node = d
                while node != o:
                    node, road_id = via[node]
                    used[road_id].append((o, d))
    rows = []
    for road_id, pairs in used.items():
        if road_id in network.approach_roads:  # an end's own road: closing it only cuts that end off
            continue
        extra = lost = 0.0
        for origin in {o for o, _ in pairs}:
            best, _ = tree(origin, road_id)
            for o, d in pairs:
                if o != origin:
                    continue
                if d in best:
                    extra += flows[(o, d)] * (best[d] - base[(o, d)]) / 3600
                else:
                    lost += flows[(o, d)]
        rows.append({"road": road_id, "pairs_using": len(pairs), "extra_veh_h_per_h": round(extra, 2),
                     "cut_off_vph": round(lost)})
    rows.sort(key=lambda row: (-row["cut_off_vph"], -row["extra_veh_h_per_h"]))
    return {"roads_tested": len(rows), "critical": rows[:max(1, min(_int(limit), 50))], **cut_elements(network),
            "note": "Free-flow times at the speed limits, no congestion; boundary roads left out (closing one only "
                    "cuts off its end). Test the worst with run_experiment, a scenario deleting the road (delete_objects)."}


_FUNCTIONS = {function.__name__: function for function in (
    set_signal_timing, place_map, add_bus_stop, update_bus_stop, update_institution,
    add_crossing, add_path, set_pedestrian_demand, pedestrian_summary, add_spawn_points,
    network_summary, list_roads, list_junctions, describe, analyse_network, read_source, list_institutions,
    set_junction_control, update_roads, add_junction, add_road, add_boundary_end, move_junctions,
    delete_objects, build_interchange, add_slip_road, set_end_flows, deploy_institutions, network_from_background, generate_grid,
    network_indices, cluster_conditions, place_institution)}


_KINDS = {"string": "text", "number": "number", "integer": "whole number", "boolean": "true/false",
          "array": "list", "object": "object"}
_TRUE, _FALSE = {"true", "yes", "on", "1"}, {"false", "no", "off", "0"}
# Text a model sends for an argument it means to leave out (llama3.2 fills every optional one with "None" or "null"):
# left out like a null, never read as a value ("None" is no arrival pattern, "null" no seed)
_NOTHING = {"", "none", "null", "nil", "undefined", "n/a"}
# Optional numbers no tool means as 0 (an area of no size, no trips, a trip of no length, no traffic): a 0 is left
# out as above (llama3.2 sent network_from_background height_m 0, generate_demand total 0 and mean_trip_km 0)
_ZERO_IS_NONE = {"width_m", "height_m", "resolution_m", "total", "mean_trip_km", "degree_of_saturation",
                 # llama3.2 sent update_roads lanes_per_direction 0, add_lanes 0 (no change), length_m 0 and
                 # pocket_length_m 0 for every setting it left alone, and was refused for the speed limit it asked
                 "lanes_per_direction", "add_lanes", "length_m", "pocket_length_m", "phases", "cycle_min_s",
                 "cycle_max_s", "target_saturation_percent", "spacing_m"}
# A traffic level a model puts in a choice of another kind (demand "peak", arrival_pattern "heavy"): read as the
# degree_of_saturation it means (_SATURATION's levels) where the tool takes one and none was given, else refused
LEVEL_WORDS = {**dict.fromkeys(("peak", "peak hour", "rush", "rush hour", "busy", "heavy", "high", "congested"), 0.85),
               **dict.fromkeys(("medium", "moderate", "normal", "off-peak", "off peak"), 0.65),
               **dict.fromkeys(("light", "low", "quiet"), 0.4)}


def signature(name: str) -> str:
    """What a tool takes, in one line, for errors the model can act on."""
    tool = next(tool for tool in TOOLS if tool["name"] == name)
    properties, required = tool["parameters"]["properties"], set(tool["parameters"]["required"])

    def kind(spec: dict) -> str:
        return "one of " + "/".join(spec["enum"]) if "enum" in spec else _KINDS.get(spec.get("type", ""), "any")

    parts = [f"{key} ({kind(spec)}{', required' if key in required else ''})" for key, spec in properties.items()]
    return f"{name} takes " + (", ".join(parts) if parts else "no arguments")


def normalise(name: str, args) -> dict:
    """The model's arguments for `name` in the shapes its schema gives, before anything runs: a close
    misspelling or a plural (ends -> end) takes the parameter's name; numbers, whole numbers and true/false
    sent as text become them; a choice is matched whatever its case; a one-item list where one value is
    wanted is that value; nulls, and text meaning nothing ("None", "null"), are left out. Lists stay as sent (the tools read them: ids like "2,2" hold
    commas). ToolError, saying what the tool takes, for an unknown or missing argument or a value that
    can't be read, so the model can call again."""
    tool = next((tool for tool in TOOLS if tool["name"] == name), None)
    if tool is None:  # small models invent tools for what the app can't do (teleport_vehicles, set_population)
        close = difflib.get_close_matches(str(name), [tool["name"] for tool in TOOLS], n=3, cutoff=0.6)
        raise ToolError(f"There is no tool called {name!r}." + (f" Named alike: {', '.join(close)}; use one only if it "
                        "does what the user asked." if close else "") + " If no tool does it, the app can't do that: "
                        "tell the user so, or call find_tools with what you need.")
    if args in (None, ""):
        args = {}
    if isinstance(args, str):
        args = _parsed_json(args)
    if not isinstance(args, dict):
        raise ToolError(f"Arguments must be an object. {signature(name)}.")
    properties, required = tool["parameters"]["properties"], tool["parameters"]["required"]
    args = _level_read(args, properties)
    out: dict = {}
    for key, value in args.items():
        if value is None or isinstance(value, str) and value.strip().lower() in _NOTHING:
            continue  # left out: it says nothing, even under a name the tool doesn't have
        target = key if key in properties else _parameter_for(str(key), properties)
        if target is None:  # another tool's argument (place for place_map) says where it belongs
            owners = [tool["name"] for tool in TOOLS if key in tool["parameters"]["properties"]][:3]
            raise ToolError(f"{name} has no argument {key!r}" + (f" ({key} is {' or '.join(owners)}'s)" if owners else "")
                            + f". {signature(name)}.")
        if target in out:
            raise ToolError(f"{name} got {target} twice (as {key!r} too). {signature(name)}.")
        if target in _ZERO_IS_NONE and target not in required and str(value).strip() in ("0", "0.0"):
            continue  # a size, total or level of 0 can't be meant: llama3.2's filler for a number it doesn't know
        spec = properties[target]
        if spec.get("type") == "string" and "enum" not in spec and target not in required \
                and str(value).strip().lower() in ("false", "true", "0"):
            continue  # an id or name given as true/false/0 (pocket_junction "false"): filler, not an id
        out[target] = _coerced(name, target, properties[target], value)
    missing = [key for key in required if out.get(key) in (None, "", [])]
    if missing:
        raise ToolError(f"{name} needs {', '.join(missing)}. {signature(name)}.")
    return out


def _level_read(args: dict, properties: dict) -> dict:
    """A level word in a choice of another kind (llama3.2: demand "peak", then degree_of_saturation 0 when refused)
    as the degree_of_saturation it means, when the tool takes one and none in range was given; a 0 with no level
    word is the filler it sends for a number it doesn't know (llama3.2), so not given."""
    if "degree_of_saturation" not in properties:
        return args
    given = args.get("degree_of_saturation")
    if isinstance(given, str) and given.strip().lower() in LEVEL_WORDS:  # the level itself ("busy", llama3.2)
        args = {**args, "degree_of_saturation": LEVEL_WORDS[given.strip().lower()]}
    levels = [key for key, value in args.items() if "enum" in properties.get(key, {}) and isinstance(value, str)
              and value.strip().lower() in LEVEL_WORDS]
    level = levels[0] if levels else None
    try:
        x = float(args.get("degree_of_saturation"))
    except (TypeError, ValueError):
        x = None
    if x is not None and SATURATION_RANGE[0] <= x <= SATURATION_RANGE[1]:  # a level word beside it says it again
        return {key: value for key, value in args.items() if key not in levels}
    if level is not None:
        word = args[level].strip().lower()
        return {**{key: value for key, value in args.items() if key != level}, "degree_of_saturation": LEVEL_WORDS[word]}
    return {key: value for key, value in args.items() if key != "degree_of_saturation"} if x == 0 else args


def _parameter_for(key: str, properties: dict) -> str | None:
    """The parameter a misnamed argument clearly means: the same name in another case, its singular or
    plural, or one close spelling; None if none is clear."""
    lowered = {name.lower(): name for name in properties}
    key = key.strip().lower()
    for guess in (key, key.rstrip("s"), key + "s", key.replace(" ", "_"), key.replace("-", "_")):
        if guess in lowered:
            return lowered[guess]
    close = difflib.get_close_matches(key, list(lowered), n=2, cutoff=0.85)
    return lowered[close[0]] if len(close) == 1 else None


def _coerced(name: str, key: str, spec: dict, value):
    kind = spec.get("type")
    wrong = f"{key} must be {_KINDS.get(kind, kind)}, not {value!r}. {signature(name)}."
    if kind == "string" and isinstance(value, list):
        if len(value) != 1:
            raise ToolError(f"{key} takes one value; call {name} once for each. {signature(name)}.")
        value = value[0]
    if "enum" in spec:
        match = [choice for choice in spec["enum"] if str(choice).lower() == str(value).strip().lower().replace("_", " ")
                 or str(choice).lower() == str(value).strip().lower()]
        if not match:  # one close spelling ("scot": scoot) is that choice
            close = difflib.get_close_matches(str(value).strip().lower(), [str(c).lower() for c in spec["enum"]],
                                              n=2, cutoff=0.8)
            match = [c for c in spec["enum"] if len(close) == 1 and str(c).lower() == close[0]]
        if not match:
            level = str(value).strip().lower() in LEVEL_WORDS  # llama3.2: arrival_pattern "heavy", demand "peak"
            here = any(t["name"] == name and "degree_of_saturation" in t["parameters"]["properties"] for t in TOOLS)
            raise ToolError(f"{key} must be one of {', '.join(map(str, spec['enum']))}, not {value!r}." + (
                f" {value!r} is how busy traffic is, which {key} isn't: leave {key} out and give degree_of_saturation "
                + ("" if here else "to generate_demand (or scale_demand, once there is demand) ")
                + "(light 0.4, medium 0.65, busy or peak 0.85, near capacity 0.95)." if level else ""))
        return match[0]
    if kind == "string":
        return value if isinstance(value, str) else str(value) if isinstance(value, (int, float)) else value
    if kind in ("number", "integer"):
        if isinstance(value, bool):
            raise ToolError(wrong)
        try:
            number = float(value.strip()) if isinstance(value, str) else float(value)
        except (TypeError, ValueError):
            raise ToolError(wrong) from None
        if not isfinite(number):  # "nan", "inf" (text, or JSON's NaN/Infinity): no tool can use them
            raise ToolError(wrong)
        if kind == "integer":
            if number != int(number):
                raise ToolError(wrong)
            return int(number)
        return number
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE or text in _FALSE:
            return text in _TRUE
        raise ToolError(wrong)
    if kind in ("array", "object") and isinstance(value, str) and value.strip()[:1] in ("[", "{"):
        try:  # a list or object sent as JSON text (small models do): read, repaired if it can be
            value = _repaired_json(value)
        except ValueError as exc:
            raise ToolError(f"{key} was sent as text that isn't valid JSON ({exc.msg} at character {exc.pos} of "
                            f"{len(value)}): send {key} as a {'list' if kind == 'array' else 'object'} itself, not "
                            f"a string. {signature(name)}.") from None
    if kind == "array" and isinstance(value, list) and isinstance(spec.get("items"), dict) and \
            spec["items"].get("properties"):  # each item's figures and choices, like the top level's
        fields = spec["items"]["properties"]
        return [{key: _coerced(name, key, fields[key], item_value) if key in fields and item_value is not None
                 else item_value for key, item_value in item.items()} if isinstance(item, dict) else item
                for item in value]
    if kind == "object" and isinstance(value, str):
        parsed = _parsed_json(value)
        if not isinstance(parsed, dict):
            raise ToolError(wrong)
        return parsed
    return value


def run_tool(network: Network, name: str, args: dict) -> dict:
    """Run a tool; an edit that fails, or would change nothing, leaves the network exactly as it was.
    ToolError says why. A network edit's result lists what it changed (`changed`)."""
    function = _FUNCTIONS.get(name)
    if function is None:
        raise ToolError(f"There is no tool called {name!r}.")
    args = _every(network, name, normalise(name, args))
    flipped: dict = {}  # road ids named from the other end ("2,2-2,3" for 2,3-2,2), and the road's own id
    if name == "update_roads":  # its two junctions for one road (llama3.2 3B sent roads ["2,2", "3,2"] for 2,2-3,2)
        try:
            ends = _ids(args.get("roads") or [])
        except ValueError:  # objects in the list: the tool refuses them below, saying how
            ends = []
        road = next((f"{a}-{b}" for a, b in (ends, ends[::-1]) if f"{a}-{b}" in network.roads), None) \
            if len(ends) == 2 and all(end in network.nodes for end in ends) else None
        if road:
            flipped[f"{ends[0]}, {ends[1]}"] = road
            args = {**args, "roads": [road]}
    while True:
        before = deepcopy(network) if name in EDIT_TOOLS else None
        try:
            result = function(network, **args)
            if before is not None:
                network.validate()
                if network == before:
                    raise ToolError(f"Nothing would change ({describe_call(name, args)}): it is like that already.")
                if isinstance(result, dict):
                    result.setdefault("changed", describe_changes(before, network, limit=10))
            if flipped and isinstance(result, dict):
                result["read_as"] = {given: own for given, own in flipped.items()}  # the model learns the real ids
            return result
        except (ToolError, ValueError, KeyError, TypeError) as exc:
            if before is not None:
                vars(network).update(vars(before))
            if isinstance(exc, KeyError):
                given = str(exc.args[0])
                own = _road_from_other_end(network, given)
                if own and given not in flipped:  # the same road, its ends named the other way round: run again
                    flipped[given] = own
                    args = _swapped(args, given, own)
                    continue
                raise ToolError(f"Unknown id {given!r}.") from exc
            if isinstance(exc, TypeError):
                raise ToolError(f"Bad arguments for {name}: {exc}. {signature(name)}.") from exc
            raise ToolError(str(exc)) from exc


# "all" where a tool takes ids: what it means for that tool (small models send roads "all" for "every road", and the
# system prompt asks to retime "every signalised one"); deleting or moving everything isn't offered
ALL_IDS = {("update_roads", "roads"): lambda network: [road.id for road in network.all_roads()],
           ("set_junction_control", "junctions"): lambda network: list(network.nodes),
           ("set_signal_timing", "junctions"): lambda network: [node_id for node_id, node in network.nodes.items()
                                                                 if node.control == "signalised"]}
_ALL_WORDS = {"all", "every", "*", "all roads", "all junctions", "every road", "every junction", "all signals",
              "list_junctions", "list_roads"}  # the listing tool named for what it lists (llama3.2 3B)


def _every(network: Network, name: str, args: dict) -> dict:
    """`args` with an "all" in place of ids (ALL_IDS) as the ids it means."""
    for (tool, key), ids in ALL_IDS.items():
        if tool == name and str(args.get(key)).strip("[]'\" ").lower() in _ALL_WORDS:
            args = {**args, key: ids(network)}
    return args


def _road_from_other_end(network: Network, road_id: str) -> str | None:
    """The id of the road `road_id` names from its other end ("2,2-2,3" for the road 2,3-2,2), or None."""
    for split in [i for i, char in enumerate(road_id) if char == "-"]:
        other = f"{road_id[split + 1:]}-{road_id[:split]}"
        if other in network.roads or other in network.approach_roads:
            return other
    return None


def _swapped(value, old: str, new: str):
    """`value` (arguments: text, lists, objects) with every `old` string, whole, replaced by `new`."""
    if isinstance(value, str):
        return new if value == old else value
    if isinstance(value, list):
        return [_swapped(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _swapped(item, old, new) for key, item in value.items()}
    return value


def describe_changes(before: Network, after: Network, limit: int = 25) -> list[str]:
    """What changed between two states of the network, one line each: how the assistant
    hears about the user's own edits since its last reply."""
    config = ("horizontal_corridors", "vertical_corridors", "default_spacing", "driving_side")
    if any(getattr(before, key) != getattr(after, key) for key in config):
        return [f"Generated a new network: a {after.vertical_corridors} x {after.horizontal_corridors} grid "
                f"({after.vertical_corridors} columns, {after.horizontal_corridors} rows), "
                f"{after.default_spacing:g} m spacing, driving on the {after.driving_side}"]
    lines = []
    old_nodes, new_nodes = {**before.nodes, **before.approach_nodes}, {**after.nodes, **after.approach_nodes}

    def kind(node_id: str, network: Network) -> str:
        return "junction" if node_id in network.nodes else "boundary end"

    lines += [f"Added {kind(i, after)} {i}" for i in new_nodes if i not in old_nodes]
    lines += [f"Deleted {kind(i, before)} {i}" for i in old_nodes if i not in new_nodes]
    moved = set()
    for node_id in old_nodes.keys() & new_nodes.keys():
        old, new = old_nodes[node_id], new_nodes[node_id]
        if hypot(new.x - old.x, new.y - old.y) > 0.5:
            moved.add(node_id)
            lines.append(f"Moved {node_id} by ({new.x - old.x:+.0f}, {new.y - old.y:+.0f}) m")
        if old.control != new.control and node_id in after.nodes:
            lines.append(f"{node_id}: {old.control} → {new.control}")
        for key, label in (("supply_vph", "supply"), ("demand_vph", "demand")):
            if getattr(old, key) != getattr(new, key):
                lines.append(f"{node_id}: {label} {getattr(old, key):g} → {getattr(new, key):g} veh/h")
        timing = ("signal_phases", "signal_method", "green_times_s", "cycle_min_s", "cycle_max_s",
                  "target_saturation", "stop_weight_s")
        if any(getattr(old, key) != getattr(new, key) for key in timing):
            lines.append(f"{node_id}: signal settings → {new.signal_phases} phases, {new.signal_method}, "
                         f"greens {', '.join(f'{green:g}' for green in new.green_times_s)} s, cycle "
                         f"{new.cycle_min_s:g}-{new.cycle_max_s:g} s, target {new.target_saturation:.0%}, "
                         f"stop weight {new.stop_weight_s:g} s")
    old_roads, new_roads = {**before.roads, **before.approach_roads}, {**after.roads, **after.approach_roads}
    lines += [f"Added road {i} ({new_roads[i].from_node} → {new_roads[i].to_node})"
              for i in new_roads if i not in old_roads and i not in new_nodes]  # an added end names its road
    lines += [f"Deleted road {i}" for i in old_roads if i not in new_roads and i not in old_nodes]
    for road_id in old_roads.keys() & new_roads.keys():
        old, new = old_roads[road_id], new_roads[road_id]
        changes = [f"{label} {getattr(old, key)} → {getattr(new, key)}"
                   for key, label in (("lanes_per_direction", "lanes/direction"), ("speed_limit_kmh", "speed"),
                                      ("direction", "direction"), ("lane_changing", "lane changing"),
                                      ("road_type", "type"), ("overpass", "overpass"), ("pockets", "turn pockets"),
                                      ("cycle_lanes", "cycle lanes to"), ("footpaths", "footpaths"))
                   if getattr(old, key) != getattr(new, key)]
        if (old.from_node, old.to_node) == (new.to_node, new.from_node):
            changes.append(f"now runs {new.from_node} → {new.to_node}")
        if abs(old.length_m - new.length_m) > 0.5 and not {new.from_node, new.to_node} & moved:
            changes.append(f"length {old.length_m:.0f} → {new.length_m:.0f} m")
        if changes:
            lines.append(f"Road {road_id}: {', '.join(changes)}")
    old_stops, new_stops = before.bus_stops, after.bus_stops
    for stop_id, new in new_stops.items():
        old = old_stops.get(stop_id)
        if old is None or (old.road, old.direction, old.dwell_s, old.boarding_per_h, old.alighting_percent) != \
                (new.road, new.direction, new.dwell_s, new.boarding_per_h, new.alighting_percent) \
                or abs(old.distance_m - new.distance_m) > 0.5:
            road = after.road(new.road)
            serves = {"forward": f"{road.from_node} → {road.to_node}", "backward": f"{road.to_node} → {road.from_node}",
                      "both": "both directions"}[new.direction]
            stands = (f"{new.boarding_per_h:g} passengers an hour boarding, {new.alighting_percent:g} % of each load "
                      f"alighting" if new.boarding_per_h or new.alighting_percent else f"buses stand {new.dwell_s:g} s")
            lines.append(f"{'Added' if old is None else 'Changed'} bus stop {stop_id}: {new.distance_m:.0f} m "
                         f"along road {road.id} from {road.from_node}, serving {serves}, {stands}")
    lines += [f"Deleted bus stop {stop_id}" for stop_id in old_stops if stop_id not in new_stops]
    lines += [f"Placed institution {key} ({item.kind}) at ({item.x:.0f}, {item.y:.0f}) m, its circle "
              f"{item.radius_m:.0f} m" for key, item in after.institutions.items() if key not in before.institutions]
    lines += [f"Deleted institution {key}" for key in before.institutions if key not in after.institutions]
    lines += [f"Added path {key} ({way.start} to {way.end})" for key, way in after.walkways.items()
              if key not in before.walkways]
    lines += [f"Deleted path {key}" for key in before.walkways if key not in after.walkways]
    lines += [f"Added spawn point {key} at ({p.x:.0f}, {p.y:.0f}) m" for key, p in after.spawn_points.items()
              if key not in before.spawn_points]
    lines += [f"Deleted spawn point {key}" for key in before.spawn_points if key not in after.spawn_points]
    for pair in sorted(set(before.spawn_demand) | set(after.spawn_demand)):
        old, new = before.spawn_demand.get(pair), after.spawn_demand.get(pair)
        if old != new:
            lines.append(f"Pedestrians {pair}: {old or 0:g} → {new or 0:g} an hour")
    lines += [f"Added {c.kind} {key} on {c.road}, {c.distance_m:.0f} m along" for key, c in after.pedestrian_crossings.items()
              if key not in before.pedestrian_crossings]
    lines += [f"Deleted crossing {key}" for key in before.pedestrian_crossings if key not in after.pedestrian_crossings]
    for pair in sorted(set(before.walk_demand) | set(after.walk_demand)):
        old, new = before.walk_demand.get(pair), after.walk_demand.get(pair)
        if old != new:
            lines.append(f"Pedestrians {pair}: {old or 0:g} → {new or 0:g} an hour")
    if before.route_choice != after.route_choice:
        choice = after.route_choice
        lines.append("Route choice off: every vehicle takes the fastest route" if not choice else
                     f"Route choice: {choice.get('routes', 3)} routes a pair by path-size logit, theta "
                     f"{choice.get('theta_per_min', 0.5):g}/min, {len(choice.get('link_times_s', {}))} measured link times")
    if len(lines) > limit:
        lines = lines[:limit] + [f"...and {len(lines) - limit} more changes"]
    return lines


def _split_items(split) -> list[tuple[str, object]]:
    """(vehicle type, percent) from a mode_split given as a dict or a list of {mode, percent}."""
    if isinstance(split, dict):
        return list(split.items())
    if isinstance(split, list):
        return [(item.get("mode", "?"), item.get("percent", "?")) for item in split if isinstance(item, dict)]
    return []


ERROR_KINDS = (  # (what the message says, its type, what to do): ChatSUMO's structured errors, so a model can recover
    (("unknown id", "no road called", "no junction called", "no bus stop called", "no end or junction called",
      "no extension called",
      "no run called", "no experiment called", "no test called", "no figure", "no such", "isn't a signalised"),
     "unknown_id", "Use one of the ids it lists, or read them first (list_roads, list_junctions, read_results)."),
    (("no route",), "unroutable", "Connect the two places (add_road) or pick ends that are joined (analyse_network)."),
    (("a run is going on", "a run or test is going on", "stop it first"), "busy",
     "Wait for the user's run to end (they Stop it), then call again."),
    (("no demand", "no hourly demand"), "no_demand", "Generate demand first (generate_demand or set_od_demand)."),
    (("must be", "is one of", "takes ", "give ", "needs "), "bad_argument", "Call again with the value the message asks for."),
    (("nothing would change",), "no_change", "It is like that already: nothing to do."),
)


def already_so(message: str) -> dict:
    """An edit that would change nothing, as the answer it is: what was asked is how it already is."""
    return {"unchanged": True, "already": message,
            "note": "Nothing needed changing: it is like that already. Tell the user so; don't call it again."}


def explain_error(message: str, network: Network | None = None) -> dict:
    """A refused tool call as data: the message, its type and what to do (ERROR_KINDS); for an unknown id, the
    network's nearest ids by spelling (difflib), so the model can call again without guessing."""
    import difflib  # noqa: PLC0415

    lowered = message.lower()
    kind, fix = "refused", "Read the message: it says why; change the call or ask the user."
    for words, found, advice in ERROR_KINDS:
        if any(word in lowered for word in words):
            kind, fix = found, advice
            break
    answer = {"error": message, "error_type": kind, "suggested_fix": fix}
    if kind == "unknown_id" and network is not None:
        quoted = re.findall(r"(?:called|id) '([^']+)'|called ([\w,.<>-]+)", message)
        wanted = next((a or b for a, b in quoted), "")
        known = [*network.roads, *network.approach_roads, *network.nodes, *network.bus_stops, *network.institutions]
        close = difflib.get_close_matches(wanted, known, n=5, cutoff=0.5) if wanted else []
        other = _road_from_other_end(network, wanted) if wanted else None
        if other:  # a road named from its other end comes first
            close = [other, *[item for item in close if item != other]][:5]
        if close:
            answer["did_you_mean"] = close
    return answer


def describe_call(name: str, args: dict) -> str:
    """One line saying what a tool call does, for the chat and the approval card."""
    a = args if isinstance(args, dict) else {}

    def listed(key: str) -> str:
        return ", ".join(_ids(a.get(key, [])))

    def crossing() -> str:
        return f", {a['crossings']} where it crosses roads" if a.get("crossings") else ""

    if name == "set_junction_control":
        return f"Make {listed('junctions')} {a.get('control')}"
    if name == "update_roads":
        labels = {"lanes_per_direction": "lanes/direction", "add_lanes": "lanes added each way", "speed_limit_kmh": "speed (km/h)",
                  "direction": "direction", "lane_changing": "lane changing", "road_type": "type",
                  "length_m": "length (m)", "pocket_junction": "turn pocket at", "pocket_lanes": "pocket lanes",
                  "pocket_length_m": "pocket length (m)", "pocket_side": "pocket side"}
        changes = ", ".join(f"{label} → {a[key]}" for key, label in labels.items() if a.get(key) is not None)
        return f"Change road {listed('roads')}: {changes or 'nothing'}"
    if name == "add_junction":
        return f"Add a junction at ({float(a.get('x', 0)):.0f}, {float(a.get('y', 0)):.0f}) m"
    if name == "add_road":
        return f"Add a road {a.get('from_junction')} ↔ {a.get('to_junction')}{crossing()}"
    if name == "add_boundary_end":
        return (f"Add a boundary end from {a.get('from_junction')} at "
                f"({float(a.get('x', 0)):.0f}, {float(a.get('y', 0)):.0f}) m{crossing()}")
    if name == "move_junctions":
        return f"Move {listed('junctions')} by ({float(a.get('dx', 0)):+.0f}, {float(a.get('dy', 0)):+.0f}) m"
    if name == "delete_objects":
        return f"Delete {listed('ids')}"
    if name == "build_interchange":
        return (f"Build a {a.get('kind')} interchange at {listed('junctions')}"
                f" ({'keep' if a.get('replace') is False else 'replace'} existing roads)")
    if name == "add_slip_road":
        return f"Add a {'single-lane' if _bool(a.get('single_lane', False)) else 'two-lane'} slip road from {listed('roads')}"
    if name == "set_end_flows":
        flows = ", ".join(f"{label} → {a[key]:g} veh/h" for key, label in
                          (("supply_vph", "supply"), ("demand_vph", "demand"))
                          if isinstance(a.get(key), (int, float)))
        return f"Set {a.get('end')} {flows or 'flows'}"
    if name == "generate_grid":
        return (f"Replace the whole network with a new {a.get('vertical')} × {a.get('horizontal')} grid "
                f"({a.get('vertical')} columns, {a.get('horizontal')} rows)")
    if name == "set_signal_timing":
        what = ", ".join(f"{key.replace('_', ' ')} {a[key]}" for key in ("method", "phases", "green_times_s", "cycle_min_s",
                                                                          "cycle_max_s", "target_saturation_percent",
                                                                          "stop_weight_s") if a.get(key) is not None)
        return f"Time {listed('junctions')}: {what or 'no change'}"
    if name == "run_simulation":
        return f"Test the network: a {a.get('duration_min')} min run, seed {a.get('seed', 'from the Seed box')}"
    if name == "run_experiment":
        scenarios = [s.get("name", "?") for s in a.get("scenarios") or [] if isinstance(s, dict)]
        factors = [f for f in a.get("factors") or [] if isinstance(f, dict)]
        conditions = [c.get("name", "?") for c in a.get("conditions") or [] if isinstance(c, dict)]
        seeds = a.get("seeds") if isinstance(a.get("seeds"), list) else [a.get("seeds")]
        designs = 1
        for factor in factors:
            designs *= len(factor.get("levels") or []) or 1
        what = (" × ".join(f"{f.get('name')} ({len(f.get('levels') or [])} levels)" for f in factors) if factors
                else ", ".join(scenarios))
        count = (designs if factors else len(scenarios)) * max(len(conditions), 1) * len(seeds)
        return (f"Run an experiment: {what}" + (f" under {', '.join(conditions)}" if conditions else "")
                + f" × {len(seeds)} seed{'s' if len(seeds) != 1 else ''}, {a.get('duration_min')} min each ({count} runs)")
    if name == "set_extension":
        return f"{'Tick' if _bool(a.get('on')) else 'Untick'} the extension {a.get('name')}"
    if name == "preflight_check":
        return f"Check the network: runs at 10 % and 50 % of the demand, {a.get('duration_min') or 15} min each"
    if name == "schedule_emergency":
        rows = a.get("schedules") if isinstance(a.get("schedules"), list) else []
        listed = "; ".join(f"{row.get('from')} → {row.get('to')}" + (f" every {row['every_min']} min" if row.get("every_min")
                           else f" after {row['after_min']} min" if row.get("after_min") else " once")
                           for row in rows if isinstance(row, dict))
        return f"Schedule emergency vehicles: {listed or 'none'}" + (", and send them now" if _bool(a.get("dispatch_now")) else "")
    if name == "start_batch":
        tests = " and ".join(kind for kind, key in (("TSP", "test_tsp"), ("DEVL", "test_devl")) if _bool(a.get(key)))
        return f"Start a batch: seeds {a.get('seeds')}, {a.get('duration_min')} min each" + (f", testing {tests}" if tests else "")
    if name == "replay_run":
        return f"Replay the run {a.get('path')}"
    if name == "calibrate_demand":
        counts = a.get("counts") if isinstance(a.get("counts"), list) else []
        return f"Fit the hourly OD matrix to {len(counts)} counted flow{'s' if len(counts) != 1 else ''}"
    if name == "build_scenario":
        what = a.get("place") or (f"a {a['grid']} grid" if a.get("grid") else "the network as it is")
        tests = len(a.get("what_if") or []) if isinstance(a.get("what_if"), list) else 0
        return f"Build and test {what}" + (f" with {tests} what-if{'s' if tests != 1 else ''}" if tests else "")
    if name == "assign_routes":
        if _bool(a.get("off", False)):
            return "Route choice off: every vehicle on the fastest route"
        return (f"Route choice by path-size logit, {a.get('routes', 3)} routes a pair, {a.get('iterations', 4)} "
                f"averaging runs of {a.get('duration_min', 30)} min")
    if name == "optimise_signals":
        return f"Optimise the fixed-time signals at {listed('junctions')} by a genetic algorithm ({a.get('duration_min')} min runs)"
    if name == "choose_modes":
        modes = [str(item.get("mode")) for item in a.get("alternatives", []) if isinstance(item, dict)] \
            if isinstance(a.get("alternatives"), list) else []
        return f"Split the pairs between {', '.join(modes) or 'the types'} by mode choice (logit)" + \
            (f", times from {a['from_run']}" if a.get("from_run") else "")
    if name == "place_map":
        where = a.get("place") or f"{a.get('lat')}, {a.get('lon')}"
        return f"Put the map of {where} under the network"
    if name == "add_bus_stop":
        return f"Add a bus stop on {a.get('road')} ({a.get('direction', 'both')})"
    if name == "add_crossing":
        return f"Add a {a.get('kind', 'zebra')} crossing on {a.get('road')}" + \
            (f", {a['at_m']:g} m along" if isinstance(a.get("at_m"), (int, float)) else "")
    if name == "add_path":
        return f"Add a path from {a.get('start')} to {a.get('end')}"
    if name == "set_pedestrian_demand":
        people = f"{a['people_per_hour']:g} " if isinstance(a.get("people_per_hour"), (int, float)) else ""
        return f"Set {people}pedestrians an hour on {', '.join(map(str, a.get('pairs') or [])) or 'pairs'}"
    if name == "pedestrian_summary":
        return "Read the network for pedestrians"
    if name == "add_spawn_points":
        return f"Place {len(a.get('points') or [])} pedestrian spawn points" if a.get("points") else \
            "Place pedestrian spawn points"
    if name == "update_bus_stop":
        return f"Change bus stop {a.get('stop')}"
    if name == "update_institution":
        return f"Change institution {a.get('id')}"
    if name == "update_vehicle_type":
        return f"Change the vehicle type {a.get('name')}"
    if name == "note_plan":
        return "Note the plan"
    if name == "create_vehicle_types":
        made = [f"{spec.get('name')} ({spec.get('kind')}" + (f", every {spec['headway_min']} min" if spec.get("headway_min") else "")
                + ")" for spec in (a.get("types") if isinstance(a.get("types"), list) else []) if isinstance(spec, dict)]
        return f"Add vehicle types: {', '.join(made) or 'none'}"
    if name == "deploy_institutions":
        kinds = listed("kinds") or "any kind"
        return f"Place {a.get('count')} institutions at random on clear ground ({kinds})"
    if name == "place_institution":
        where = f"near {a['near']}" if a.get("near") else f"at {a.get('x')}, {a.get('y')}"
        circle = f", catchment {a['radius_m']:g} m" if isinstance(a.get("radius_m"), (int, float)) else ""
        return f"Place a {a.get('kind')} {where}{circle}"
    if name == "institution_mode_split":
        which = listed("institutions") or "every institution"
        calibrated = (" calibrated to " + ", ".join(f"{k} {v:g} %" for k, v in a["observed_percent"].items())
                      if isinstance(a.get("observed_percent"), dict) and a["observed_percent"] else "")
        return f"Set the mode split of {which} by logit{calibrated}"
    if name == "network_from_background":
        what = (f"the image's {a['road_colour']} roads" if a.get("road_colour") else
                f"OpenStreetMap's {a.get('road_classes') or 'all'} roads, "
                f"{float(a.get('width_m') or 1200):g} × {float(a.get('height_m') or 1200):g} m round its centre")
        return f"Replace the whole network with {what}"
    if name == "generate_demand":
        if a.get("pairs"):
            which = "the pairs " + ", ".join(_ids(a["pairs"])[:4])
        else:
            which = "every pair" + (f" from {listed('origins')}" if a.get("origins") else "") + \
                (f" to {listed('destinations')}" if a.get("destinations") else "")
        total = (f"{a['total']:g} {'trips/day' if a.get('demand') == 'daily' else 'veh/h'} in all"
                 if isinstance(a.get("total"), (int, float)) else "the ends' supply and demand")
        return f"Generate {a.get('demand') or 'hourly'} demand for {which} by a gravity model from {total}"
    if name == "scale_demand":
        which = ("the pairs " + ", ".join(_ids(a["pairs"])[:4])) if a.get("pairs") else "every pair" +             (f" from {listed('origins')}" if a.get("origins") else "") + (f" to {listed('destinations')}" if a.get("destinations") else "")
        how = (f"so the busiest junction runs at {a['degree_of_saturation']:g} of capacity"
               if isinstance(a.get("degree_of_saturation"), (int, float)) else f"by {a.get('factor')}")
        return f"Scale the demand of {which} {how}"
    if name in ("set_od_demand", "shape_daily_curve"):
        if a.get("pairs"):
            named = [str(p).replace("->", " → ") for p in _ids(a["pairs"])]
            which = ", ".join(named[:4]) + (f" and {len(named) - 4} more" if len(named) > 4 else "")
        else:
            which = "every pair" + (f" from {listed('origins')}" if a.get("origins") else "") \
                + (f" to {listed('destinations')}" if a.get("destinations") else "")
        if name == "shape_daily_curve":
            how = (f"the {a['preset']} shape" if a.get("preset") else f"a degree-{len(a['polynomial']) - 1} "
                   "polynomial" if isinstance(a.get("polynomial"), list) else
                   f"{len(a['points'])} curve points" if isinstance(a.get("points"), list) else "a new shape")
            return f"Shape the daily curve for {which} with {how}"
        unit = "trips/day" if a.get("demand") == "daily" else "veh/h"
        split = a.get("mode_split")
        parts = [f"{a['value']} {unit}" if a.get("value") is not None else "",
                 f"{a['arrival_pattern']} arrivals" if a.get("arrival_pattern") else "",
                 f"peak-hour factor {a['peak_hour_factor']}" if a.get("peak_hour_factor") is not None else
                 "15-minute quarters" if a.get("quarters") is not None else "",
                 " · ".join(f"{mode} {share}%" for mode, share in _split_items(split))]
        return f"Set {a.get('demand', '')} demand for {which}: {', '.join(p for p in parts if p) or 'no change'}"\
            .replace("Set  demand", "Set demand")
    if name == "read_od_matrix":
        return "Read the OD matrix"
    if name == "show_on_map":
        return f"Show {a.get('id')} on the map"
    shown = ", ".join(f"{key}={value}" for key, value in a.items())
    return f"{name}({shown})"
