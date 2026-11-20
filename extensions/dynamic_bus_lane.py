"""Dynamic Bus Lane (DBL): at each signalised junction, a controller that clears the kerbside lane of an approach for a
bus (an intermittent bus lane: Viegas and Lu 2004; Eichler and Daganzo 2006), gated by one TRUE/FALSE per request
from System 1 or System 2, as the Dynamic Emergency Vehicle Lane is for emergency vehicles (see
src/traffic_simulator/extensions.py for the hooks).

Ticked, signal_override() runs inside every signal, for each arm of each signalised junction, every simulation step,
after TSP's and before DEVL's. The controller is src/traffic_simulator/dynamic_lane.py's, which DEVL shares (KIND
says what this one watches for and how it asks). Each junction (dynamic_lane.Junction) shows the state it is given
(its plan, or TSP's) unless it is clearing a lane:
- detection, like TSP's check-in: a bus (a type whose vehicle class is in KERB_LANE_FOR, as TSP tells one) on an arm
  within DETECT_S of the stop line at the arm's speed limit, or held in a queue further out (`in_queue`), checks in,
  and its request (key "dbl/junction/arm/vehicle") is asked once, through the run's signal agent, the soonest to
  arrive first. It checks out once it has left the arm. The agent reads the approach's telemetry
  (dynamic_lane.telemetry, schema SCHEMA, a JSON message kept in the run's feed and written beside a batch run's
  workbook as "dbl telemetry.jsonl"): the bus (distance, speed, expected arrival, its lane, its people on board), the
  arm's signal, the traffic ahead of it in the kerbside lane (vehicles, how many queued), the people a clearance holds
  at red (`people_held`) and the road ahead on its route (`route_ahead`: the next signalised junctions, their queues,
  signals and lane clearances). Neither system reads those numbers: `words` turns them into sentences (road_words),
  the same for both, and the goal is passenger throughput. System 1 reads four with one typed question each (traffic
  in the bus's lane, more people on the bus than held, the road ahead clear, free of lane clearances; TRUE when all
  say yes); System 2, the engineer supervising the lane, calls decide_dbl(activate). Nobody to ask (outside the window; a batch's No LLM): TRUE, the rule. Past the agent's
  deadline (a batch's Decide within): FALSE. Who decides is the panel's (agent_choice.py): System 1 or System 2 and
  its model, or Multiple, the models Batch Run tests.
- clearance, on TRUE: every arm ends its green (after its minimum green), then amber, then all-red; then only the
  kerbside lane of the bus's approach (the view's `kerb_lane`) shows green, its lane signal flashing, while every other
  lane and arm stays red, so the queue ahead of the bus clears and it goes through. Ticked, buses keep to that lane
  over the last BUS_LANE_M before each signal where they may turn on from it (KERB_LANE_FOR, read by
  Simulation._lane_reach), and go on its own green. Once the bus has left the arm (or after CLEAR_MAX_S), the lane
  shows amber, then all-red, and the junction goes back to its plan, or straight on to the next granted request.
- priority 1, below DEVL's 2: one controller clears a junction at a time, and a bus lane being cleared ends early for
  an emergency vehicle granted at the same junction (dynamic_lane's holds).
Unticked, the simulation never calls it. report(agent) gives a run's workbook its DBL sheet and DBL decisions.
"""

from statistics import fmean

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QFormLayout, QGraphicsEllipseItem, QGraphicsRectItem, QWidget

from traffic_simulator import dynamic_lane as lane
from traffic_simulator import road_words
from traffic_simulator.agent_choice import NOBODY, AgentChoice, run_for
from traffic_simulator.dynamic_lane import CLEAR_MAX_S, DETECT_S, in_queue  # noqa: F401
from traffic_simulator.network_scene import SIGNAL_SIZE
from traffic_simulator.theme import LANE_SIGNAL_LIT, LANE_SIGNAL_UNLIT, SIGNAL_BODY, SIGNAL_RIM
from traffic_simulator.widgets import hint_label

NAME = "Dynamic Bus Lane (DBL)"
ORDER = 2.5  # after TSP, before DEVL: an emergency vehicle has the last word

