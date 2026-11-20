"""The agent behind Transit Signal Priority: one TRUE/FALSE for each bus request at a signalised junction.

The TSP extension (extensions/transit_signal_priority.py) runs each junction's TSP controller and
reads priority(...) once a second while a bus is checked in there:
    TRUE   the controller may act for the bus (hold its green, or bring it forward)
    FALSE  the plan (the default)
The agent reads only telemetry: each second a request is on, the junction publishes a JSON message
(`telemetry`, schema SCHEMA, kept in `feed`): the request as a bus would send it (its approach, phase,
distance to the stop line, speed, expected arrival, vehicles ahead, passengers on board, the room on the
lane it will leave by: SAE J2735's signal request), the signal phase and timing (SPaT: the current phase
and interval, when the bus's green starts or ends) and each approach's queue (vehicles, and the people
in them, and their queue storage ratio), and the lead bus's road ahead (`route_ahead`: the next signalised
junctions on its route, their queues, signals and lane clearances). Neither system reads the numbers: `facts`
turns them into sentences (road_words), the same for both, and the goal they weigh is passenger throughput (people
moved through the network). Who decides is `system`, set by the extension's System 1 / System 2 buttons:
- "1": System 1 (AI Mode > System 1: Jev or Laya): the sentences as its state, four of them each read with
  one typed yes/no (the bus's exit lane clear, more people on the bus than queued across, the road ahead on
  its route clear, and free of lane clearances); TRUE when all are yes;
- "2": System 2 (the model started in AI Mode > System 2), zero-shot and role-based: told it is the
  engineer supervising TSP at the junction, given the sentences, it answers by calling the boolean
  tool decide_tsp(activate) (a model that can't call tools replies TRUE or FALSE).
TRUE is permission for the bus's whole approach: the controller then holds or brings forward the green
only if and when the bus needs it, second by second; FALSE keeps the plan for it. Each request is asked
once, through the run's signal agent (signal_agent.py): its queue takes the
request whose bus arrives soonest first and sends the freshest telemetry, and drops a request whose
buses have gone before it was asked (`release`). The answer counts from when it reaches the
controller, the run never pausing for it; past the agent's deadline (`deadline_s`, simulated seconds)
the request falls back to the plan. The assistant's unpaced headless tests wait for answers instead
(`wait_s`), each counting its latency later in simulated time, as if the run went at 1x.
Every decision goes in the feed too. Each run keeps its own record (`Run`: requests, feed, open
questions), so a batch's runs can go side by side; `RUN` is the one used when none is named. Every
question carries both System 1's and System 2's form, and whoever answers takes theirs (a batch's
agents differ from run to run; `system` is the extension's choice for a live run). Written decisions
win: decisions["1,1"] = True (here, or from any thread while a run is on). Edits here apply from the
next start (the Extensions tab reads its files then).

`context` is what the simulation shows the junction's signals (Simulation._junction_view):
    time_s, plan, amber_s, all_red_s, min_green_s, box_m
    arms      road -> {"approaching": [{"id", "type", "distance_m", "speed_kmh", "lane"}, nearest first],
                       "speed_limit_mps", "length_m", "queue_share"}
    crossing  ids of the vehicles crossing the junction
    phases    [{"arms", "green_s"}], in order
    route_ahead  vehicle id -> its next signalised junctions (Simulation.route_ahead)
"""

from __future__ import annotations

import json

from . import road_words
from .results import clock
from .transit_signal_priority import eff_min

SYSTEMS = {"1": "System 1", "2": "System 2"}
system = "1"  # who decides: "1" or "2" (the extension's buttons)
model: dict | None = None  # its model (an AI Mode agent_options entry) picked in the extension; None: AI Mode's own
decisions: dict[str, bool] = {}  # junction -> TRUE/FALSE written by hand: wins over the systems
lookahead_s = 0.0  # > 0: each request's telemetry carries a rollout this many seconds ahead (the extension's Look ahead)


class Run:
    """One run's record: its requests (key -> what was asked and answered, see report), its telemetry feed
    (the JSON the agent read, and its decisions, in order) and each junction's requests not yet answered."""

    def __init__(self) -> None:
        self.requests: dict = {}
        self.feed: list[dict] = []
        self.open: dict = {}


