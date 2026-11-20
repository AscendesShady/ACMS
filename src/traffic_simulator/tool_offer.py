"""Which assistant tools and skills a request is offered (no Qt).

The keyword groups, the SKILLS texts, place-name detection, tools_for, find_tools (expand_tools) and the contradicts
check, apart from the tools themselves, so tuning what is offered can't touch them.
"""

from __future__ import annotations

import re

from .tool_args import _bool, _ids
from .tool_schemas import TOOLS, tool_specs

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
    "metro": (r"metro|\bmrt\b|rail|subway|underground|\btube\b|train|station|transit|ridership|crowd",
              ("add_metro_line", "update_metro_line", "set_metro_demand", "metro_report", "delete_objects",
               "choose_modes", "institution_mode_split")),
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
    # not "peak": peak traffic is a demand level (demand's words), and a run's tools are 6k characters more
    "run": (r"simulat|\brun|test|result|perform|throughput|delay|speed|queue|slow|congest|jam|how (?:well|bad|good)|"
            r"busiest|worst|live",
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
    "pedestrians": (r"pedestri|walk|footpath|sidewalk|pavement|zebra|crossing|overbridge|footbridge|\bpath",
                    ("pedestrian_summary", "add_crossing", "add_path", "set_pedestrian_demand", "add_spawn_points",
                     "update_roads",
                     "delete_objects")),
    # a cycle lane is ridden only by a bicycle type: offered with the types
    "cycling": (r"cycle lane|bike lane|cycle path|cyclist|cycling|bicycl",
                ("update_roads", "pedestrian_summary", "list_vehicle_types", "create_vehicle_types")),
    "transit": (r"\bbus(?:es)?\b|bus stop|\bstops?\b|transit|passenger|vehicle type|truck|lorry|bike|bicycle|taxi|van\b|fleet|headway|tram",
                ("add_bus_stop", "update_bus_stop", "list_vehicle_types", "create_vehicle_types",
                 "update_vehicle_type")),
    "extensions": (r"extension|\btsp\b|priority|devl|dbl|bus lane|emergenc|ambulance|fire|police|batch|replay|agent|decision|"
                   r"dispatch|research log|complian|sensitiv",
                   ("read_extensions", "set_extension", "schedule_emergency", "start_batch", "replay_run",
                    "test_agent_compliance", "read_decisions", "decision_sensitivity")),
    "docs": (r"\bcode\b|source|function|module|formula|implement|where does|how (?:do|can) i\b|how to\b|readme|"
             r"setup|set up|install|api key|\bkeys?\b|button|menu|shortcut|undo", ("read_source",)),
}
# Skills: how to do a group's kind of task well, sent with a request only when its words offer that group (and with
# find_tools' tools), not every request: as one system-prompt list they were 4.5k characters of every request. The
# words in quotes are examples of what users say.
SKILLS = {
    "build": "\"a city of W x H metres\", \"a new grid\": generate_grid with vertical = W / spacing_m + 1 columns and "
             "horizontal = H / spacing_m + 1 rows (spacing_m 200-400; a dense city 200-250). It replaces the network; "
             "to keep some roads, edit the network instead (add_road, delete_objects, update_roads). No pedestrian walkways "
             "unless the user's words ask for them (footpaths, walkways, sidewalks, pedestrians): generate_grid "
             "footpaths false otherwise; never ask about them or offer them.",
    "place": "A real named place (\"map out Dhanmondi\", \"Times Square's roads\"): place_map with the place as "
             "the user named it, a city or country only if they gave one (never a guessed one: the search finds "
             "it), then network_from_background (never a grid in its place), footpaths "
             "'none' (no walkways, crossings or paths) unless the user's words ask for them ('tagged' as mapped, "
             "'all' every road); never ask about them or offer them. place_map sets the work zone and "
             "network_from_background builds it: leave width_m, height_m and road_classes out of both for a "
             "neighbourhood, old town or downtown (1200 m, every street); for a size the user names give it to "
             "place_map (\"3 km\": width_m 3000), road_classes 'main' past 2500. Lanes the user "
             "gives go in that call: \"main roads 2 to 3 lanes\" -> main_lanes [2, 3], \"side streets single lane\" -> "
             "local_lanes [1, 1]; it holds every road in them, so change no roads after. It keeps only roads a car can "
             "drive between the ends, so every OD pair has a route: report its junctions, roads, "
             "roads_by_lanes_per_direction and streets_left_out, and don't add those streets back. Then demand, if "
             "asked.",
    "demand": "\"busy\", \"heavy\", \"rush hour\", \"peak\", \"quiet\" traffic: a level, not a place. generate_demand "
              "with degree_of_saturation: light 0.4, medium 0.65, busy/heavy/peak 0.85, near capacity 0.95. Hourly is "
              "the default; a whole day is demand \"daily\" then shape_daily_curve. \"Busier\", \"quieter\", \"30% "
              "more\", \"double it\": scale_demand on the demand there is (factor 1.3, 0.7, 1.3, 2), not a new build. "
              "Trips ending part way (\"30% of buses stop at N12\"): set_od_demand drop_offs on those pairs, at a "
              "junction on each pair's route (a refusal lists them).",
    "study": "A whole study described (place or grid, traffic level, signals, buses, what-ifs, reports): "
             "build_scenario does all of it in one call.",
    "signals": "\"use SCOOT\", \"switch the signals to Webster\", \"make 2,2 actuated\": set_signal_timing with that "
               "method (the junctions named, else every signalised one from list_junctions); \"north green 1-120, "
               "east 121-150\", \"amber 4 s\": windows by arm (N/E/S/W) and amber_s. optimise_signals only tunes "
               "fixed-time greens by many test runs, when the user asks to optimise them. \"why is junction X "
               "slow\": run_simulation if there is no run yet, then diagnose_signal (a signal) or junction_los.",
    "run": "\"simulate\", \"test it\", \"how does it perform\": run_simulation (30 min unless said), then read_results "
           "or analyse_run.",
    "experiment": "\"would X help\", \"compare\", \"what if\": never edit the open network to test. note_plan (goal, "
                  "hypothesis), then run_experiment with the network as built first and each other scenario one "
                  "change (changes: edit tools applied to a copy; demand_scale to stress it), 3 or more seeds; then "
                  "read_results and note_plan with your conclusion. Call a difference only where the comparison says "
                  "clear; where replications_needed is above the seeds run, say more seeds are needed.",
    "institutions": "\"demand from buildings / institutions\", \"a dense city with homes, schools, offices\": "
                    "deploy_institutions (count 1-50: about 10 a square km, 30 for a 2 x 1.5 km city; 50 when dense). "
                    "A run spawns their trips itself, on top of the OD matrix's demand. \"more people\": there is no "
                    "population figure: update_institution raises their people (else scale_demand); say that trips, "
                    "not residents, changed.",
    "metro": "The MRT is its own network: add_metro_line builds a line through stations (merge true makes an "
             "interchange where it crosses another), update_metro_line changes its service, set_metro_demand fills "
             "its OD matrix (station to station), delete_objects removes a line; metro_report gives its loads and "
             "institution_mode_split buildings' MRT shares. Road trips never turn into MRT trips. Lines curve "
             "(300 m at least, turns under 90 deg); riders change once at an interchange; the service is the schedule.",
    "pedestrians": "Pedestrians walk footpaths (update_roads footpaths), paths (add_path) and crossings (add_crossing: "
                   "a zebra, where traffic stops for them, or an overbridge); at a junction's arm they wait for a gap. "
                   "Walkers come from set_pedestrian_demand and buildings' Walk share; pedestrian_summary says who "
                   "can't walk anywhere; a run's results give walk times, kerb waits and each crossing's figures.",
    "cycling": "Cycle lanes: update_roads cycle_lanes (both ways or one); only a bicycle vehicle type rides them "
               "(create_vehicle_types).",
}