KERB_LANE_FOR = ("bus",)  # vehicle classes it clears for, which keep to the kerbside lane before a signal while ticked
SCHEMA = "acms-dbl-telemetry/1"

FLASH_PERIOD_S = 1.0  # one on-and-off cycle; lit for the first half
GREEN = QColor(LANE_SIGNAL_LIT)
UNLIT = QColor(LANE_SIGNAL_UNLIT)

_runs: dict = {}  # each run's signal agent (or NOBODY) -> its dynamic_lane.Run, the latest last (agent_choice.run_for)
_latest = None  # the run stepped last
app = None  # the window's services (agent_options, signal_agent: the live run's), set by the Extensions tab
_choice = None  # who decides (AgentChoice)
_figures = None  # the panel's live figures line

# System 1: the facts in words (`words`), the deciding ones each read with one typed yes/no, TRUE when all say yes
# (an encoder reads words, not numbers: see TSP's agent). The goal is passenger throughput: clearing the lane is worth
# it when traffic is in the bus's way, the bus carries more people than the clearance holds at red, and the road
# ahead lets it on (a queue reaching back at a red, or another lane cleared there, would only hold it again).
SYSTEM_1_QUESTIONS = {"in_the_lane": "Is traffic in the bus's lane?",
                      "more_on_bus": "Are more people on the bus than would be held at red?",
                      "route_clear": "Is the road ahead on the bus's route clear?",
                      "route_free": "Is the bus's route ahead free of lane clearances?"}
# System 2: a boolean tool to answer with, so the decision is data rather than words to parse.
DECIDE_TOOL = {
    "name": "decide_dbl",
    "description": "Your decision for this bus's approach: clear the dynamic bus lane for it, or keep the signal plan.",
    "parameters": {"type": "object", "properties": {
        "activate": {"type": "boolean", "description": "true: clear the lane; false: keep the plan"},
        "reason": {"type": "string", "description": "A few words on why"}}, "required": ["activate"]}}
SYSTEM_2_ROLE = (
    "You are an experienced traffic signal engineer supervising a dynamic bus lane (an intermittent bus lane) at a "
    "signalised junction in a city road network. Your goal is passenger throughput: to move as many people as "
    "possible through the network, people rather than vehicles. A bus is coming in on one approach. You read the "
    "situation in sentences: the bus (the people on board, where it is, when it arrives), the traffic ahead of it in "
    "the kerbside lane, the people a clearance would hold at red, and the road ahead on its route (the next "
    "junctions' queues and signals, and any lane being cleared there). "
    "Decide whether to clear the lane. You decide once, for this bus's approach.\n"
    "If you do, the junction ends its greens safely (after their minimum green, amber and all-red), then gives only "
    "the approach's kerbside lane a green, its lane signal flashing, while everything else waits at red, so the "
    "queue ahead of the bus clears and it goes through; then the junction returns to its plan. If you don't, the "
    "signals keep their plan and the bus waits its turn.\n"
    "Clearing moves more people when traffic is in the bus's way and the bus carries more than the clearance holds at "
    "red. It moves nobody when the road ahead is blocked or held: the bus would only wait there while everyone else "
    "waited here.\n"
    "Give your decision by calling decide_dbl: activate true to clear the lane, false to keep the plan. "
    "If you can't call tools, reply with exactly one word: TRUE or FALSE.")


def is_bus(vehicle: dict) -> bool:
    """A bus in a junction's view: its type's vehicle class, as TSP tells one."""
    return vehicle.get("class", vehicle["type"].lower()) in KERB_LANE_FOR


def words(message: dict) -> dict:
    """The approach's telemetry in words (road_words), what System 1 and System 2 both read: the bus, the traffic in
    its lane, the people a clearance holds at red, and the road ahead on its route. SYSTEM_1_QUESTIONS read lane,
    more_people_on, route_clear and route_free."""
    bus, ahead = message["bus"], message["lane_ahead"]
    on_bus, held = round(bus.get("people") or 0), message.get("people_held", 0)  # compared as told
    return {"bus": (f"The bus carries {road_words.people(on_bus)}, {round(bus['distance_m'])} m from the stop line, "
                    f"arriving in about {bus['eta_s']:g} s; the signal ahead of it is {message['signal']}."),
            "lane": (f"Traffic is queued in the bus's lane ahead of it ({ahead['vehicles']} vehicles)." if ahead["queued"]
                     else f"Traffic is moving in the bus's lane ahead of it ({ahead['vehicles']} vehicles)."
                     if ahead["vehicles"] else "The bus's lane ahead of it is empty."),
            "held": f"Clearing the lane would hold {road_words.people(held)} at red on the other lanes and approaches.",
            "more_people_on": ("More people are on the bus than would be held at red." if on_bus > held
                               else "No more people are on the bus than would be held at red."),
            **road_words.route(message.get("route_ahead") or [], "bus")}


