"""Dynamic Emergency Vehicle Lane (DEVL): at each signalised junction, a controller that clears the kerbside lane of
an approach for an emergency vehicle, gated by one TRUE/FALSE per request from System 1 or System 2, as Transit
Signal Priority is for buses (see src/traffic_simulator/extensions.py for the hooks).

Ticked, signal_override() runs inside every signal, for each arm of each signalised junction, every simulation step,
after TSP's and DBL's. The controller is src/traffic_simulator/dynamic_lane.py's, which the Dynamic Bus Lane shares
(KIND says what this one watches for and how it asks). Each junction (dynamic_lane.Junction) shows the state it is
given (its plan, or TSP's or DBL's) unless it is clearing a lane:
- detection, like TSP's check-in: an emergency vehicle (dispatched, or a type in EMERGENCY_TYPES) on an arm within
  DETECT_S of the stop line at the arm's speed limit, or held in a queue further out (`in_queue`), checks in, and its request (key "devl/junction/arm/vehicle")
  is asked once, through the run's signal agent, the soonest to arrive first. It checks out once it has left the arm.
  The agent reads the approach's telemetry (dynamic_lane.telemetry, schema SCHEMA, a JSON message kept in the run's feed and
  written beside a batch run's workbook as "devl telemetry.jsonl"): the vehicle (distance, speed, expected arrival,
  its lane), the arm's signal and the traffic ahead of it in the kerbside lane (vehicles, how many queued).
  System 1 reads the facts in words with one typed question (is traffic in the emergency vehicle's lane?); System 2,
  the engineer supervising the lane, calls decide_devl(activate). Nobody to ask (outside the window; a batch's No
  LLM): TRUE, the rule. Past the agent's deadline (a batch's Decide within): FALSE. Who decides is the panel's
  (agent_choice.py): System 1 or System 2 and its model, or Multiple, the models Batch Run tests.
- clearance, on TRUE: every arm ends its green (after its minimum green, as TSP never cuts one shorter), then amber,
  then all-red; then only the kerbside lane of the vehicle's approach (the view's `kerb_lane`: lane 0, whichever side
  traffic drives on, or the lane past a kerb turn pocket's) shows green, its lane signal flashing, while every other lane and arm stays red, so the
  queue ahead of the vehicle clears and it goes through. The vehicle keeps to that lane as it comes (Simulation's
  EMERGENCY_LANE_M). Once it has left the arm (or after CLEAR_MAX_S), the lane shows amber, then all-red, and the
  junction goes back to its plan, or straight on to the next granted request. An emergency vehicle obeys red, but
  goes on that flashing green whichever lane of the approach it is in (Simulation._signal_for).
- priority 2 over DBL's 1: one controller clears a junction at a time, and a bus lane being cleared ends early for an
  emergency vehicle granted at the same junction (dynamic_lane's holds).
- dispatch: its panel's Dispatch Emergency Vehicle section (src/traffic_simulator/emergency_dispatch.py, `dispatch`)
  sends emergency vehicles into the live run, and its schedules are the fleet Batch Run's Test DEVL sends in each run.
Unticked, the simulation never calls it. report(agent) gives a run's workbook its DEVL sheet and DEVL decisions.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QFormLayout, QGraphicsEllipseItem, QGraphicsRectItem, QWidget

from traffic_simulator import dynamic_lane as lane
from traffic_simulator import emergency_dispatch as dispatch
from traffic_simulator.agent_choice import NOBODY, AgentChoice, run_for
from traffic_simulator.dynamic_lane import CLEAR_MAX_S, DETECT_S, QUEUED_KMH, SIGNALS, in_queue  # noqa: F401
from traffic_simulator.network_scene import SIGNAL_SIZE
from traffic_simulator.theme import LANE_SIGNAL_LIT, LANE_SIGNAL_UNLIT, SIGNAL_BODY, SIGNAL_RIM
from traffic_simulator.widgets import Collapsible, hint_label

NAME = "Dynamic Emergency Vehicle Lane (DEVL)"
ORDER = 3

EMERGENCY_TYPES = ("emergency", "ambulance", "fire engine", "police")  # vehicle types it watches (any case)
SCHEMA = "acms-devl-telemetry/1"

FLASH_PERIOD_S = 1.0  # one on-and-off cycle; lit for the first half
GREEN = QColor(LANE_SIGNAL_LIT)
UNLIT = QColor(LANE_SIGNAL_UNLIT)

_runs: dict = {}  # each run's signal agent (or NOBODY) -> its dynamic_lane.Run, the latest last (agent_choice.run_for)
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


KIND = lane.Kind(key="devl", short="DEVL", name="Dynamic Emergency Vehicle Lane", schema=SCHEMA,
                 is_target=is_emergency, vehicle_key="emergency_vehicle", vehicle="emergency vehicle", noun="emergency",
                 questions=SYSTEM_1_QUESTIONS, tool=DECIDE_TOOL, role=SYSTEM_2_ROLE, priority=2)


def signal_override(junction: str, arm: str, state: str, context: dict, signal_agent):
    """This arm's state this step: the state it was given (the plan, TSP's or DBL's), or while a lane is cleared for
    an emergency vehicle, the clearance's: "R" for every arm, (the arm's "R", {kerbside lane: "G" or "Y"}) for the
    vehicle's. `signal_agent` carries the questions to System 1 or System 2."""
    global _latest
    run = _latest = run_for(_runs, signal_agent, context["time_s"], lane.Run)
    return lane.step(KIND, run, junction, arm, state, context, signal_agent, _choice)


