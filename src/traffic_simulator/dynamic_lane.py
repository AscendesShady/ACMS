"""The dynamic lane controller the Dynamic Emergency Vehicle Lane (DEVL) and Dynamic Bus Lane (DBL) extensions share:
at each signalised junction, it clears the kerbside lane of an approach for one kind of vehicle (`Kind`: emergency
vehicles, buses), gated by one TRUE/FALSE per request from System 1 or System 2. No Qt here: the physics and batch
threads run it; each extension keeps its runs (`_runs`, `_latest`), its panel's choice and figures, and draws the
lane signal heads `heads` places.

Each junction (`Junction`) shows the state it is given (its plan, or an earlier override's) unless it is clearing a
lane: "plan" -> "to_red" (every green ending after its minimum green, amber, all-red) -> "clear" (only the kerbside
lane of the vehicle's arm green) -> "lane_amber" -> "all_red" -> the plan again, or straight on to the next granted
request. One controller clears a junction at a time: the run's `holds` (Simulation.lane_holds, in the junction view)
say which, junction -> (kind's key, its priority, when last held, the arm it clears for); leaving the plan takes the hold, coming back to it
lets it go. A controller whose granted request waits on a lower-priority holder leaves ("want", junction) ->
(its priority, when); that holder ends its clearance early (to lane amber), or, still going to red, goes back to its
plan at all-red with its request granted and not yet green, to try again once the junction is free. A hold or want
not renewed for STALE_S (its controller unticked) no longer counts. Without "holds" in the view, no arbitration.
"""

import json
from dataclasses import dataclass, field
from math import hypot
from statistics import fmean, median
from typing import Callable

from . import road_words
from .models import stop_line_inset

QUEUED_KMH = 5.0  # slower than this, a vehicle ahead counts as queued
DETECT_S = 30.0  # check-in: within this many seconds of the stop line at the arm's speed limit, or held in a queue
CLEAR_MAX_S = 60.0  # the longest one vehicle's lane stays green
SIGNALS = {"G": "green", "Y": "amber", "R": "red"}
STALE_S = 2.0  # a hold or want older than this was left by a controller no longer running


@dataclass(frozen=True)
class Kind:
    """What one dynamic lane controller watches for and how it asks."""
    key: str  # "devl", "dbl": request keys (key/junction/arm/vehicle), feed types (key_request), measures (key_*)
    short: str  # "DEVL", "DBL": its sheets' names
    name: str  # "Dynamic Emergency Vehicle Lane": its summary sheet's description
    schema: str  # its telemetry's schema
    is_target: Callable[[dict], bool]  # a vehicle in a junction's view it clears the lane for
    vehicle_key: str  # the telemetry's field for that vehicle ("emergency_vehicle", "bus")
    vehicle: str  # the vehicle in words ("emergency vehicle", "bus")
    noun: str  # its requests in words ("emergency", "bus": "emergency requests")
    questions: dict  # System 1's typed questions
    tool: dict  # System 2's decide tool (activate, reason)
    role: str  # System 2's system prompt
    priority: int  # who goes first at a junction both want (emergency 2, bus 1)
    fields: tuple = field(default=())  # the vehicle's own view fields its telemetry adds (DBL's "people")
    # its telemetry in words (DBL's: road_words), what both systems read in place of the numbers; its telemetry then
    # also carries the people a clearance holds at red and the road ahead on the vehicle's route. None: the JSON (DEVL)
    words: Callable[[dict], dict] | None = None


def in_queue(vehicle: dict, ahead: list) -> bool:
    """Whether a vehicle is held in a queue: slower than QUEUED_KMH with a vehicle ahead of it in its lane (`ahead`:
    those nearer the stop line on its arm). With the distance, either checks it in."""
    return vehicle["speed_kmh"] < QUEUED_KMH and any(other["lane"] == vehicle["lane"] for other in ahead)