RUN = Run()  # the run when none is named (one at a time)
requests, feed = RUN.requests, RUN.feed  # its record, by name
SCHEMA = "acms-tsp-telemetry/1"
QUEUED_KMH = 5.0  # slower than this, a vehicle on an approach counts as queued
ROOM_NEEDED_M = 14.5  # on the bus's exit lane for it to clear the junction: a 12 m bus and its 2.5 m gap
INTERVALS = {"G": "green", "Y": "amber", "AR": "all-red"}
# what the agent reported with an answer, kept with each decision in the feed (a replay's workbook shows it)
AGENT_REPORTED = ("system", "model", "provider", "latency_s", "p_yes", "via", "reason", "guard", "error", "input_tokens",
                  "output_tokens")

# System 1: an encoder reads words well, but neither compares numbers nor weighs factors in one yes/no (on
# Laya, 60 random requests: the full telemetry or "should the bus get priority?" scored no better than
# chance). So its state is the facts in words (`facts`, road_words) and it reads the deciding ones with one noul
# each; TRUE when all say yes (Laya: 60/60 on the first two). Whether the bus will meet a red isn't one: asked once,
# ~300 m out, it refused buses held up later (delay worse than the plan); the controller judges that each second.
# The road ahead is: a queue reaching back at a red, or a lane cleared there, holds the bus whatever this junction
# does, so priority would delay the cross streets and move nobody. See sources.md.
SYSTEM_1_QUESTIONS = {"exit_clear": "Is the bus's exit lane clear?",
                      "more_on_bus": "Are more people on the bus than waiting on the cross streets?",
                      "route_clear": "Is the road ahead on the bus's route clear?",
                      "route_free": "Is the bus's route ahead free of lane clearances?"}
# System 2: a boolean tool to answer with, so the decision is data rather than words to parse.
DECIDE_TOOL = {
    "name": "decide_tsp",
    "description": "Your decision for this bus's priority request: allow transit signal priority for it, or keep "
                   "the signal plan.",
    "parameters": {"type": "object", "properties": {
        "activate": {"type": "boolean", "description": "true: allow TSP for this bus; false: keep the plan"},
        "reason": {"type": "string", "description": "A few words on why"}}, "required": ["activate"]}}
# System 2: zero-shot (no examples), role-based (who it is and what it is responsible for).
SYSTEM_2_ROLE = (
    "You are an experienced traffic signal engineer supervising transit signal priority (TSP) at a signalised "
    "junction in a city road network. Your goal is passenger throughput: to move as many people as possible through "
    "the network, people rather than vehicles. A bus has requested priority on one approach. You read the situation "
    "in sentences: the bus (the people on board, where it is, when it arrives and whether it arrives on red or green "
    "as the signals stand), the queues on its approach and on the cross streets and the people waiting there, whether "
    "its exit lane has room, the road ahead on its route (the next junctions' queues and signals, and any lane being "
    "cleared there) and an estimate of the trade in person-time. Decide whether to allow TSP for this bus. You decide "
    "once, for its whole approach.\n"
    "If you allow it, the controller acts only if and when the bus needs it, checking each second as the bus "
    "comes: it holds the bus's green for up to 15 s if the bus would reach the stop line just after it ends, or "
    "ends the conflicting greens early (never below their minimum green) if the bus would wait at a red; the "
    "cross streets get that green back over the next cycles. If you don't, the signals keep their plan for it.\n"
    "Priority moves more people when the bus carries more than it holds up on the cross streets. It moves nobody when "
    "the bus can't get through: its exit lane full, or the road ahead blocked or held, so it would only wait there "
    "while the cross streets lost their green.\n"
    "Give your decision by calling decide_tsp: activate true to allow the bus priority, false to keep the plan. "
    "If you can't call tools, reply with exactly one word: TRUE or FALSE.")


def key_of(junction: str, request) -> str:
    """A request's id: its junction, phase and when it checked in."""
    return f"{junction}/{request.phase_id}/{request.t_checkin:g}"