def skills_for(request: str, seen: str = "") -> str:
    """The [Skills] block for a request: the SKILLS of the groups its words point to, less any already in `seen`
    (the conversation so far: sent once, they stay in it); "" when none."""
    texts = [SKILLS[group] for group in tool_groups(request) if group in SKILLS and SKILLS[group] not in seen]
    return "\n\n[Skills]\n" + "\n".join(f"- {text}" for text in texts) + "\n[End of skills]" if texts else ""


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


# "a network of X" names a place even typed in lower case ("Create a netowrk of dhanmondi dhaka": no capital, so no
# map tools offered, and grok-4.7 placed the map, said it would build the streets next and stopped)
_OF_A_PLACE = re.compile(r"\b(?:net\w*|roads?|streets?|map)\s+(?:of|for|in)\s+(?!(?:the|a|an|my|this|that|it|its|"
                         r"roads?|streets?|junctions?|lanes?|grid|size)\b)[a-z]{3,}")
# ...and so does "a dhanmondi network" (qwen3.5 4B built a made-up 8 x 6 grid for it): the word before network, roads
# or streets, unless it's one that describes a network rather than naming somewhere
_A_PLACES_NETWORK = re.compile(r"\b([a-z]{3,})\s+(?:net\w*|roads|streets)\b")
_NOT_A_PLACE = set("""the this that its new grid road small big large simple nice dense city town test sample whole entire
full basic current existing urban rural traffic local simulation demo square circular radial ring toy real complex busy
highway motorway freeway metro mrt rail bus pedestrian walking cycle bike transit own another different bigger smaller
larger same empty blank default standard regular typical random style like main side minor major arterial way all some
these those both other every more fewer two three four five six one city's""".split())


