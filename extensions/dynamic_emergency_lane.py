"""Dynamic Emergency Vehicle Lane (DEVL): at each signalised junction, a controller that clears the kerbside lane of
an approach for an emergency vehicle, gated by one TRUE/FALSE per request from System 1 or System 2, as Transit
Signal Priority is for buses (see src/traffic_simulator/extensions.py for the hooks).

Ticked, signal_override() runs inside every signal, for each arm of each signalised junction, every simulation step,
after TSP's. Each junction (`_Junction`) shows the state it is given (its plan, or TSP's) unless it is clearing a lane:
- detection, like TSP's check-in: an emergency vehicle (dispatched, or a type in EMERGENCY_TYPES) on an arm within
  DETECT_S of the stop line at the arm's speed limit, or held in a queue further out (`in_queue`), checks in, and its request (key "devl/junction/arm/vehicle")
  is asked once, through the run's signal agent, the soonest to arrive first. It checks out once it has left the arm.
  The agent reads the approach's telemetry (`telemetry`, schema SCHEMA, a JSON message kept in the run's feed and
  written beside a batch run's workbook as "devl telemetry.jsonl"): the vehicle (distance, speed, expected arrival,
  its lane), the arm's signal and the traffic ahead of it in the kerbside lane (vehicles, how many queued).
  System 1 reads the facts in words with one typed question (is traffic in the emergency vehicle's lane?); System 2,
  the engineer supervising the lane, calls decide_devl(activate). Nobody to ask (outside the window; a batch's No
  LLM): TRUE, the rule. Past the agent's deadline (a batch's Decide within): FALSE. Who decides is the panel's
  (agent_choice.py): System 1 or System 2 and its model, or Multiple, the models Batch Run tests.
- clearance, on TRUE: every arm ends its green (after its minimum green, as TSP never cuts one shorter), then amber,
  then all-red; then only the kerbside lane of the vehicle's approach (lane 0, whichever side traffic drives on)
  shows green, its lane signal flashing, while every other lane and arm stays red, so the
  queue ahead of the vehicle clears and it goes through. The vehicle keeps to that lane as it comes (Simulation's
  EMERGENCY_LANE_M). Once it has left the arm (or after CLEAR_MAX_S), the lane shows amber, then all-red, and the
  junction goes back to its plan, or straight on to the next granted request. An emergency vehicle obeys red, but
  goes on that flashing green whichever lane of the approach it is in (Simulation._signal_for).
Unticked, the simulation never calls it. report(agent) gives a run's workbook its DEVL sheet and DEVL decisions.
"""

import json
import threading
from math import hypot
from statistics import fmean, median

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QFormLayout, QGraphicsEllipseItem, QGraphicsRectItem, QWidget

from traffic_simulator.agent_choice import AgentChoice
from traffic_simulator.models import stop_line_inset
from traffic_simulator.network_scene import SIGNAL_SIZE
from traffic_simulator.properties_panel import hint_label

NAME = "Dynamic Emergency Vehicle Lane (DEVL)"
ORDER = 3

EMERGENCY_TYPES = ("emergency", "ambulance", "fire engine", "police")  # vehicle types it watches (any case)
SCHEMA = "acms-devl-telemetry/1"
QUEUED_KMH = 5.0  # slower than this, a vehicle ahead counts as queued
DETECT_S = 30.0  # check-in: within this many seconds of the stop line at the arm's speed limit, or held in a queue
CLEAR_MAX_S = 60.0  # the longest one vehicle's lane stays green
SIGNALS = {"G": "green", "Y": "amber", "R": "red"}
KERBSIDE = 0  # the lane DEVL clears: the kerbside lane, whichever side traffic drives on

FLASH_PERIOD_S = 1.0  # one on-and-off cycle; lit for the first half
GREEN = QColor("#4ade80")
UNLIT = QColor("#26332a")

NOBODY = "no agent"  # a run's key when it has no signal agent
KEEP_RUNS = 16  # runs whose state is kept: twice the most a batch has going at once
_runs: dict = {}  # each run's signal agent (or NOBODY) -> its _Run, the latest last
_runs_lock = threading.Lock()  # a batch's runs start side by side
_latest = None  # the run stepped last
app = None  # the window's services (agent_options, signal_agent: the live run's), set by the Extensions tab
_choice = None  # who decides (AgentChoice)
_figures = None  # the panel's live figures line