def telemetry(junction: str, context: dict, request, controller) -> dict:
    """The junction's telemetry message for a bus request now (JSON-ready)."""
    t, phase, arm = context["time_s"], request.phase_id, request.approach_id
    current = controller.ids[controller.cur]
    state = "green" if controller.interval == "G" and current == phase else \
        "amber" if controller.interval == "Y" and current == phase else "red"
    spat = {"current_phase": current, "interval": INTERVALS.get(controller.interval, controller.interval),
            "bus_phase": phase, "bus_phase_state": state}
    eta = round(request.d / max(request.v, 2.0))
    if state == "green":
        spat["bus_green_ends_in_s"] = round(max(0.0, controller.t_green_onset + controller.green_target - t))
        arrives_on = "red" if eta > spat["bus_green_ends_in_s"] else "green"
    else:
        spat["bus_green_starts_in_s"] = round(max(0.0, controller.planned_onset(phase) - t))
        arrives_on = "red" if eta < spat["bus_green_starts_in_s"] else "green"
    approaches, seen = {}, {}
    for road, info in context.get("arms", {}).items():
        approaching = info["approaching"]
        queued = [vehicle for vehicle in approaching if vehicle["speed_kmh"] < QUEUED_KMH]
        approaches[road] = {"queued_vehicles": len(queued), "approaching_vehicles": len(approaching),
                            "people_queued": round(sum(vehicle.get("people", 1.0) for vehicle in queued)),
                            "queue_share": info.get("queue_share", 0.0), "bus_approach": road == arm}
        seen.update((vehicle["id"], vehicle) for vehicle in approaching)
    buses = sorted((seen[bus] for bus in request.bus_ids if bus in seen), key=lambda vehicle: vehicle["distance_m"])
    exit_lane = buses[0].get("exit") if buses else None  # the lead bus's
    room = context.get("exit_room_m", {}).get(exit_lane) if exit_lane else None
    message = {"schema": SCHEMA, "type": "tsp_request", "id": key_of(junction, request), "time_s": round(t, 1),
               "clock": clock(t), "junction": junction,
               "request": {"approach": arm, "phase": phase, "buses": len(request.bus_ids),
                           "distance_to_stop_line_m": round(request.d), "speed_kmh": round(request.v * 3.6),
                           "eta_s": eta, "arrives_on": arrives_on, "vehicles_ahead": request.n,
                           "passengers": round(sum(bus.get("people", 0.0) for bus in buses)),
                           "exit_room_m": room, "exit_has_room": room is None or room >= ROOM_NEEDED_M},
               "spat": spat, "approaches": approaches,
               # the lead bus's road ahead (Simulation.route_ahead): the next signalised junctions on its route
               "route_ahead": context["route_ahead"](buses[0]["id"]) if buses and callable(context.get("route_ahead")) else []}
    message["person_delay"] = person_delay(message, controller)
    return message


def person_delay(message: dict, controller) -> dict | None:
    """What priority would trade, in person-seconds (HCM 2000 Ch 14: weigh priority by people, not vehicles): the bus's
    passengers × the red they'd avoid, against the people queued on the other approaches × the green taken from them.
    The bus meeting a red because its green ends first: a hold of up to E_max seconds saves the red until its next
    green (the other phases' planned greens and intervals), costing the cross streets the hold. The bus meeting a red
    now: ending the others' greens early (never below their minimum) saves up to the wait, costing them as much. An
    estimate from the plan, not the run to come."""
    if getattr(controller, "cfg", None) is None:
        return None  # no plan to estimate from
    request, spat = message["request"], message["spat"]
    phases, eta = controller.cfg.phases, request["eta_s"]
    mine = next(p for p in phases if p.phase_id == request["phase"])
    others = [p for p in phases if p is not mine]
    saved = cost = 0.0
    why = "it makes its green: nothing to gain"
    if request["arrives_on"] == "red" and not request["exit_has_room"]:
        why = "its exit lane is full: priority can't get it through"
    elif request["arrives_on"] == "red" and spat["bus_phase_state"] == "green":
        needed = eta - spat["bus_green_ends_in_s"]
        if needed <= controller.cfg.E_max:
            next_green = spat["bus_green_ends_in_s"] + mine.yellow + mine.all_red + sum(
                p.planned_green + p.yellow + p.all_red for p in others)
            saved, cost, why = max(next_green - eta, 0.0), max(needed, 0.0), "a hold of its green"
        else:
            why = f"its green would need {needed:.0f} s more: over the {controller.cfg.E_max:g} s a hold may add"
    elif request["arrives_on"] == "red":
        wait = max(spat.get("bus_green_starts_in_s", 0) - eta, 0.0)
        spare = sum(max(p.planned_green - eff_min(p), 0.0) for p in others)
        saved = cost = min(wait, spare)
        why = "ending the other greens early"
    across = sum(road["people_queued"] for road in message["approaches"].values() if not road["bus_approach"])
    gained, lost = request["passengers"] * saved, across * cost
    return {"bus_seconds_saved": round(saved), "cross_seconds_lost": round(cost), "person_s_saved": round(gained),
            "person_s_lost": round(lost), "net_person_s": round(gained - lost),
            "favours": "priority" if gained > lost else "plan", "how": why}