def telemetry(kind: Kind, junction: str, arm: str, vehicle: dict, context: dict, state: str) -> dict:
    """The approach's telemetry for the vehicle on it now (JSON-ready)."""
    view = context["arms"][arm]
    left = view["kerb_lane"]  # the lane it clears: the kerbside lane, past a kerb pocket's (the one it keeps to)
    ahead = [other for other in view["approaching"] if other["lane"] == left and other["distance_m"] < vehicle["distance_m"]]
    speed = vehicle["speed_kmh"] / 3.6
    message = {"schema": kind.schema, "type": f"{kind.key}_request", "time_s": round(context["time_s"], 1),
            "junction": junction, "approach": arm, "lane": left,
            kind.vehicle_key: {"id": vehicle["id"], "type": vehicle["type"], "distance_m": vehicle["distance_m"],
                               "speed_kmh": vehicle["speed_kmh"], "eta_s": round(vehicle["distance_m"] / max(speed, 1.0), 1),
                               "lane": vehicle["lane"], "in_the_lane": vehicle["lane"] == left,
                               **{name: vehicle.get(name) for name in kind.fields}},
            "signal": SIGNALS.get(state, "red"),
            "lane_ahead": {"vehicles": len(ahead), "queued": sum(other["speed_kmh"] < QUEUED_KMH for other in ahead)},
            "approach_queued": sum(other["speed_kmh"] < QUEUED_KMH for other in view["approaching"])}
    if kind.words is not None:  # what a clearance costs (everyone else at red: the vehicle's kerb lane goes) and the way on
        message["people_held"] = round(sum(other.get("people", 1.0) for road, info in context["arms"].items()
                                           for other in info["approaching"] if other["id"] != vehicle["id"]
                                           and (road != arm or other["lane"] != left)))
        message["route_ahead"] = context["route_ahead"](vehicle["id"]) if callable(context.get("route_ahead")) else []
    return message


def question_for(kind: Kind, system: str, model, message: dict) -> dict:
    """What the signal agent sends, in both forms (System 1's state and typed questions, System 2's messages and
    tool), from the kind's sentences (`Kind.words`) when it has them, else the telemetry; `system` and `choice` (its picked model) say who the panel chose (a batch's agent answers with its own)."""
    lane = message["lane_ahead"]
    traffic = "queued" if lane["queued"] else "moving" if lane["vehicles"] else "none"
    tool = kind.tool["name"]
    said = kind.words(message) if kind.words is not None else None  # the same sentences for both systems
    return {"system": system, "choice": model if model and model.get("system") == system else None,
            "telemetry": message, "state": said or {"traffic_in_lane": traffic, "signal": message["signal"]},
            "questions": {name: {"type": "noul", "instructions": text} for name, text in kind.questions.items()},
            "tools": [kind.tool], "answer_from": [tool, "activate"],
            "messages": [{"role": "system", "content": kind.role},
                         {"role": "user", "content": ("The situation now:\n" + road_words.text(said) if said else
                                                      "Telemetry now:\n" + json.dumps(message, indent=1))
                                                     + f"\n\nDecide: call {tool}."}]}


class Run:
    """One run's junctions, its requests (key -> what was asked and answered) and its telemetry feed."""

    def __init__(self, agent) -> None:
        self.agent, self.junctions, self.requests, self.feed, self.last_t = agent, {}, {}, [], None
        self.generation = getattr(agent, "generation", 0)  # the agent's run (SignalAgent.reset moves it on)