KIND = lane.Kind(key="dbl", short="DBL", name="Dynamic Bus Lane", schema=SCHEMA, is_target=is_bus,
                 vehicle_key="bus", vehicle="bus", noun="bus", questions=SYSTEM_1_QUESTIONS, tool=DECIDE_TOOL,
                 role=SYSTEM_2_ROLE, priority=1, fields=("people",), words=words)


def signal_override(junction: str, arm: str, state: str, context: dict, signal_agent):
    """This arm's state this step: the state it was given (the plan, or TSP's), or while a lane is cleared for a bus,
    the clearance's: "R" for every arm, (the arm's "R", {kerbside lane: "G" or "Y"}) for the bus's. `signal_agent`
    carries the questions to System 1 or System 2."""
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
    """The run's DBL sheet (requests, answers, lanes cleared and for how long, how soon after the request the lane
    went green, check-in to check-out) and DBL decisions (every request: what was observed, asked and answered), its
    measures (dbl_approach_s: the mean seconds from a bus's check-in to its check-out, those that checked out) and its
    telemetry feed ("dbl telemetry.jsonl")."""
    run = _runs.get(agent if agent is not None else NOBODY) or lane.Run(agent)
    found = lane.report(KIND, run, agent)
    spans = [row["checked_out_s"] - row["asked_s"] for row in run.requests.values() if row["checked_out_s"] is not None]
    approach = round(fmean(spans), 1) if spans else None
    found["sheets"][0][1].append(["Check-in to check-out, mean (s)", approach])
    found["measures"]["dbl_approach_s"] = approach
    return found


def panel():
    """Who decides each bus's lane: System 1 or System 2 and its model, or Multiple: the models Batch Run tests, each
    against the default configuration (agent_choice.py)."""
    global _choice, _figures
    widget = QWidget()
    form = QFormLayout(widget)
    form.setContentsMargins(0, 0, 0, 0)
    _choice = AgentChoice(form, lambda: app.agent_options() if hasattr(app, "agent_options") else [], "1")
    _figures = hint_label("No run yet.")  # live, during a run: algorithm()
    form.addRow(_figures)
    return widget


def batch_settings() -> dict:
    """What Batch Run tests DBL with (AgentChoice.batch_settings)."""
    if _choice is None:
        return {"system": "1", "deadline_s": 30}
    return _choice.batch_settings()


def draw(network, t):
    """Each signalised approach's bus lane signal: a square head behind DEVL's (two behind the junction's own); its
    lamp flashes green while the approach's kerbside lane is cleared for a bus, dark otherwise."""
    flash_on = t % FLASH_PERIOD_S < FLASH_PERIOD_S / 2
    items = []
    for x, y, lit in lane.heads(network, _shown(), t, SIGNAL_SIZE, 2):  # one head further back than DEVL's
        head = QGraphicsRectItem(x - SIGNAL_SIZE / 2, y - SIGNAL_SIZE / 2, SIGNAL_SIZE, SIGNAL_SIZE)
        head.setBrush(QColor(SIGNAL_BODY))  # square, so it reads apart from the round junction heads
        head.setPen(QPen(QColor(SIGNAL_RIM), 0.1))
        head.setZValue(6)
        head.setToolTip("Bus lane cleared" if lit else "Bus lane")
        lamp = QGraphicsEllipseItem(head.rect().adjusted(0.25, 0.25, -0.25, -0.25))
        lamp.setBrush(GREEN if lit and flash_on else UNLIT)  # cleared: flashing; otherwise dark
        lamp.setPen(QPen(Qt.PenStyle.NoPen))
        lamp.setZValue(7)
        items += [head, lamp]
    return items