def perturbed(message: dict, change: str) -> dict:
    """The telemetry with one fact changed (decision_sensitivity): no passengers on the bus, its exit lane full, nobody
    queued across, the bus arriving on green, no person-delay estimate, or the road ahead on its route blocked."""
    found = json.loads(json.dumps(message))
    request = found["request"]
    if change == "no_passengers":
        request["passengers"] = 0
        found.pop("person_delay", None)
    elif change == "exit_full":
        request["exit_room_m"], request["exit_has_room"] = 0.0, False
        found.pop("person_delay", None)  # it reckoned with the room there was
    elif change == "no_cross_queues":
        for road in found["approaches"].values():
            if not road["bus_approach"]:
                road["queued_vehicles"] = road["people_queued"] = 0
                road["queue_share"] = 0.0
        found.pop("person_delay", None)
    elif change == "arrives_on_green":
        request["arrives_on"] = "green"
        found.pop("person_delay", None)
    elif change == "no_estimate":
        found.pop("person_delay", None)
    elif change == "route_blocked":  # the next junction on its route: a queue reaching back, at red
        ahead = found.setdefault("route_ahead", [])
        if not ahead:
            ahead.append({"junction": "the next junction", "distance_m": 200, "priority": None})
        ahead[0].update(queue_share=1.0, signal="R")
    return found


def facts(message: dict) -> dict:
    """The telemetry in words (road_words), what System 1 and System 2 both read: the bus, the queues and the people
    waiting, its exit lane, the road ahead on its route, and the person-time estimates. `SYSTEM_1_QUESTIONS` read
    exit_lane, more_people_on, route_clear and route_free; the rest is for System 2."""
    request, approaches = message.get("request", {}), message.get("approaches", {})
    across = sum(road["people_queued"] for road in approaches.values() if not road["bus_approach"])
    passengers = request.get("passengers") or 0
    found = {}
    if "distance_to_stop_line_m" in request:
        found["bus"] = (f"The bus carries {road_words.people(passengers)}, {request['distance_to_stop_line_m']} m from "
                        f"the stop line, arriving in about {request.get('eta_s')} s.")
    if request.get("arrives_on"):
        found["arrives_on"] = f"As the signals stand, it arrives on {request['arrives_on']}."
    own = next((road for road in approaches.values() if road["bus_approach"]), None)
    if own is not None:
        found["own_queue"] = f"Its own approach has {road_words.queue(own.get('queue_share'))}."
    cross = [road for road in approaches.values() if not road["bus_approach"]]
    if cross:
        longest = road_words.queue(max(road.get("queue_share") or 0 for road in cross))
        found["cross_queues"] = f"The cross streets have at most {longest}, with {road_words.people(across)} queued on them in all."
    found["more_people_on"] = ("More people are on the bus than waiting on the cross streets." if passengers > across
                               else "No more people are on the bus than waiting on the cross streets.")
    found["exit_lane"] = ("The bus's exit lane is clear: the road it leaves by has room for it."
                          if request.get("exit_has_room", True) else "The bus's exit lane is full: the road it leaves by "
                                                                      "has no room for it, so priority can't get it through.")
    found.update(road_words.route(message.get("route_ahead") or [], "bus"))
    estimate = message.get("person_delay")
    if estimate:
        found["estimate"] = (f"By the plan's timings priority gains nothing: {estimate['how']}."
                             if not estimate["bus_seconds_saved"] and not estimate["cross_seconds_lost"] else
                             f"By the plan's timings, {estimate['how']} would save the bus's passengers about "
                             f"{estimate['person_s_saved']} person-seconds and cost those queued across about "
                             f"{estimate['person_s_lost']}, so it moves "
                             f"{'more' if estimate['person_s_saved'] > estimate['person_s_lost'] else 'fewer' if estimate['person_s_saved'] < estimate['person_s_lost'] else 'no more'} people.")
    ahead = message.get("lookahead")
    if ahead:
        saved = ahead["person_s_saved_by_priority"]
        found["lookahead"] = (f"Played {ahead['horizon_s']:g} s ahead both ways on copies of the whole network, priority "
                              f"{'saves' if saved > 0 else 'costs'} about {abs(round(saved))} person-seconds of delay in all.")
    return found