class Junction:
    """One signalised junction's controller: "plan" (passing its state on), "to_red" (every green ending after its
    minimum, amber, all-red), "clear" (only the kerbside lane of the vehicle's arm green), "lane_amber", "all_red",
    then the plan again or the next granted request."""

    def __init__(self, kind: Kind, junction: str, run: Run) -> None:
        self.kind, self.junction, self.run = kind, junction, run
        self.mode, self.since, self.t = "plan", 0.0, None
        self.shown: dict = {}  # arm -> (the state it showed, since when)
        self.serving = None  # the request being cleared: its key
        self.cleared: list = []  # (arm, lane, green from, green until) for the report
        self.holds = None  # this step's view's holds (Simulation.lane_holds), None: no arbitration

    def update(self, context: dict, agent, system: str, model) -> None:
        """Once a step: check-ins and check-outs, the agent's answers, and the controller's next mode."""
        t = context["time_s"]
        if self.t == t:
            return
        self.t, self.holds = t, context.get("holds")
        self._detect(context, agent, system, model)
        row = self.run.requests.get(self.serving)
        if self.mode == "plan":
            self._next(t, "to_red")
        elif self.mode == "to_red":
            if all(state == "R" and t - since >= context["all_red_s"] - 1e-9 for state, since in self.shown.values()):
                if row["checked_out_s"] is None and not self._yields(t):
                    self._go("clear", t)
                    row["green_s"] = t
                else:  # it went through before its lane was cleared, or a higher priority waits: the next one, or the plan
                    self.serving = None
                    self._go("plan", t)
                    if self._next(t, "clear"):
                        self.run.requests[self.serving]["green_s"] = t
        elif self.mode == "clear" and (row["checked_out_s"] is not None or t - self.since >= CLEAR_MAX_S - 1e-9
                                       or self._yields(t)):
            self.cleared.append((row["approach"], row["lane"], self.since, t))
            self._go("lane_amber", t)
        elif self.mode == "lane_amber" and t - self.since >= context["amber_s"] - 1e-9:
            self._go("all_red", t)
        elif self.mode == "all_red" and t - self.since >= context["all_red_s"] - 1e-9:
            self.serving = None
            self._go("plan", t)
            if self._next(t, "clear"):  # everything is red already: straight on to the next one
                self.run.requests[self.serving]["green_s"] = t
        if self.holds is not None and self.mode != "plan":
            serving = self.run.requests.get(self.serving) or {}  # its arm: route_ahead tells a lane cleared on another
            self.holds[self.junction] = (self.kind.key, self.kind.priority, t, serving.get("approach"))  # renewed

    def _go(self, mode: str, t: float) -> None:
        self.mode, self.since = mode, t
        if mode == "plan" and self.holds is not None and self.holds.get(self.junction, ("",))[0] == self.kind.key:
            del self.holds[self.junction]  # back to the plan: the junction is free

    def _free(self, t: float) -> bool:
        """Whether this controller may leave the plan now: no other one clearing the junction and none of a higher
        priority waiting. Blocked by a lower-priority holder, it says it waits."""
        if self.holds is None:
            return True
        priority, holder = self.kind.priority, self.holds.get(self.junction)
        if holder is not None and holder[0] != self.kind.key and t - holder[2] <= STALE_S:
            want = self.holds.get(("want", self.junction))
            if holder[1] < priority and (want is None or want[0] <= priority or t - want[1] > STALE_S):
                self.holds[("want", self.junction)] = (priority, t)
            return False
        want = self.holds.get(("want", self.junction))
        return want is None or want[0] <= priority or t - want[1] > STALE_S

    def _yields(self, t: float) -> bool:
        """Whether a controller of a higher priority waits for this junction (its want, fresh)."""
        want = self.holds.get(("want", self.junction)) if self.holds is not None else None
        return want is not None and want[0] > self.kind.priority and t - want[1] <= STALE_S

    def _next(self, t: float, mode: str) -> bool:
        """Serve the granted request whose vehicle arrives soonest, if any and the junction is free: go to `mode`."""
        granted = [(row["eta_at_s"], key) for key, row in self.run.requests.items()
                   if row["junction"] == self.junction and row["answer"] and row["answered_s"] is not None
                   and row["answered_s"] <= t + 1e-9 and row["checked_out_s"] is None and row.get("green_s") is None]
        if not granted or not self._free(t):
            return False
        self.serving = min(granted)[1]
        self._go(mode, t)  # update() takes the hold
        return True

    def _detect(self, context: dict, agent, system: str, model) -> None:
        t, live = context["time_s"], set()
        for arm, view in context["arms"].items():
            reach = view["speed_limit_mps"] * DETECT_S
            for i, vehicle in enumerate(view["approaching"]):
                key = f"{self.kind.key}/{self.junction}/{arm}/{vehicle['id']}"
                on = self.run.requests.get(key)
                if not self.kind.is_target(vehicle) or not (
                        vehicle["distance_m"] <= reach or in_queue(vehicle, view["approaching"][:i])
                        or (on is not None and on["checked_out_s"] is None)):
                    continue
                live.add(key)
                row = self.run.requests.get(key)
                message = telemetry(self.kind, self.junction, arm, vehicle, context, self.shown.get(arm, ("R",))[0])
                if row is None:
                    row = self.run.requests[key] = {
                        "junction": self.junction, "approach": arm, "lane": message["lane"], "vehicle": vehicle["id"],
                        "asked_s": t, "system": system, "answer": None, "answered_s": None, "fell_back": False,
                        "checked_out_s": None, "green_s": None, "eta_at_s": t + message[self.kind.vehicle_key]["eta_s"],
                        "no_agent": agent is None or not getattr(agent, "asks", True), "telemetry": message}
                    self.run.feed.append(message)
                if row["answered_s"] is None:
                    self._ask(key, row, message, agent, model, t)
        for key, row in self.run.requests.items():
            if row["junction"] == self.junction and key not in live and row["checked_out_s"] is None:
                row["checked_out_s"] = t  # it has left the arm (or turned off it)
                if row["answered_s"] is None and agent is not None and agent.withdraw(key):
                    row["withdrawn"] = True

    def _ask(self, key: str, row: dict, message: dict, agent, model, t: float) -> None:
        """The request's answer, once it has reached the controller: TSP's rules (signal_agent.py)."""
        if row["no_agent"]:
            row.update(answer=getattr(agent, "rule", None) is not False, answered_s=t)  # the rule: every lane, or none
        else:
            wait, deadline = getattr(agent, "wait_s", None), getattr(agent, "deadline_s", None)
            agent.decide(key, question_for(self.kind, row["system"], model, message), urgency=row["eta_at_s"], once=True)
            answer = agent.answer_of(key)
            if answer is not None:
                if wait is not None:  # an unpaced run paused for it: it counts its latency later, as at 1x
                    row["fell_back"] = bool(agent.fell_back(key))
                    latency = (agent.details(key) or {}).get("latency_s") or 0.0
                    row["answered_s"] = row["asked_s"] + (wait if row["fell_back"] else latency)
                else:
                    row["answered_s"] = t
                row["answer"] = answer
            elif deadline is not None and t - row["asked_s"] > deadline + 1e-9:  # none in time: the plan
                agent.withdraw(key)
                row["fell_back"], row["answered_s"] = True, row["asked_s"] + deadline
            if row["answered_s"] is not None and deadline is not None and row["answered_s"] - row["asked_s"] > deadline + 1e-9:
                row["fell_back"] = True  # came, but too late to count
        if row["answered_s"] is None:
            return
        if row["fell_back"]:
            row["answer"] = False
        found = (agent.details(key) if agent is not None and not row["no_agent"] else None) or {}
        self.run.feed.append({"schema": self.kind.schema, "type": f"{self.kind.key}_decision", "id": key,
                              "time_s": row["answered_s"], "junction": self.junction, "activate": row["answer"],
                              "fell_back": row["fell_back"],
                              **{name: found[name] for name in ("system", "model", "latency_s", "via", "reason", "guard")
                                 if found.get(name) is not None}, **({"no_agent": True} if row["no_agent"] else {})})

    def state(self, arm: str, given: str, context: dict):
        """What this arm shows this step: the state it was given, or the clearance's (the arm's state, and for the
        vehicle's arm its kerbside lane's own)."""
        t = context["time_s"]
        row = self.run.requests.get(self.serving)
        lane = None
        if self.mode == "plan":
            out = given
        elif self.mode == "to_red":
            now, since = self.shown.get(arm, (given, t))
            if now == "G":
                out = "G" if t - since < context["min_green_s"] - 1e-9 else "Y"
            elif now == "Y":
                out = "Y" if t - since < context["amber_s"] - 1e-9 else "R"
            else:
                out = "R"
        else:
            out = "R"
            if row is not None and arm == row["approach"] and self.mode in ("clear", "lane_amber"):
                lane = {row["lane"]: "G" if self.mode == "clear" else "Y"}
        if self.shown.get(arm, (None,))[0] != out:
            self.shown[arm] = (out, t)
        return (out, lane) if lane else out

    def lane_state(self, arm: str) -> str | None:
        """The kerbside lane's own state on this arm now (clearing for a vehicle), else None."""
        row = self.run.requests.get(self.serving)
        if row is None or row["approach"] != arm or self.mode not in ("clear", "lane_amber"):
            return None
        return "G" if self.mode == "clear" else "Y"