def _places_in(text: str) -> bool:
    """Whether the request names somewhere: capitalised (_places_named) or not ("a network of X", "an X network")."""
    lowered = text.lower().replace("-", " ")
    return _places_named(text) or bool(_OF_A_PLACE.search(lowered)) or any(
        word not in _NOT_A_PLACE for word in _A_PLACES_NETWORK.findall(lowered))
STUDY_GROUPS = {"build", "place", "demand", "run", "signals", "experiment"}  # 3 or more of them: a whole study


def tool_groups(text: str) -> list[str]:
    """The groups whose words the text uses (TOOL_GROUPS), "place" when it names somewhere, and "study" when it
    touches three or more of STUDY_GROUPS or both builds and tests (build it, run it: build_scenario's kind)."""
    lowered = text.lower()
    found = [name for name, (pattern, _) in TOOL_GROUPS.items() if re.search(pattern, lowered)]
    if "place" not in found and _places_in(text):
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


def _adds_walkways(name: str, args: dict) -> bool:
    """Whether a call puts pedestrian walkways, crossings or paths on the network (the user's to ask for, never the
    model's to offer: 2026-10-30)."""
    if name == "generate_grid":
        return args.get("footpaths") is not None and _bool(args["footpaths"])
    if name in ("network_from_background", "update_roads"):
        return str(args.get("footpaths") or "none").strip().lower() != "none"
    return name in ("add_crossing", "add_path")


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
        try:
            ids = [str(item).strip() for item in _ids(args.get("ids") or [])]
        except ValueError:  # objects for ids: the tool's own check refuses them, saying how to give them
            ids = []
        for road in re.findall(r"[\w.]+,[\w.]+-[\w.]+,[\w.]+|N\d+-N\d+", request):
            ends = road.split("-", 1) if road.startswith("N") else re.split(r"-(?=[\w.]+,)", road, maxsplit=1)
            rest = request.replace(road, " ")  # a junction the user names apart from the road is theirs to delete
            named = [end for end in ends if re.search(rf"(?<![\w.,-]){re.escape(end)}(?![\w.,-])", rest)]
            if road not in ids and len(ends) == 2 and set(ends) <= set(ids) and not named:
                return (f"The user asked to delete road {road}, but this deletes its junctions {ends[0]} and {ends[1]} "
                        f"(and every road at them). Call again with ids [\"{road}\"].")
    # lanes the user gave, applied with update_roads after the build instead (qwen3.5 4B, 2 of 3 stress prompts)
    said = re.search(r"\b(?:\d|one|two|three|four|five|six|single)(?:\s*(?:to|or)\s*(?:\d|two|three|four|five|six))?"
                     r"\s+lanes?\b", words)
    if name == "network_from_background" and said and not {"main_lanes", "local_lanes"} & set(args):
        return (f"The user gave lanes (\"{said.group(0)}\"): put them in this call, main_lanes [fewest, most] for main "
                "roads and/or local_lanes for side streets, e.g. \"2 to 3 lanes\" -> [2, 3], \"single lane\" -> [1, 1]. "
                "(Lanes for particular roads only: main_lanes [1, 6] here, then update_roads.)")
    if name == "generate_grid" and _places_in(request) and "grid" not in words:
        return ("The user asked for a real place's roads, not an invented grid: generate_grid would put a made-up grid "
                "in its place. Use place_map (if the map isn't there yet), then network_from_background; or tell the "
                "user why that can't be done.")
    if _adds_walkways(name, args) and not re.search(TOOL_GROUPS["pedestrians"][0], words):
        return ("The user didn't ask for pedestrian walkways: build without them (generate_grid footpaths false, "
                "network_from_background footpaths 'none') and don't offer them. Walkways, crossings and paths only "
                "when the user's own words ask for footpaths, walkways or pedestrians.")
    for key, spec in tool["parameters"].get("properties", {}).items():
        value = args.get(key)
        if not (key == "method" or key.endswith("_method")) or "enum" not in spec or not isinstance(value, str):
            continue
        named = [c for c in spec["enum"] if re.search(rf"\b{re.escape(str(c).replace('_', ' '))}\b", words.replace("_", " "))]
        if named and value.lower() not in [str(c).lower() for c in named]:
            return (f"The user asked for {', '.join(map(str, named))}, but this call sets {key} {value!r}. Call again "
                    f"with what they asked for, or say why not.")
    return None


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
    skills = [SKILLS[group] for group in tool_groups(need) if group in SKILLS]  # how to use them well
    return {"added": [{"name": spec["name"], "does": spec["description"].split(". ")[0]} for spec in added],
            "note": "Call them now as you need them.", **({"skills": skills} if skills else {})}
