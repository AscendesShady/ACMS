"""Reading a model's arguments for the assistant's tools (no Qt).

Coercing types, filling in defaults, turning "all" into ids and repairing a call before it runs, plus ToolError and
the small argument readers every tool module uses; moved out of network_tools.py, which re-exports them.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
from math import isfinite

from .models import Network
from .tool_schemas import TOOLS

class ToolError(Exception):
    """Why a tool couldn't run; the model gets this text back and can correct itself."""


LIST_LIMIT = 60  # rows per list; filters narrow bigger networks (150 rows of roads were ~25k characters a call)


SATURATION_RANGE = (0.1, 1.2)


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


def _limited(rows: list[dict], what: str) -> dict:
    result = {what: rows[:LIST_LIMIT], "count": len(rows)}
    if len(rows) > LIST_LIMIT:
        result["note"] = f"Showing {LIST_LIMIT} of {len(rows)}; filter to see the rest."
    return result


def _sides(width_m, height_m) -> tuple[float, float]:
    """An area's width_m x height_m as given; one side alone makes a square."""
    width = float(width_m) if width_m is not None else float(height_m)
    return width, float(height_m) if height_m is not None else width


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
        every = [f"{key} 'all' is every one" for key in missing if (name, key) in ALL_IDS]  # never list them all
        raise ToolError(f"{name} needs {', '.join(missing)}{f' ({chr(59).join(every)})' if every else ''}. "
                        f"{signature(name)}.")
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