def step(kind: Kind, run: Run, junction: str, arm: str, state: str, context: dict, signal_agent, choice):
    """An extension's signal_override for `run` (its run_for): this arm's state this step, the state it was given or
    while a lane is cleared, the clearance's: "R" for every arm, (the arm's "R", {kerbside lane: "G" or "Y"}) for the
    vehicle's. `choice` is the panel's AgentChoice (None: System 1, no model)."""
    here = run.junctions.get(junction)
    if here is None:
        here = run.junctions[junction] = Junction(kind, junction, run)
    system, model = (choice.system(), choice.model_option()) if choice is not None else ("1", None)
    here.update(context, signal_agent, system, model)
    return here.state(arm, state, context)


def shown(runs: dict, latest: Run | None, app) -> Run | None:
    """The run on screen: the window's live run (app.signal_agent's), or the one stepped last."""
    live = getattr(app, "signal_agent", None)
    return runs.get(live) if live is not None else latest


def live_text(kind: Kind, run: Run | None) -> str:
    """The panel's line: requests, TRUE, fell back, lanes cleared and clearing now."""
    if run is None:
        return "No run yet."
    rows = list(run.requests.values())
    clearing = sum(here.mode != "plan" for here in run.junctions.values())
    return (f"{len(rows)} {kind.noun} requests, {sum(bool(row['answer']) for row in rows)} TRUE, "
            f"{sum(row['fell_back'] for row in rows)} fell back, {sum(len(here.cleared) for here in run.junctions.values())} "
            f"lanes cleared, {clearing} junction{'s' if clearing != 1 else ''} clearing now.")


