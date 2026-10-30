"""Transit Signal Priority (TSP): each signalised junction is run by a TSP controller
(src/traffic_simulator/transit_signal_priority.py), gated by one TRUE/FALSE per bus request from
the agent (src/traffic_simulator/agent_transit_signal_priority.py). Nothing agent-side lives here:
its panel says who decides (agent_choice.py): System 1 or System 2 and which of its models, or Multiple, for
Batch Run: the models it tests, each against the baselines (Never, Always preempt), and how (Decide within, Pace,
Runs at once, Times each: batch_settings(), which Batch Run reads). A live run asks the System chosen last.

Ticked, signal_override() runs inside every signal, for each arm of each signalised junction,
every simulation step (0.5 s), whatever the junction's timing method (but manual windows: such a junction shows its
windows, since they needn't run as phases in turn). The junction shows its own
coordinated plan, exactly, unless priority is acting for a bus:
- plan: each whole second a TSPController (built from the plan: its phases and greens, amber, minimum
  green 7 s, and the plan's all-red, lengthened only where amber and all-red can't let a bus that entered as
  the green ended clear the junction, (distance across + 12 m) / 8 m/s) is put in the plan's state (`sync`) and offered the buses; only if it acts (holds
  the bus's green, up to 15 s, or brings it forward) does it take over, from exactly where the plan is.
- a bus on an arm within the check-in distance (the speed limit x the longest wait for its phase's
  green), or held in a queue further out (slower than 5 km/h behind a vehicle in its lane), checks in; it checks out once it has left the junction. While it is checked in, the agent's
  TRUE/FALSE for its request is read once a second: TRUE lets the controller act, within its limits,
  unless the bus's exit lane has no room for it (then BLOCKED: no priority can help it through);
  FALSE: the plan.
- back to the plan: once the bus has passed (no request on), the controller goes back the shorter way
  round: each green it starts ends with the plan's same green if it can, cut to its minimum to catch
  up or lengthened by up to a fifth to wait, and the plan takes over again as soon as both show the
  same phase's green for at least its minimum (`_recover`). With nothing granted the junction never
  leaves its plan, so coordination holds.
Unticked, the simulation never calls it: the signals run their plans untouched.
Each run keeps its own state (`_Run`: its controllers, the agent's record of it, the agent's backlog),
found by the run's signal agent, so a batch's runs can go side by side.
Watching it: draw() rings each junction with a bus request on, in the theme's status colours
(asking the agent, priority granted, refused, fell back to the plan, returning to the plan);
algorithm() keeps the panel's
live figures (requests, questions waiting, answered in time, fell back, TRUE, latency, errors).
report(agent) gives a run's workbook its TSP sheet (what the controllers did, and the agent's
figures: decisions, latency, tokens), every decision (Agent decisions) and each minute's (Agent per
minute, with the agent's backlog), and the run's telemetry feed (the JSON the agent read, and its
decisions) as a .jsonl file beside it.
"""

import importlib
import json
from math import ceil, floor
from statistics import fmean, median

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QFormLayout, QGraphicsEllipseItem, QSpinBox, QWidget

from traffic_simulator.agent_choice import NOBODY, AgentChoice, run_for
from traffic_simulator.widgets import hint_label
from traffic_simulator.stats import percentile
from traffic_simulator.theme import SECONDARY_TEXT, STATUS
from traffic_simulator.transit_signal_priority import ApproachCfg, NodeCfg, PhaseCfg, TSPController, eff_min
from traffic_simulator.signal_timing import SATURATION_PER_LANE_VPH, START_UP_LOST_S

# read afresh on each load (at start), so edits to the agent apply from the next start
tsp_agent = importlib.reload(importlib.import_module("traffic_simulator.agent_transit_signal_priority"))

NAME = "Transit Signal Priority (TSP)"
ORDER = 2

BUS_TYPES = ("bus",)  # vehicle classes that get priority (a type's vehicle_class, whatever it is called)
V_CROSS = 8.0  # m/s, a bus's speed across the junction
V_LO_SHARE = 0.4  # slowest expected bus speed, as a share of the speed limit
L_BUS = 12.0  # m
RECOVER_LONGER = 0.2  # back to the plan: a green may be lengthened by up to this share of its plan green
ACTS = ("HOLD", "TRUNCATE", "SKIP")  # what the controller logs when priority changes the signals

_choice = None  # who decides (agent_choice.AgentChoice), for batch_settings()