# System 1: the facts in words, one typed yes/no (an encoder reads words, not numbers: see TSP's agent).
SYSTEM_1_QUESTIONS = {"in_the_lane": "Is traffic in the emergency vehicle's lane?"}
# System 2: a boolean tool to answer with, so the decision is data rather than words to parse.
DECIDE_TOOL = {
    "name": "decide_devl",
    "description": "Your decision for this emergency vehicle's approach: clear the dynamic emergency vehicle lane "
                   "for it, or keep the signal plan.",
    "parameters": {"type": "object", "properties": {
        "activate": {"type": "boolean", "description": "true: clear the lane; false: keep the plan"},
        "reason": {"type": "string", "description": "A few words on why"}}, "required": ["activate"]}}
SYSTEM_2_ROLE = (
    "You are an experienced traffic signal engineer supervising a dynamic emergency vehicle lane at a signalised "
    "junction in a city road network. An emergency vehicle is coming in on one approach. You read the approach's live "
    "telemetry (JSON): the emergency vehicle (its distance to the stop line, speed, expected arrival and lane), the "
    "approach's signal, and the traffic ahead of it in the kerbside lane (vehicles, and how many are queued). "
    "Decide whether to clear the lane. You decide once, for this vehicle's approach.\n"
    "If you do, the junction ends its greens safely (after their minimum green, amber and all-red), then gives only "
    "the approach's kerbside lane a green, its lane signal flashing, while everything else waits at red, so the "
    "queue ahead of the emergency vehicle clears and it goes through; then the junction returns to its plan. If you "
    "don't, the signals keep their plan and the vehicle waits its turn.\n"
    "Weigh the emergency vehicle's need against the delay to everyone else: clearing costs the other approaches a "
    "short stop.\n"
    "Give your decision by calling decide_devl: activate true to clear the lane, false to keep the plan. "
    "If you can't call tools, reply with exactly one word: TRUE or FALSE.")


def is_emergency(vehicle: dict) -> bool:
    """An emergency vehicle in a junction's view: dispatched as one, or of an emergency type."""
    return bool(vehicle.get("emergency")) or str(vehicle.get("type", "")).lower() in EMERGENCY_TYPES


def in_queue(vehicle: dict, ahead: list) -> bool:
    """Whether a vehicle is held in a queue: slower than QUEUED_KMH with a vehicle ahead of it in its lane (`ahead`:
    those nearer the stop line on its arm). With the distance, either checks it in."""
    return vehicle["speed_kmh"] < QUEUED_KMH and any(other["lane"] == vehicle["lane"] for other in ahead)

def telemetry(junction: str, arm: str, vehicle: dict, context: dict, state: str) -> dict:
    """The approach's telemetry for an emergency vehicle on it now (JSON-ready)."""
    view = context["arms"][arm]
    left = KERBSIDE
    ahead = [other for other in view["approaching"] if other["lane"] == left and other["distance_m"] < vehicle["distance_m"]]
    speed = vehicle["speed_kmh"] / 3.6
    return {"schema": SCHEMA, "type": "devl_request", "time_s": round(context["time_s"], 1), "junction": junction,
            "approach": arm, "lane": left,
            "emergency_vehicle": {"id": vehicle["id"], "type": vehicle["type"], "distance_m": vehicle["distance_m"],
                                  "speed_kmh": vehicle["speed_kmh"], "eta_s": round(vehicle["distance_m"] / max(speed, 1.0), 1),
                                  "lane": vehicle["lane"], "in_the_lane": vehicle["lane"] == left},
            "signal": SIGNALS.get(state, "red"),
            "lane_ahead": {"vehicles": len(ahead), "queued": sum(other["speed_kmh"] < QUEUED_KMH for other in ahead)},
            "approach_queued": sum(other["speed_kmh"] < QUEUED_KMH for other in view["approaching"])}