def report(kind: Kind, run: Run, agent) -> dict:
    """The run's <short> sheet (requests, answers, lanes cleared and for how long, how soon after the request the lane
    went green) and <short> decisions (every request: what was observed, asked and answered), its measures and its
    telemetry feed ("<key> telemetry.jsonl")."""
    rows = list(run.requests.values())
    found = {key: ((agent.details(key) if agent is not None and getattr(agent, "asks", False) else None) or {})
             for key in run.requests}
    latencies = [found[key]["latency_s"] for key in run.requests if found[key].get("latency_s") is not None]
    cleared = [entry for here in run.junctions.values() for entry in here.cleared]
    waits = [row["green_s"] - row["asked_s"] for row in rows if row.get("green_s") is not None]
    summary = [["Figure", "Value"], [f"{kind.noun.capitalize()} requests", len(rows)],
               ["Clearance granted (TRUE)", sum(bool(row["answer"]) for row in rows)],
               ["Refused (FALSE)", sum(row["answer"] is False and not row["fell_back"] for row in rows)],
               ["Fell back to the plan", sum(row["fell_back"] for row in rows)],
               ["Decided by nobody (a baseline's rule)", sum(row["no_agent"] for row in rows)],
               ["Lanes cleared", len(cleared)], ["Lane green, in all (s)", round(sum(end - start for *_, start, end in cleared), 1)],
               ["Request to lane green, mean (s)", round(fmean(waits), 1) if waits else None],
               ["Agent latency, mean (s)", round(fmean(latencies), 2) if latencies else None],
               ["Agent latency, median (s)", round(median(latencies), 2) if latencies else None]]
    decisions = [["Asked", "Junction", "Approach", "Lane", "Vehicle", "System", "Model", "Answer", "Answered", "Fell back",
                  "Lane green", "Checked out", "Latency (s)", "Guard", "Reason", "Error"]]
    for key, row in run.requests.items():
        details = found[key]
        answer = ("baseline: " + ("TRUE" if row["answer"] else "FALSE")) if row["no_agent"] else \
            "no answer" if row["answered_s"] is None else "TRUE" if row["answer"] else "FALSE"
        decisions.append([round(row["asked_s"], 1), row["junction"], row["approach"], row["lane"], row["vehicle"],
                          "none" if row["no_agent"] else f"System {row['system']}", details.get("model"), answer,
                          None if row["answered_s"] is None else round(row["answered_s"], 1), "yes" if row["fell_back"] else "",
                          None if row.get("green_s") is None else round(row["green_s"], 1),
                          None if row["checked_out_s"] is None else round(row["checked_out_s"], 1),
                          details.get("latency_s"), details.get("guard"), details.get("reason"), details.get("error")])
    key = kind.key
    return {"sheets": [(kind.short, summary, f"{kind.name}: requests, answers and the lanes cleared."),
                       (f"{kind.short} decisions", decisions, f"Each {kind.vehicle}'s request at each junction: who "
                                                              "decided, the answer, when its lane went green and what "
                                                              "the model said.")],
            "measures": {f"{key}_requests": len(rows), f"{key}_true": sum(bool(row["answer"]) for row in rows),
                         f"{key}_fell_back": sum(row["fell_back"] for row in rows), f"{key}_cleared": len(cleared),
                         f"{key}_lane_green_s": round(sum(end - start for *_, start, end in cleared), 1),
                         f"{key}_to_green_s": round(fmean(waits), 1) if waits else None,
                         f"{key}_latency_s": round(fmean(latencies), 2) if latencies else None},
            "files": [(f"{key} telemetry.jsonl", "".join(json.dumps(message) + "\n" for message in list(run.feed)))]}