_runs: dict = {}  # each run's signal agent (or NOBODY) -> its _Run, the latest last (agent_choice.run_for)
_latest = None  # the run stepped last
_figures = None  # the panel's live figures line
app = None  # the window's services, set by the Extensions tab: its live run's agent is app.signal_agent
# A junction's ring on the map, by its bus request's state: (colour, its name)
HALOS = {"asking": (STATUS["waiting"], "Asking the agent"), "granted": (STATUS["passed"], "Priority granted"),
         "refused": (SECONDARY_TEXT, "Priority refused"), "fell back": (STATUS["failed"], "Fell back to plan"),
         "recovering": (STATUS["running"], "Returning to the plan")}


def plan_at(view: dict, t: float) -> tuple[int, str, float, float]:
    """The plan's interval running at time t: (its phase's index, "G", "Y" or "AR", when it began, the phase's
    green), its phases' greens then amber then all-red from the cycle's start."""
    phases, amber, all_red = view["phases"], view["amber_s"], view["all_red_s"]
    cycle = sum(phase["green_s"] + amber + all_red for phase in phases)
    begun = t - (t - view["cycle_start_s"]) % cycle  # this cycle's start
    for index, phase in enumerate(phases):
        for interval, length in (("G", phase["green_s"]), ("Y", amber), ("AR", all_red)):
            if t < begun + length - 1e-9:
                return index, interval, begun, phase["green_s"]
            begun += length
    return len(phases) - 1, "AR", begun - all_red, phases[-1]["green_s"]