def question_for(system: str, model, message: dict) -> dict:
    """What the signal agent sends, in both forms (System 1's state and typed question, System 2's messages and
    tool); `system` and `choice` (its picked model) say who the panel chose (a batch's agent answers with its own)."""
    lane = message["lane_ahead"]
    traffic = "queued" if lane["queued"] else "moving" if lane["vehicles"] else "none"
    return {"system": system, "choice": model if model and model.get("system") == system else None,
            "telemetry": message, "state": {"traffic_in_lane": traffic, "signal": message["signal"]},
            "questions": {name: {"type": "noul", "instructions": text} for name, text in SYSTEM_1_QUESTIONS.items()},
            "tools": [DECIDE_TOOL], "answer_from": ["decide_devl", "activate"],
            "messages": [{"role": "system", "content": SYSTEM_2_ROLE},
                         {"role": "user", "content": "Telemetry now:\n" + json.dumps(message, indent=1)
                                                     + "\n\nDecide: call decide_devl."}]}


class _Run:
    """One run's junctions, its requests (key -> what was asked and answered) and its telemetry feed."""

    def __init__(self, agent) -> None:
        self.agent, self.junctions, self.requests, self.feed, self.last_t = agent, {}, {}, [], None
        self.generation = getattr(agent, "generation", 0)  # the agent's run (SignalAgent.reset moves it on)