def heads(network, run: Run | None, t: float, size: float, back: int) -> list[tuple[float, float, bool]]:
    """Where each signalised approach's lane signal head goes, `back` heads of `size` behind the junction's own
    (placed as NetworkGraphicsScene places those), and whether the controller has its kerbside lane green now:
    (x, y, lit) each."""
    keep = 1.0 if network.driving_side == "left" else -1.0
    roads_at: dict = {}
    for road in network.all_roads():
        roads_at.setdefault(road.from_node, []).append(road)
        roads_at.setdefault(road.to_node, []).append(road)
    found = []
    for node in network.nodes.values():
        if node.control != "signalised":
            continue
        roads = roads_at.get(node.id, [])
        here = run.junctions.get(node.id) if run is not None and run.last_t is not None and abs(t - run.last_t) <= 5 else None
        junction_half = max(road.width_m for road in roads) * 1.28 / 2
        for road in roads:
            if road.direction == "one-way" and road.to_node != node.id:
                continue  # no traffic arrives on this arm
            points = network.road_points(road)
            next_x, next_y = points[1] if road.from_node == node.id else points[-2]
            length = hypot(next_x - node.x, next_y - node.y) or 1.0
            ux, uy = (next_x - node.x) / length, (next_y - node.y) / length  # away from the junction
            width = road.width_m
            inset = max(stop_line_inset(width, network.road_geometry_length(road)),
                        junction_half + size / 2 + 0.5) + back * (size + 0.3)
            kerb = width / 2 + size / 2 + 0.5
            found.append((node.x + ux * inset - uy * kerb * keep, node.y + uy * inset + ux * kerb * keep,
                          here is not None and here.lane_state(road.id) == "G"))
    return found