def question_for(chosen: str, message: dict) -> dict:
    """What the signal agent sends, in both forms, the same facts in words (`facts`): System 1's state and typed
    questions (TRUE when every one is yes), and System 2's messages and its tool; `system` says which the extension
    chose, `choice` its model if one is picked for that system (a batch's agent answers with its own)."""
    said = facts(message)
    return {"system": chosen, "choice": model if model and model.get("system") == chosen else None, "telemetry": message, "state": said,
            "questions": {name: {"type": "noul", "instructions": text} for name, text in SYSTEM_1_QUESTIONS.items()},
            "tools": [DECIDE_TOOL], "answer_from": ["decide_tsp", "activate"],
            "messages": [{"role": "system", "content": SYSTEM_2_ROLE},
                         {"role": "user", "content": "The situation now:\n" + road_words.text(said)
                                                     + "\n\nDecide: call decide_tsp."}]}


def priority(junction: str, context: dict, request=None, controller=None, agent=None, run: Run | None = None) -> bool:
    """TRUE or FALSE for this junction's bus request now: a written decision, else the chosen system's answer
    for the request once it has reached the controller (FALSE until then, and after the deadline). `run`: the
    run's record (RUN when none is named)."""
    if junction in decisions:
        return decisions[junction]
    if request is None or controller is None or agent is None or system not in SYSTEMS:
        return False
    run = run or RUN
    key, t = key_of(junction, request), context["time_s"]
    message = telemetry(junction, context, request, controller)
    row = run.requests.get(key)
    if row is None and lookahead_s > 0 and callable(context.get("rollout")) and getattr(controller, "cfg", None):
        message["lookahead"] = look_ahead(junction, context, request, controller)
    run.feed.append(message)
    if row is None:
        row = run.requests[key] = {"junction": junction, "phase": request.phase_id, "approach": request.approach_id,
                                   "buses": ", ".join(sorted(request.bus_ids)), "asked_s": t, "system": system,
                                   "answer": None, "answered_s": None, "fell_back": False, "withdrawn": False,
                                   "estimate": message["person_delay"]}
        run.open.setdefault(junction, set()).add(key)
        if not getattr(agent, "asks", True):  # nobody to ask (a batch's baseline): its rule, from the start
            row.update(no_agent=True)
    if row["answered_s"] is None:
        wait, deadline = getattr(agent, "wait_s", None), getattr(agent, "deadline_s", None)
        if isinstance(agent, Replay):  # the recorded decision, from when it reached the controller
            line = agent.decisions.get(key)
            if line is not None:
                row.update(answer=line["activate"], answered_s=line["time_s"], fell_back=line["fell_back"],
                           no_agent=bool(line.get("no_agent")))
        elif row.get("no_agent"):  # nobody to ask: its rule (the plan, or priority for every request)
            row.update(answer=getattr(agent, "rule", None) is True, answered_s=t)
        else:
            agent.decide(key, question_for(row["system"], message), urgency=t + message["request"]["eta_s"], once=True)
            answer = agent.answer_of(key)
            if answer is not None:
                if wait is not None:  # an unpaced run paused for it: it counts its latency later, as at 1x
                    row["fell_back"] = bool(agent.fell_back(key))
                    latency = (agent.details(key) or {}).get("latency_s") or 0.0
                    row["answered_s"] = row["asked_s"] + (wait if row["fell_back"] else latency)
                else:  # it reaches the controller now
                    row["answered_s"] = t
                row["answer"] = answer
            elif deadline is not None and t - row["asked_s"] > deadline + 1e-9:  # none in time: the plan
                agent.withdraw(key)
                row["fell_back"], row["answered_s"] = True, row["asked_s"] + deadline
        if row["answered_s"] is not None:
            if deadline is not None and row["answered_s"] - row["asked_s"] > deadline + 1e-9:
                row["fell_back"] = True  # came, but too late to count
            if row["fell_back"]:
                row["answer"] = False
            run.open.get(junction, set()).discard(key)
            # the exact time (a replay applies it again) and what the agent reported, for a replay's workbook
            found = (agent.details(key) if not isinstance(agent, Replay) else None) or {}
            run.feed.append({"schema": SCHEMA, "type": "tsp_decision", "id": key, "time_s": row["answered_s"],
                             "junction": junction, "activate": row["answer"], "fell_back": row["fell_back"],
                             **{name: found[name] for name in AGENT_REPORTED if found.get(name) is not None},
                             **({"no_agent": True} if row.get("no_agent") else {})})
    if row["answered_s"] is None or t < row["answered_s"] - 1e-9:
        return False  # no answer yet: the plan
    return row["answer"]


