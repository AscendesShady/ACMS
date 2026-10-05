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
in them). Who decides is `system`, set by the extension's System 1 / System 2 buttons:
- "1": System 1 (AI Mode > System 1: Jev or Laya): the telemetry's facts in words as its state (the bus's
  exit lane clear or full, more people on the bus or the cross streets), each read with one typed yes/no;
  TRUE when both are yes;
- "2": System 2 (the model started in AI Mode > System 2), zero-shot and role-based: told it is the
  engineer supervising TSP at the junction, given the telemetry, it answers by calling the boolean
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
                       "speed_limit_mps", "length_m"}
    crossing  ids of the vehicles crossing the junction
    phases    [{"arms", "green_s"}], in order
"""

from __future__ import annotations

import json

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
# chance). So its state is the facts in words (`facts`) and it reads each with one noul; TRUE when all say
# yes (Laya: 60/60). Whether the bus will meet a red isn't one: asked once, ~300 m out, it refused buses
# held up later (delay worse than the plan); the controller judges that each second. See sources.md.
SYSTEM_1_QUESTIONS = {"exit_clear": "Is the bus's exit lane clear?",
                      "more_on_bus": "Are more people on the bus than on the cross streets?"}
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
    "junction in a city road network. A bus has requested priority on one approach. You read the junction's live "
    "telemetry (JSON): the request (the bus's distance to the stop line, speed, expected arrival and whether it "
    "arrives on red or green as the signals stand, vehicles ahead "
    "of it, passengers on board, the room on the lane it will leave by), the signal phase and timing (spat: the "
    "current phase and interval, when the bus's green starts or ends) and each approach's queue (vehicles and "
    "the people in them). Decide whether to allow TSP for this bus. You decide once, for its whole approach.\n"
    "If you allow it, the controller acts only if and when the bus needs it, checking each second as the bus "
    "comes: it holds the bus's green for up to 15 s if the bus would reach the stop line just after it ends, or "
    "ends the conflicting greens early (never below their minimum green) if the bus would wait at a red; the "
    "cross streets get that green back over the next cycles. If you don't, the signals keep their plan for it.\n"
    "Weigh the bus's passengers against the people waiting on the other approaches: priority costs the cross "
    "streets little while they aren't badly queued. If the bus's exit lane has no room, priority can't help it "
    "through. person_delay estimates the trade in person-seconds from the plan (saved for the bus's passengers, "
    "lost by the people queued across): a guide, not a rule. lookahead, when there, plays the next seconds on copies "
    "of the network both ways and compares the person-seconds of delay.\n"
    "Give your decision by calling decide_tsp: activate true to allow the bus priority, false to keep the plan. "
    "If you can't call tools, reply with exactly one word: TRUE or FALSE.")


def clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


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
                            "bus_approach": road == arm}
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
               "spat": spat, "approaches": approaches}
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
    queued across, the bus arriving on green, or no person-delay estimate."""
    found = json.loads(json.dumps(message))
    request = found["request"]
    if change == "no_passengers":
        request["passengers"] = 0
        found.pop("person_delay", None)
    elif change == "exit_full":
        request["exit_room_m"], request["exit_has_room"] = 0.0, False
    elif change == "no_cross_queues":
        for road in found["approaches"].values():
            if not road["bus_approach"]:
                road["queued_vehicles"] = road["people_queued"] = 0
        found.pop("person_delay", None)
    elif change == "arrives_on_green":
        request["arrives_on"] = "green"
        found.pop("person_delay", None)
    elif change == "no_estimate":
        found.pop("person_delay", None)
    return found


def facts(message: dict) -> dict:
    """System 1's state: the telemetry's facts for priority, in words (`SYSTEM_1_QUESTIONS` reads them)."""
    request = message["request"]
    across = sum(road["people_queued"] for road in message["approaches"].values() if not road["bus_approach"])
    return {"exit_lane": "clear" if request["exit_has_room"] else "full",
            "more_people_on": "bus" if request["passengers"] > across else "cross streets"}


def question_for(chosen: str, message: dict) -> dict:
    """What the signal agent sends, in both forms: System 1's state and typed questions (TRUE when every one
    is yes), and System 2's messages and its tool; `system` says which the extension chose, `choice` its model if
    one is picked for that system (a batch's agent answers with its own)."""
    return {"system": chosen, "choice": model if model and model.get("system") == chosen else None, "telemetry": message, "state": facts(message),
            "questions": {name: {"type": "noul", "instructions": text} for name, text in SYSTEM_1_QUESTIONS.items()},
            "tools": [DECIDE_TOOL], "answer_from": ["decide_tsp", "activate"],
            "messages": [{"role": "system", "content": SYSTEM_2_ROLE},
                         {"role": "user", "content": "Telemetry now:\n" + json.dumps(message, indent=1)
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
        if not getattr(agent, "asks", True):  # nobody to ask (a batch's No LLM): the plan, from the start
            row.update(no_agent=True)
    if row["answered_s"] is None:
        wait, deadline = getattr(agent, "wait_s", None), getattr(agent, "deadline_s", None)
        if isinstance(agent, Replay):  # the recorded decision, from when it reached the controller
            line = agent.decisions.get(key)
            if line is not None:
                row.update(answer=line["activate"], answered_s=line["time_s"], fell_back=line["fell_back"],
                           no_agent=bool(line.get("no_agent")))
        elif row.get("no_agent"):
            row.update(answer=False, answered_s=t)
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