def plan_greens(view: dict, k: int, t: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """The plan's phase-k greens around t: (start, end) of the last to begin at or before t, and of the next."""
    phases, span = view["phases"], view["amber_s"] + view["all_red_s"]
    cycle = sum(phase["green_s"] + span for phase in phases)
    first = view["cycle_start_s"] + sum(phase["green_s"] + span for phase in phases[:k])
    last = first + floor((t - first + 1e-9) / cycle) * cycle
    green = phases[k]["green_s"]
    return (last, last + green), (last + cycle, last + cycle + green)


class _Run:
    """One run's TSP state: its junctions' controllers, its last time, the agent's backlog by minute (the most
    questions waiting), and the agent's own record of it (`tsp_agent.Run`: requests, telemetry feed)."""

    def __init__(self, agent) -> None:
        self.junctions: dict = {}
        self.last_t, self.agent, self.backlog, self.record = None, agent, {}, tsp_agent.Run()
        self.generation = getattr(agent, "generation", 0)  # the agent's run (SignalAgent.reset moves it on)


class _Junction:
    """One junction: its plan unless priority acts (`mode`: "plan", "tsp" while a bus's request is on after
    the controller took over, "recovering" on the way back), its controller fed the buses, a step a second.
    `episodes`: each time the plan was left, [when the controller took over, when the last bus had passed,
    when the plan was back (None: not yet)]."""

    def __init__(self, junction: str, view: dict, agent, run: _Run) -> None:
        self.view, self.checked_in, self.junction, self.agent, self.run = view, set(), junction, agent, run
        self.mode, self.next, self.now, self.shown, self.episodes = "plan", 0, view["time_s"], None, []
        amber, arms, n = view["amber_s"], view["arms"], len(view["phases"])
        # the plan's all-red, lengthened only if amber and all-red together can't let a bus that entered as the green
        # ended clear the junction: a longer one than the plan's costs every change while off the plan
        all_red = max(view["all_red_s"], ceil((view["box_m"] + L_BUS) / V_CROSS - amber))
        phases = [PhaseCfg(phase_id=f"P{i + 1}", planned_green=p["green_s"], min_green=min(view["min_green_s"], p["green_s"]),
                           yellow=amber, all_red=all_red) for i, p in enumerate(view["phases"])]
        self.arms_of = {phase.phase_id: set(p["arms"]) for phase, p in zip(phases, view["phases"])}
        self.x, approaches = {}, []
        for phase in phases:
            switch = phase.yellow + phase.all_red + sum(eff_min(q) + q.yellow + q.all_red for q in phases if q is not phase)
            for arm in self.arms_of[phase.phase_id]:
                v_hi = arms[arm]["speed_limit_mps"]
                self.x[arm] = v_hi * switch + 1.0
                approaches.append(ApproachCfg(approach_id=arm, phase_id=phase.phase_id, x=self.x[arm],
                                              v_lo=V_LO_SHARE * v_hi, v_hi=v_hi))
        worst = max(p.yellow + p.all_red + sum(q.planned_green + q.yellow + q.all_red for q in phases if q is not p)
                    for p in phases)
        cfg = NodeCfg(phases=phases, approaches=approaches, W=view["box_m"], L_bus=L_BUS, v_cross=V_CROSS,
                      h=3600 / SATURATION_PER_LANE_VPH, t_su=START_UP_LOST_S,  # the queue ahead clears as simulated
                      W_max=worst + (n - 1) * NodeCfg.C_max + NodeCfg.E_max)  # the plan's longest red, credit, a hold
        self.controller = TSPController(cfg, gate=self._gate)

    def _gate(self, request, controller) -> bool:
        """The agent's TRUE/FALSE for the bus request; a TRUE refused (BLOCKED) when the lead bus's exit lane has
        no room for it, as no change to the signals could get it through."""
        allowed = bool(tsp_agent.priority(self.junction, self.view, request, controller, self.agent, self.run.record))
        if allowed and not self._room(request):
            controller.note(self.now, "BLOCKED", phase=request.phase_id)
            return False
        return allowed

    def _room(self, request) -> bool:
        buses = sorted((vehicle for info in self.view["arms"].values() for vehicle in info["approaching"]
                        if vehicle["id"] in request.bus_ids), key=lambda vehicle: vehicle["distance_m"])
        lane = buses[0].get("exit") if buses else None
        room = self.view.get("exit_room_m", {}).get(lane) if lane else None
        return room is None or room >= tsp_agent.ROOM_NEEDED_M

    def update(self, view: dict) -> None:
        t = view["time_s"]
        self._follow(view)
        tick = round(t)
        if abs(t - tick) > 1e-6 or tick < self.next:
            return  # a step a second, on the whole second
        self.view, self.now, self.next = view, t, tick + 1
        controller = self.controller
        if self.mode == "plan":
            self._sync(view, t)  # in the plan's state, ready to act for a bus
        self._detect(view, t)
        logged = len(controller.log)
        shown = controller.step(t)
        if self.mode == "plan" and any(event in ACTS for _, event, _ in controller.log[logged:]):
            self.mode = "tsp"  # priority acts: the controller takes over, from exactly where the plan was
            self.episodes.append([t, None, None])
        if self.mode != "plan":
            self.shown = shown
            if controller.active is not None or controller.queue:
                self.mode = "tsp"
            else:  # the bus has passed: back to the plan
                if self.mode == "tsp":
                    self.mode, self.episodes[-1][1] = "recovering", t
                self._recover(view, t)
        # the requests still on: the agent drops questions for any that have gone
        tsp_agent.release(self.junction, {tsp_agent.key_of(self.junction, request) for request in
                                          (controller.active, *controller.queue) if request is not None}, self.agent,
                          view["time_s"], self.run.record)
        if self.agent is not None and hasattr(self.agent, "waiting"):  # the agent's backlog, each second
            minute, backlog = int(view["time_s"] // 60), self.run.backlog
            backlog[minute] = max(backlog.get(minute, 0), self.agent.waiting())

    def _follow(self, view: dict) -> None:
        """The method's greens as they are now, for the phases to come (an adaptive method changes
        them as it goes); the longest red the controller allows grows with them."""
        cfg = self.controller.cfg
        for phase, planned in zip(cfg.phases, view["phases"]):
            phase.planned_green = max(planned["green_s"], eff_min(phase))
        worst = max(p.yellow + p.all_red + sum(q.planned_green + q.yellow + q.all_red for q in cfg.phases if q is not p)
                    for p in cfg.phases)
        cfg.W_max = max(cfg.W_max, worst + (len(cfg.phases) - 1) * cfg.C_max + cfg.E_max)

    def _sync(self, view: dict, t: float) -> None:
        """The controller in the plan's state just before the second at t: the interval then running (a green
        ending at its last whole second, so a hold is decided before the plan would end it), and when each
        other phase's green last ended."""
        index, interval, began, green = plan_at(view, t - 1e-6)
        if interval == "G":
            green = floor(began + green + 1e-6) - began
        red_since = {}
        for k in range(len(view["phases"])):
            if k != index:
                (_, end), _ = plan_greens(view, k, t)
                red_since[f"P{k + 1}"] = end if end <= t else end - sum(
                    phase["green_s"] + view["amber_s"] + view["all_red_s"] for phase in view["phases"])
        self.controller.sync(t, f"P{index + 1}", interval, began, green, red_since)

    def _recover(self, view: dict, t: float) -> None:
        """Back to the plan the shorter way round: at each green's onset the controller ends it with the plan's
        same green if it can (cut to its minimum when behind the plan, lengthened by up to RECOVER_LONGER when
        ahead); the plan takes over as soon as both show the same phase's green for at least its minimum."""
        controller = self.controller
        if controller.interval != "G":
            return
        k = controller.cur
        phase = controller.cfg.phases[k]
        index, interval, began, green = plan_at(view, t)
        if index == k and interval == "G" and t - controller.t_green_onset + began + green - t >= eff_min(phase) - 1e-6:
            self.mode, self.episodes[-1][2] = "plan", t  # the plan again, the junction's green carrying on
            return
        if abs(controller.t_green_onset - t) > 1e-6:
            return  # a green is set at its onset
        (start, end), (_, end_next) = plan_greens(view, k, t)
        if t < end:  # the plan's same green is on: end with it
            target = end - t
        elif t - start <= end_next - green - t:  # behind the plan: the shortest greens, to catch up
            target = 0.0
        else:  # ahead of it: the longest allowed, to let it come round
            target = end_next - t
        controller.set_green(min(target, phase.planned_green * (1 + RECOVER_LONGER)))

    def _detect(self, view: dict, t: float) -> None:
        controller, seen = self.controller, set()
        for arm, info in view["arms"].items():
            for i, vehicle in enumerate(info["approaching"]):
                bus, speed = vehicle["id"], vehicle["speed_kmh"] / 3.6
                ahead = sum(other["lane"] == vehicle["lane"] for other in info["approaching"][:i])
                queued = ahead and vehicle["speed_kmh"] < tsp_agent.QUEUED_KMH  # held in a queue, however far out
                if vehicle.get("class", vehicle["type"].lower()) not in BUS_TYPES or not (
                        vehicle["distance_m"] <= self.x[arm] or queued or bus in self.checked_in):
                    continue
                if bus in self.checked_in:
                    controller.on_update(bus, vehicle["distance_m"], speed, ahead, t)
                else:
                    controller.on_checkin(bus, arm, vehicle["distance_m"], speed, ahead, t)
                    self.checked_in.add(bus)
                seen.add(bus)
        for bus in self.checked_in - seen - set(view["crossing"]):  # left the junction
            controller.on_checkout(bus, t)
            self.checked_in.discard(bus)

    def state(self, arm: str, planned: str) -> str:
        """The arm's signal: the plan's (`planned`), unless the controller has taken over."""
        if self.mode == "plan" or self.shown is None:
            return planned
        phase, interval = self.shown
        return "R" if arm not in self.arms_of[phase] else {"G": "G", "Y": "Y"}.get(interval, "R")


def panel():
    """Who decides each bus's priority: System 1 or System 2 and its model (AI Mode lists them), or Multiple: the
    models Batch Run tests, each against the baseline, and how (agent_choice.py)."""
    global _choice, _figures
    widget = QWidget()
    form = QFormLayout(widget)
    form.setContentsMargins(0, 0, 0, 0)

    def changed(system, model) -> None:
        tsp_agent.system, tsp_agent.model = system, model

    _choice = AgentChoice(form, lambda: app.agent_options() if hasattr(app, "agent_options") else [],
                          tsp_agent.system, changed)
    ahead = QSpinBox()
    ahead.setRange(0, 120)
    ahead.setSingleStep(10)
    ahead.setSuffix(" s")
    ahead.setSpecialValueText("off")
    ahead.setValue(int(tsp_agent.lookahead_s))
    ahead.setToolTip("Rollout horizon")
    ahead.valueChanged.connect(lambda seconds: setattr(tsp_agent, "lookahead_s", float(seconds)))
    form.addRow("Look ahead", ahead)
    _figures = hint_label("No run yet.")  # live, during a run: algorithm()
    form.addRow(_figures)
    return widget


def batch_settings() -> dict:
    """What Batch Run tests TSP with (AgentChoice.batch_settings): the deadline, pace, runs at once, times each and
    the models, or the System whose model the window finds."""
    if _choice is None:
        return {"system": tsp_agent.system, "deadline_s": 30, "speed": 1, "at_once": 4, "times": 1}
    return _choice.batch_settings()


def _shown() -> _Run | None:
    """The run on screen: the window's live run (app.signal_agent's), or the one stepped last."""
    live = getattr(app, "signal_agent", None)
    return _runs.get(live) if live is not None else _latest


def halo(junction: str, t: float, run: _Run | None = None) -> str | None:
    """The state of the junction's bus request now, for its ring: asking, granted, refused, fell back; None
    when it has none on (or the run, the one shown by default, isn't at the clock's time)."""
    run = run or _shown()
    here = run.junctions.get(junction) if run is not None else None
    if here is None or run.last_t is None or abs(t - run.last_t) > 5:
        return None
    request = here.controller.active
    row = run.record.requests.get(tsp_agent.key_of(junction, request)) if request is not None else None
    if row is None:
        return "recovering" if here.mode == "recovering" else None
    if row["answered_s"] is None or t < row["answered_s"] - 1e-9:
        return "asking"
    return "fell back" if row["fell_back"] else "granted" if row["answer"] else "refused"


def draw(network, t):
    """A ring round each junction with a bus request on, its colour the request's state (HALOS)."""
    widths: dict = {}
    for road in network.all_roads():
        for node in (road.from_node, road.to_node):
            widths[node] = max(widths.get(node, 0.0), road.width_m)
    items, run = [], _shown()
    for junction in list(run.junctions) if run is not None else []:
        state = halo(junction, t, run)
        node = network.nodes.get(junction)
        if state is None or node is None:
            continue
        colour, name = HALOS[state]
        radius = widths.get(junction, 7.0) * 1.28 / 2 + 4.0  # just outside the junction box
        ring = QGraphicsEllipseItem(node.x - radius, node.y - radius, 2 * radius, 2 * radius)
        pen = QPen(QColor(colour), 1.2)
        if state == "asking":
            pen.setStyle(Qt.PenStyle.DashLine)  # still waiting: dashed
        ring.setPen(pen)
        fill = QColor(colour)
        fill.setAlpha(45)
        ring.setBrush(fill)
        ring.setZValue(5)
        ring.setToolTip(name)
        items.append(ring)
    return items


def live_text(run: _Run | None) -> str:
    """The panel's line: the run's requests and its agent's work so far."""
    if run is None:
        return "No run yet."
    agent, rows = run.agent, list(run.record.requests.values())
    if not rows:
        return "No bus request yet."
    decided = [row for row in rows if row["answered_s"] is not None]
    fell = sum(row["fell_back"] for row in decided)
    true = sum(row["answer"] is True for row in decided)
    log = list(getattr(agent, "log", []))
    latencies = [entry["latency_s"] for entry in log if entry.get("latency_s") is not None]
    errors = sum(bool(entry.get("error")) for entry in log)
    waiting = agent.waiting() if hasattr(agent, "waiting") else 0
    timing = (f"latency {median(latencies):.2f} s median, {percentile(latencies, 95):.2f} s at the 95th"
              if latencies else "no answer yet")
    off = sum(here.mode != "plan" for here in list(run.junctions.values()))
    return (f"{len(rows)} bus requests · {waiting} waiting · {len(decided) - fell} answered · {fell} fell back · "
            f"{true} TRUE · {timing} · {errors} error{'' if errors == 1 else 's'} · {off} off the plan")


def algorithm(frame) -> None:
    """During a run: the panel's live figures."""
    if _figures is not None:
        text = live_text(_shown())
        if _figures.text() != text:
            _figures.setText(text)


def signal_override(junction: str, arm: str, state: str, context: dict, signal_agent) -> str:
    """The state this arm shows this step: "G", "Y" or "R". `signal_agent` carries the agent's
    questions to System 1 or System 2 (signal_agent.py)."""
    global _latest
    run = _latest = run_for(_runs, signal_agent, context["time_s"], _Run)  # a new one for a new run
    if context["plan"].get("method") == "manual":
        return state  # its windows: they needn't run as phases in turn (plan_at's model), so no priority there
    here = run.junctions.get(junction)
    if here is None:
        here = run.junctions[junction] = _Junction(junction, context, signal_agent, run)
    here.update(context)
    return here.state(arm, state)


# --- The run's report -------------------------------------------------------------------------

def _stats(values: list) -> tuple:
    """Mean, median, 95th percentile and most, rounded; Nones for no values."""
    values = [value for value in values if value is not None]
    if not values:
        return None, None, None, None
    return (round(fmean(values), 3), round(median(values), 3), round(percentile(values, 95), 3),
            round(max(values), 3))


def report(agent) -> dict:
    """The run's TSP sheet (what each controller did; the agent's decisions, latency and tokens) and its
    Agent decisions sheet (every request: what was observed, asked and answered), plus the figures a batch
    compares. `agent` is the run's signal agent (its answers' records, and the key to its state)."""
    run = _runs.get(agent if agent is not None else NOBODY) or _Run(agent)
    checkouts: dict = {}  # (junction, bus) -> when it checked out, in order
    for junction, here in list(run.junctions.items()):
        for t, event, info in list(here.controller.log):
            if event == "CHECKOUT":
                checkouts.setdefault((junction, info["bus"]), []).append(t)
    rows = []
    for key, request in list(run.record.requests.items()):  # a copy: Export reads it while a run goes on
        found = (agent.details(key) if agent is not None else None) or {}
        gone = [next((t for t in checkouts.get((request["junction"], bus), []) if t >= request["asked_s"]), None)
                for bus in request["buses"].split(", ")]
        left_s = None if None in gone or not gone else max(gone)  # its buses had all gone by then
        late = "answer" in found and not request["fell_back"] and (
            request["answered_s"] is None or (left_s is not None and request["answered_s"] > left_s))
        rows.append({**request, "id": key, **{name: found.get(name) for name in (
            "latency_s", "error", "model", "provider", "p_yes", "input_tokens", "output_tokens", "reply", "via", "reason",
            "guard")}, "late": late, "late_answer": found.get("answer"), "system": found.get("system") or request["system"],
            "observation": (found.get("question") or {}).get("telemetry")})
    logs = {junction: list(here.controller.log) for junction, here in list(run.junctions.items())}
    for row in rows:
        row["outcome"] = outcome(row, logs.get(row["junction"], []))
    events = {junction: [event for _, event, _ in log] for junction, log in logs.items()}
    end = run.last_t or 0.0
    episodes = [episode for here in list(run.junctions.values()) for episode in list(here.episodes)]
    off_plan = sum((episode[2] if episode[2] is not None else end) - episode[0] for episode in episodes)
    back = _stats([episode[2] - episode[1] for episode in episodes if episode[1] is not None and episode[2] is not None])
    holds = sum(found.count("HOLD") for found in events.values())
    truncations = sum(found.count("TRUNCATE") for found in events.values())
    answered = [row for row in rows if row["answer"] is not None and not row["fell_back"] and not row["late"]
                and not (row.get("no_agent") and not row["answer"])]  # Never preempt's rows aren't decisions
    activated = sum(row["answer"] is True for row in answered)
    latency = _stats([row["latency_s"] for row in rows])
    delay = _stats([row["answered_s"] - row["asked_s"] for row in answered])
    tokens_in = [row["input_tokens"] for row in rows if row["input_tokens"] is not None]
    tokens_out = [row["output_tokens"] for row in rows if row["output_tokens"] is not None]
    p_yes = [row["p_yes"] for row in rows if row["p_yes"] is not None]
    models = sorted({str(row["model"]) for row in rows if row["model"]})
    systems = sorted({row["system"] for row in rows}) or [tsp_agent.system]
    errors = [row["error"] for row in rows if row["error"]]
    judged = [row for row in answered if row.get("estimate") and row["estimate"]["bus_seconds_saved"] > 0]
    agrees = [(row["answer"] is True) == (row["estimate"]["favours"] == "priority") for row in judged]
    summary = [["Figure", "Value", "Unit", "How"],
               ["Decided by", ", ".join(tsp_agent.SYSTEMS.get(s, s) for s in systems), "", "the system that answered"],
               ["Model", ", ".join(models) or "none answered", "", "as the model reported itself"],
               ["Bus requests", len(rows), "requests", "a bus checking in (or joining one on its phase): one decision each"],
               ["Answered in time", len(answered), "requests", "an answer reaching the controller while its bus was on"],
               ["Answered too late", sum(row["late"] for row in rows), "requests",
                "the answer reached the controller after the bus had gone"],
               ["Fell back at the deadline", sum(row["fell_back"] for row in rows), "requests",
                "no answer within the deadline" + (f" ({agent.deadline_s:g} simulated s)" if getattr(agent, "deadline_s", None)
                                                   else f" ({agent.wait_s:g} s)" if getattr(agent, "wait_s", None) else "")
                + ", so the plan"],
               ["Bus gone before it was asked", sum(row["withdrawn"] for row in rows), "requests",
                "its question was still waiting in the agent's queue: withdrawn"],
               ["Answered by the tool", sum(row["via"] == "tool" for row in rows), "requests",
                "System 2 calling decide_tsp(activate); the rest answered in words or by System 1"],
               ["Priority given (TRUE)", activated, "requests", ""],
               ["Priority refused (FALSE)", len(answered) - activated, "requests", ""],
               ["TRUE share", round(activated / len(answered), 3) if answered else None, "", "of the answered requests"],
               ["Errors", len(errors), "requests", errors[-1][:200] if errors else ""],
               ["Latency, mean", latency[0], "s", "wall-clock time per answer"],
               ["Latency, median", latency[1], "s", ""],
               ["Latency, 95th percentile", latency[2], "s", ""],
               ["Latency, most", latency[3], "s", ""],
               ["Answer came after, mean", delay[0], "simulated s", "from the request to its answer reaching the "
                                                                    "controller (a batch run: its latency, as at 1x)"],
               ["Answer came after, most", delay[3], "simulated s", ""],
               ["Tokens in", sum(tokens_in) if tokens_in else None, "tokens",
                f"mean {round(fmean(tokens_in), 1)} a request" if tokens_in else "as the model reported"],
               ["Tokens out", sum(tokens_out) if tokens_out else None, "tokens",
                f"mean {round(fmean(tokens_out), 1)} a request" if tokens_out else "System 1 generates none"],
               ["P(yes), mean", round(fmean(p_yes), 3) if p_yes else None, "", "System 1's lowest P(yes) of its facts (TRUE at 0.5)"],
               ["Green held for buses", holds, "s", "the controllers' green extensions (HOLD, a second each)"],
               ["Greens cut short for buses", truncations, "times", "the controllers' early greens (TRUNCATE)"],
               ["Agreeing with the person-delay estimate", round(100 * fmean(agrees), 1) if agrees else None, "%",
                f"of {len(agrees)} answered requests where priority could save the bus time: TRUE when the estimate "
                "favoured priority (bus passengers' seconds saved over the cross streets' lost), FALSE when the plan"],
               ["", "", "", ""], ["What came of each request", "Requests", "", "Outcome"],
               *([name, sum(row["outcome"] == name for row in rows), "", what] for name, what in OUTCOMES.items()),
               ["", "", "", ""], ["Off the plan", "", "", ""],
               ["Times priority took over", len(episodes), "times", "the controller leaving the plan to act for a bus"],
               ["Time off the plan", round(off_plan, 1), "s", "from taking over until the plan was back, all junctions"],
               ["Back to the plan after the bus, mean", back[0], "s", "from the last bus passing to the plan taking "
                                                                        "over again (the shorter way round)"],
               ["Back to the plan after the bus, most", back[3], "s", ""],
               ["Not back by the end", sum(episode[2] is None for episode in episodes), "times", "still recovering "
                                                                                                  "when the run ended"],
               ["", "", "", ""], ["Junction", "Requests", "TRUE", "Green held (s)", "Greens cut short"]]
    for junction in sorted(events):
        mine = [row for row in rows if row["junction"] == junction]
        summary.append([junction, len(mine), sum(row["answer"] is True and not row["late"] for row in mine),
                        events[junction].count("HOLD"), events[junction].count("TRUNCATE")])
    decisions = [["Asked at", "Junction", "Approach", "Phase", "Buses", "System", "Model", "Answer", "Answered at",
                  "Latency (s)", "P(yes)", "Via", "Reason", "Tokens in", "Tokens out", "Error", "Reply", "Telemetry read",
                  "Outcome", "Guard", "Estimate favours", "Net person-s (estimate)", "Agrees with the estimate"]]
    for row in rows:
        answer = row["answer"] if row["answer"] is not None else row["late_answer"]
        decisions.append([tsp_agent.clock(row["asked_s"]), row["junction"], row["approach"], row["phase"], row["buses"],
                          tsp_agent.SYSTEMS.get(row["system"], row["system"]), row["model"],
                          "none: the bus had gone" if row["withdrawn"] else None if answer is None else
                          ("TRUE" if answer else "FALSE") + (" (late, unused)" if row["late"] else " (fell back)"
                                                             if row["fell_back"] else ""),
                          None if row["answered_s"] is None else tsp_agent.clock(row["answered_s"]), row["latency_s"],
                          row["p_yes"], row["via"], row["reason"], row["input_tokens"], row["output_tokens"],
                          row["error"] or None, (row["reply"] or "")[:200] or None,
                          json.dumps(row["observation"]) if row["observation"] else None, row["outcome"], row["guard"],
                          (row.get("estimate") or {}).get("favours"), (row.get("estimate") or {}).get("net_person_s"),
                          None if row not in judged else ("yes" if (row["answer"] is True) ==
                                                          (row["estimate"]["favours"] == "priority") else "no")])
    return {"sheets": [("TSP", summary, "Transit signal priority: who decided, the agent's decisions, latency and "
                                        "tokens, and what the controllers did, in all and by junction."),
                       ("Agent per minute", per_minute(rows, run.backlog), "Each simulated minute: bus requests raised, answers "
                                                              "reaching the controllers (TRUE, FALSE, fell back), "
                                                              "requests withdrawn, latency, and the agent's backlog."),
                       ("Agent decisions", decisions, "Every bus request: what the agent observed, its answer and "
                                                      "when it came, latency, P(yes) or reply, tokens and any error.")],
            "measures": {"tsp_requests": len(rows), "tsp_true": activated, "tsp_green_held_s": holds,
                         "tsp_greens_cut": truncations, "agent_latency_s": latency[0], "agent_errors": len(errors),
                         "agent_fell_back": sum(row["fell_back"] for row in rows),
                         "agent_late": sum(row["late"] for row in rows),
                         "tsp_served": sum(row["outcome"] == "served" for row in rows),
                         "tsp_not_needed": sum(row["outcome"] == "not needed" for row in rows),
                         "tsp_denied": sum(row["outcome"] in ("blocked", "not feasible") for row in rows),
                         "agent_rejected": sum(row["outcome"] == "rejected" for row in rows),
                         "tsp_off_plan_s": round(off_plan, 1), "tsp_back_to_plan_s": back[0],
                         "agent_agrees_estimate": round(100 * fmean(agrees), 1) if agrees else None},
            "files": [("telemetry.jsonl", "".join(json.dumps(message) + "\n" for message in list(run.record.feed) + [
                {"schema": tsp_agent.SCHEMA, "type": "tsp_late_answer", "id": row["id"], "junction": row["junction"],
                 "activate": row["late_answer"], **{name: row[name] for name in tsp_agent.AGENT_REPORTED
                                                     if row.get(name) is not None}}
                for row in rows if row["late"] or (row["fell_back"] and row["late_answer"] is not None)]))],
            # last: answers that came too late to count (after their bus had gone, or the deadline)
            "systems": systems}


def per_minute(rows: list[dict], backlog: dict) -> list[list]:
    """The Agent per minute sheet from the report's rows and the backlog sampled each second."""
    def minute(seconds):
        return None if seconds is None else int(seconds // 60)

    last = max([minute(row["asked_s"]) for row in rows] + [minute(row["answered_s"]) or 0 for row in rows]
               + list(backlog) + [0])
    sheet = [["Minute", "Requests raised", "Answers reaching the controller", "TRUE", "FALSE", "Fell back",
              "Withdrawn (bus gone)", "Latency, mean (s)", "Latency, 95th percentile (s)",
              "Answer came after, mean (simulated s)", "Most waiting in the agent's queue"]]
    for at in range(last + 1):
        raised = [row for row in rows if minute(row["asked_s"]) == at]
        reached = [row for row in rows if minute(row["answered_s"]) == at and not row["withdrawn"]]
        fell = [row for row in reached if row["fell_back"]]
        latencies = [row["latency_s"] for row in reached if row["latency_s"] is not None and not row["fell_back"]]
        after = [row["answered_s"] - row["asked_s"] for row in reached if not row["fell_back"]]
        sheet.append([tsp_agent.clock(at * 60), len(raised), len(reached),
                      sum(row["answer"] is True for row in reached), sum(row["answer"] is False and not row["fell_back"]
                                                                          for row in reached), len(fell),
                      sum(minute(row.get("withdrawn_s")) == at for row in rows if row["withdrawn"]),
                      round(fmean(latencies), 3) if latencies else None,
                      round(percentile(latencies, 95), 3) if latencies else None,
                      round(fmean(after), 2) if after else None, backlog.get(at)])
    return sheet


# What came of a bus request: (its name, what it means)
OUTCOMES = {"served": "TRUE, and the controller held the bus's green or brought it forward",
            "not needed": "TRUE, but the plan served the bus by itself: nothing to change",
            "blocked": "TRUE, but the bus's exit lane had no room: refused, nothing could get it through",
            "not feasible": "TRUE, but no extension or early green could serve it in time",
            "refused": "FALSE: the plan",
            "no LLM": "nobody to ask (Never preempt, the baseline): the plan",
            "rejected": "the answer wasn't a decision (the guard): the plan",
            "fell back": "no answer within the deadline, or an error: the plan",
            "too late": "the answer reached the controller after the bus had gone",
            "withdrawn": "the bus had gone before the agent was asked",
            "no answer": "none yet when the run ended"}


def outcome(row: dict, log: list) -> str:
    """What came of a request (OUTCOMES), from the agent's answer and what its junction's controller logged for
    its phase between the request and its release."""
    if row.get("no_agent") and not row["answer"]:
        return "no LLM"  # Never preempt: the plan; Always preempt goes on to what the controllers did
    if row["withdrawn"]:
        return "withdrawn"
    if str(row.get("guard") or "").startswith("rejected"):
        return "rejected"
    if row["fell_back"]:
        return "fell back"
    if row["late"]:
        return "too late"
    if row["answer"] is None:
        return "no answer"
    if not row["answer"]:
        return "refused"
    phase, start = row["phase"], row["asked_s"]
    end = next((t for t, event, info in log if t >= start and info.get("phase") == phase
                and event in ("RELEASE", "RELEASE_END", "TIMEOUT")), float("inf"))
    mine = {event for t, event, info in log if start <= t <= end and info.get("phase") == phase}
    if mine & {"HOLD", "TRUNCATE"}:
        return "served"
    if "BLOCKED" in mine:
        return "blocked"
    if mine & {"END_EXT", "TIMEOUT"}:
        return "not feasible"
    return "not needed"