def look_ahead(junction: str, context: dict, request, controller) -> dict:
    """The next `lookahead_s` played twice from now (Simulation.rollout): with priority (the bus's phase held green,
    the others red, for up to the controller's E_max, then the plan) and with the plan; person-seconds of delay in
    the whole network and how far its buses get in each. A prediction on copies, approximate (no amber in the
    priority copy, adaptive signals there hold their greens): the run itself is untouched."""
    arms = {a.approach_id for a in controller.cfg.approaches if a.phase_id == request.phase_id}
    until = context["time_s"] + controller.cfg.E_max

    def priority_now(node, road, state, view):
        if node != junction or view["time_s"] >= until:
            return state
        return "G" if road in arms else "R"

    found = context["rollout"]({"priority": priority_now, "plan": None}, lookahead_s, watch=sorted(request.bus_ids))
    saved = found["plan"]["person_delay_s"] - found["priority"]["person_delay_s"]
    return {"horizon_s": lookahead_s, "with_priority": found["priority"], "with_plan": found["plan"],
            "person_s_saved_by_priority": saved, "favours": "priority" if saved > 0 else "plan"}


def release(junction: str, live: set, agent, t: float | None = None, run: Run | None = None) -> None:
    """The junction's requests now on (`live` keys) at `t`: any other still unanswered has gone (its buses
    left), so its question is withdrawn if it hasn't been asked."""
    run = run or RUN
    for key in [key for key in run.open.get(junction, ()) if key not in live]:
        run.open[junction].discard(key)
        if agent is not None and agent.withdraw(key):
            run.requests[key]["withdrawn"], run.requests[key]["withdrawn_s"] = True, t
            run.feed.append({"schema": SCHEMA, "type": "tsp_withdrawn", "id": key, "time_s": t, "junction": junction})


class Replay:
    """A recorded run's agent: no model is asked. Each request gets the decision its telemetry feed recorded
    (`lines`: telemetry.jsonl, read), at the simulated time it reached the controller, so the same network,
    demand and seed run exactly as they did, whatever the model's latency was then; requests withdrawn or
    answered too late are again. What the agent reported (model, latency, P(yes), guard) comes from the feed."""

    asks, wait_s, deadline_s = True, None, None

    def __init__(self, lines: list[dict]) -> None:
        self.decisions = {line["id"]: line for line in lines if line.get("type") == "tsp_decision"}
        self.late = {line["id"]: line for line in lines if line.get("type") == "tsp_late_answer"}
        self.gone = {line["id"] for line in lines if line.get("type") == "tsp_withdrawn"}
        self.asked = self.answered = self.timeouts = self.withdrawn = 0
        self.log, self.last_error = [], ""

    def withdraw(self, key) -> bool:
        return key in self.gone

    def details(self, key) -> dict | None:
        line = self.late.get(key) or self.decisions.get(key)  # an answer past the deadline: as it came
        return None if line is None else {"answer": line["activate"], **{
            name: line.get(name) for name in AGENT_REPORTED}}

    def waiting(self) -> int:
        return 0

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


def reset(run: Run | None = None) -> None:
    """A new run: forget the last one's requests and feed."""
    run = run or RUN
    run.requests.clear()
    run.feed.clear()
    run.open.clear()