class _Junction:
    """One signalised junction's DEVL controller: "plan" (passing its state on), "to_red" (every green ending
    after its minimum, amber, all-red), "clear" (only the kerbside lane of the vehicle's arm green), "lane_amber",
    "all_red", then the plan again or the next granted request."""

    def __init__(self, junction: str, run: _Run) -> None:
        self.junction, self.run = junction, run
        self.mode, self.since, self.t = "plan", 0.0, None
        self.shown: dict = {}  # arm -> (the state it showed, since when)
        self.serving = None  # the request being cleared: its key
        self.cleared: list = []  # (arm, lane, green from, green until) for the report

    def update(self, context: dict, agent, system: str, model) -> None:
        """Once a step: check-ins and check-outs, the agent's answers, and the controller's next mode."""
        t = context["time_s"]
        if self.t == t:
            return
        self.t = t
        self._detect(context, agent, system, model)
        row = self.run.requests.get(self.serving)
        if self.mode == "plan":
            self._next(t, "to_red")
        elif self.mode == "to_red":
            if all(state == "R" and t - since >= context["all_red_s"] - 1e-9 for state, since in self.shown.values()):
                if row["checked_out_s"] is None:
                    self._go("clear", t)
                    row["green_s"] = t
                else:  # it went through before its lane was cleared: the next one, or the plan
                    self.serving = None
                    self._go("plan", t)
                    if self._next(t, "clear"):
                        self.run.requests[self.serving]["green_s"] = t
        elif self.mode == "clear" and (row["checked_out_s"] is not None or t - self.since >= CLEAR_MAX_S - 1e-9):
            self.cleared.append((row["approach"], row["lane"], self.since, t))
            self._go("lane_amber", t)
        elif self.mode == "lane_amber" and t - self.since >= context["amber_s"] - 1e-9:
            self._go("all_red", t)
        elif self.mode == "all_red" and t - self.since >= context["all_red_s"] - 1e-9:
            self.serving = None
            self._go("plan", t)
            if self._next(t, "clear"):  # everything is red already: straight on to the next one
                self.run.requests[self.serving]["green_s"] = t

    def _go(self, mode: str, t: float) -> None:
        self.mode, self.since = mode, t

    def _next(self, t: float, mode: str) -> bool:
        """Serve the granted request whose vehicle arrives soonest, if any: go to `mode`."""
        granted = [(row["eta_at_s"], key) for key, row in self.run.requests.items()
                   if row["junction"] == self.junction and row["answer"] and row["answered_s"] is not None
                   and row["answered_s"] <= t + 1e-9 and row["checked_out_s"] is None and row.get("green_s") is None]
        if not granted:
            return False
        self.serving = min(granted)[1]
        self._go(mode, t)
        return True

    def _detect(self, context: dict, agent, system: str, model) -> None:
        t, live = context["time_s"], set()
        for arm, view in context["arms"].items():
            reach = view["speed_limit_mps"] * DETECT_S
            for i, vehicle in enumerate(view["approaching"]):
                key = f"devl/{self.junction}/{arm}/{vehicle['id']}"
                on = self.run.requests.get(key)
                if not is_emergency(vehicle) or not (vehicle["distance_m"] <= reach or in_queue(vehicle, view["approaching"][:i])
                                                     or (on is not None and on["checked_out_s"] is None)):
                    continue
                live.add(key)
                row = self.run.requests.get(key)
                message = telemetry(self.junction, arm, vehicle, context, self.shown.get(arm, ("R",))[0])
                if row is None:
                    row = self.run.requests[key] = {
                        "junction": self.junction, "approach": arm, "lane": KERBSIDE, "vehicle": vehicle["id"],
                        "asked_s": t, "system": system, "answer": None, "answered_s": None, "fell_back": False,
                        "checked_out_s": None, "green_s": None, "eta_at_s": t + message["emergency_vehicle"]["eta_s"],
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
            agent.decide(key, question_for(row["system"], model, message), urgency=row["eta_at_s"], once=True)
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
        self.run.feed.append({"schema": SCHEMA, "type": "devl_decision", "id": key, "time_s": row["answered_s"],
                              "junction": self.junction, "activate": row["answer"], "fell_back": row["fell_back"],
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


def _run_for(agent, t: float) -> _Run:
    """The run this agent's (or NOBODY's) junctions belong to; a new one when its time goes back or jumps, or
    its agent was reset (a new run, however soon after the last)."""
    global _latest
    key = agent if agent is not None else NOBODY
    run = _runs.get(key)
    if run is None or run.last_t is None or t < run.last_t or t - run.last_t > 2 \
            or run.generation != getattr(agent, "generation", 0):  # or its agent reset: Stop, then Run at once
        run = _Run(agent)
        with _runs_lock:
            _runs.pop(key, None)
            _runs[key] = run
            while len(_runs) > KEEP_RUNS:
                _runs.pop(next(iter(_runs)))  # the oldest, long finished
    run.last_t, _latest = t, run
    return run


def signal_override(junction: str, arm: str, state: str, context: dict, signal_agent):
    """This arm's state this step: the state it was given (the plan, or TSP's), or while a lane is cleared for an
    emergency vehicle, the clearance's: "R" for every arm, (the arm's "R", {kerbside lane: "G" or "Y"}) for the
    vehicle's. `signal_agent` carries the questions to System 1 or System 2."""
    run = _run_for(signal_agent, context["time_s"])
    here = run.junctions.get(junction)
    if here is None:
        here = run.junctions[junction] = _Junction(junction, run)
    system, model = (_choice.system(), _choice.model_option()) if _choice is not None else ("1", None)
    here.update(context, signal_agent, system, model)
    return here.state(arm, state, context)


def _shown() -> _Run | None:
    """The run on screen: the window's live run (app.signal_agent's), or the one stepped last."""
    live = getattr(app, "signal_agent", None)
    return _runs.get(live) if live is not None else _latest


def live_text(run: _Run | None) -> str:
    """The panel's line: requests, TRUE, fell back, lanes cleared and clearing now."""
    if run is None:
        return "No run yet."
    rows = list(run.requests.values())
    clearing = sum(here.mode != "plan" for here in run.junctions.values())
    return (f"{len(rows)} emergency requests, {sum(bool(row['answer']) for row in rows)} TRUE, "
            f"{sum(row['fell_back'] for row in rows)} fell back, {sum(len(here.cleared) for here in run.junctions.values())} "
            f"lanes cleared, {clearing} junction{'s' if clearing != 1 else ''} clearing now.")


def algorithm(frame) -> None:
    """During a run: the panel's live line."""
    if _figures is not None:
        _figures.setText(live_text(_shown()))


def report(agent) -> dict:
    """The run's DEVL sheet (requests, answers, lanes cleared and for how long, how soon after the request the lane
    went green) and DEVL decisions (every request: what was observed, asked and answered), its measures and its
    telemetry feed ("devl telemetry.jsonl")."""
    run = _runs.get(agent if agent is not None else NOBODY) or _Run(agent)
    rows = list(run.requests.values())
    found = {key: ((agent.details(key) if agent is not None and getattr(agent, "asks", False) else None) or {})
             for key in run.requests}
    latencies = [found[key]["latency_s"] for key in run.requests if found[key].get("latency_s") is not None]
    cleared = [entry for here in run.junctions.values() for entry in here.cleared]
    waits = [row["green_s"] - row["asked_s"] for row in rows if row.get("green_s") is not None]
    summary = [["Figure", "Value"], ["Emergency requests", len(rows)],
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
    return {"sheets": [("DEVL", summary, "Dynamic Emergency Vehicle Lane: requests, answers and the lanes cleared."),
                       ("DEVL decisions", decisions, "Each emergency vehicle's request at each junction: who decided, the "
                                                     "answer, when its lane went green and what the model said.")],
            "measures": {"devl_requests": len(rows), "devl_true": sum(bool(row["answer"]) for row in rows),
                         "devl_fell_back": sum(row["fell_back"] for row in rows), "devl_cleared": len(cleared),
                         "devl_lane_green_s": round(sum(end - start for *_, start, end in cleared), 1),
                         "devl_to_green_s": round(fmean(waits), 1) if waits else None,
                         "devl_latency_s": round(fmean(latencies), 2) if latencies else None},
            "files": [("devl telemetry.jsonl", "".join(json.dumps(message) + "\n" for message in list(run.feed)))]}


def panel():
    """Who decides each emergency vehicle's lane: System 1 or System 2 and its model, or Multiple: the models Batch
    Run tests, each against the baselines (Never, Always preempt), and how (agent_choice.py)."""
    global _choice, _figures
    widget = QWidget()
    form = QFormLayout(widget)
    form.setContentsMargins(0, 0, 0, 0)
    _choice = AgentChoice(form, lambda: app.agent_options() if hasattr(app, "agent_options") else [], "1")
    _figures = hint_label("No run yet.")  # live, during a run: algorithm()
    form.addRow(_figures)
    return widget


def batch_settings() -> dict:
    """What Batch Run tests DEVL with (AgentChoice.batch_settings)."""
    if _choice is None:
        return {"system": "1", "deadline_s": 30, "speed": 1, "at_once": 4, "times": 1}
    return _choice.batch_settings()


def draw(network, t):
    """Each signalised approach's lane signal: a square head behind the junction's own; its lamp flashes green
    while the approach's kerbside lane is cleared for an emergency vehicle, dark otherwise."""
    keep = 1.0 if network.driving_side == "left" else -1.0
    flash_on = t % FLASH_PERIOD_S < FLASH_PERIOD_S / 2
    run = _shown()
    roads_at = {}
    for road in network.all_roads():
        roads_at.setdefault(road.from_node, []).append(road)
        roads_at.setdefault(road.to_node, []).append(road)
    items = []
    for node in network.nodes.values():
        if node.control != "signalised":
            continue
        roads = roads_at.get(node.id, [])
        here = run.junctions.get(node.id) if run is not None and run.last_t is not None and abs(t - run.last_t) <= 5 else None
        # The junction box and the signal heads are placed as NetworkGraphicsScene draws them.
        junction_half = max(road.width_m for road in roads) * 1.28 / 2
        for road in roads:
            one_way = road.direction == "one-way"
            if one_way and road.to_node != node.id:
                continue  # no traffic arrives on this arm
            lit = here is not None and here.lane_state(road.id) == "G"
            points = network.road_points(road)
            next_x, next_y = points[1] if road.from_node == node.id else points[-2]
            length = hypot(next_x - node.x, next_y - node.y) or 1.0
            ux, uy = (next_x - node.x) / length, (next_y - node.y) / length  # away from the junction
            width = road.width_m
            inset = max(stop_line_inset(width, network.road_geometry_length(road)),
                        junction_half + SIGNAL_SIZE / 2 + 0.5) + SIGNAL_SIZE + 0.3  # one head further back
            kerb = width / 2 + SIGNAL_SIZE / 2 + 0.5
            x = node.x + ux * inset - uy * kerb * keep
            y = node.y + uy * inset + ux * kerb * keep
            head = QGraphicsRectItem(x - SIGNAL_SIZE / 2, y - SIGNAL_SIZE / 2, SIGNAL_SIZE, SIGNAL_SIZE)
            head.setBrush(QColor("#121719"))  # square, so it reads apart from the round junction heads
            head.setPen(QPen(QColor("#eef5ed"), 0.1))
            head.setZValue(6)
            head.setToolTip("Emergency lane cleared" if lit else "Emergency lane")
            lamp = QGraphicsEllipseItem(head.rect().adjusted(0.25, 0.25, -0.25, -0.25))
            lamp.setBrush(GREEN if lit and flash_on else UNLIT)  # cleared: flashing; otherwise dark
            lamp.setPen(QPen(Qt.PenStyle.NoPen))
            lamp.setZValue(7)
            items += [head, lamp]
    return items