def _shown() -> lane.Run | None:
    """The run on screen: the window's live run (app.signal_agent's), or the one stepped last."""
    return lane.shown(_runs, _latest, app)


def algorithm(frame) -> None:
    """During a run: the panel's live line."""
    if _figures is not None:
        _figures.setText(lane.live_text(KIND, _shown()))


def report(agent) -> dict:
    """The run's DEVL sheet (requests, answers, lanes cleared and for how long, how soon after the request the lane
    went green) and DEVL decisions (every request: what was observed, asked and answered), its measures and its
    telemetry feed ("devl telemetry.jsonl")."""
    return lane.report(KIND, _runs.get(agent if agent is not None else NOBODY) or lane.Run(agent), agent)


def panel():
    """Who decides each emergency vehicle's lane: System 1 or System 2 and its model, or Multiple: the models Batch
    Run tests (agent_choice.py); then its Dispatch Emergency Vehicle section (emergency_dispatch.py)."""
    global _choice, _figures
    widget = QWidget()
    form = QFormLayout(widget)
    form.setContentsMargins(0, 0, 0, 0)
    _choice = AgentChoice(form, lambda: app.agent_options() if hasattr(app, "agent_options") else [], "1")
    _figures = hint_label("No run yet.")  # live, during a run: algorithm()
    form.addRow(_figures)
    dispatch.services = lambda: app  # this extension's window services, read when a button is pressed
    form.addRow(Collapsible("Dispatch Emergency Vehicle", dispatch.panel(), expanded=True, tooltip="Send emergency vehicles"))
    return widget


def state() -> dict:
    """Its Dispatch Emergency Vehicle schedules and seed, for the project file (ExtensionsPanel.project_state)."""
    return dispatch.state()


def set_state(saved: dict, check_only: bool = False) -> None:
    """A project's schedules and seed back in its Dispatch Emergency Vehicle section (ValueError says why not)."""
    dispatch.set_state(saved, check_only)


def batch_settings() -> dict:
    """What Batch Run tests DEVL with (AgentChoice.batch_settings)."""
    if _choice is None:
        return {"system": "1", "deadline_s": 30}
    return _choice.batch_settings()


def draw(network, t):
    """Each signalised approach's lane signal: a square head behind the junction's own; its lamp flashes green
    while the approach's kerbside lane is cleared for an emergency vehicle, dark otherwise."""
    flash_on = t % FLASH_PERIOD_S < FLASH_PERIOD_S / 2
    items = []
    for x, y, lit in lane.heads(network, _shown(), t, SIGNAL_SIZE, 1):  # one head further back
        head = QGraphicsRectItem(x - SIGNAL_SIZE / 2, y - SIGNAL_SIZE / 2, SIGNAL_SIZE, SIGNAL_SIZE)
        head.setBrush(QColor(SIGNAL_BODY))  # square, so it reads apart from the round junction heads
        head.setPen(QPen(QColor(SIGNAL_RIM), 0.1))
        head.setZValue(6)
        head.setToolTip("Emergency lane cleared" if lit else "Emergency lane")
        lamp = QGraphicsEllipseItem(head.rect().adjusted(0.25, 0.25, -0.25, -0.25))
        lamp.setBrush(GREEN if lit and flash_on else UNLIT)  # cleared: flashing; otherwise dark
        lamp.setPen(QPen(Qt.PenStyle.NoPen))
        lamp.setZValue(7)
        items += [head, lamp]
    return items
