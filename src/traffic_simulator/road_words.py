"""The numbers the priority agents read, in words: what Transit Signal Priority's and the Dynamic Bus Lane's System 1
and System 2 are given in place of telemetry numbers. An encoder (System 1) reads words well but neither compares
numbers nor weighs factors (agent_transit_signal_priority's note: full telemetry on Laya scored no better than chance,
facts in words 60/60), and a chat model (System 2) reads a short digest better than raw JSON (GSM-IC: irrelevant
numbers lower accuracy; CoLLMLight: neighbours filtered beat all neighbours). So Python bands the numbers and both
systems read the same sentences. No Qt.

A queue is told by its queue storage ratio (HCM: the queue's length over the storage the lane has; 1 or more is
spillback into the junction behind): none, short, long, or reaching back. The bands are ours, the knob to tune.
"""

QUEUED_KMH = 5.0  # slower than this, a vehicle counts as queued
SHORT_SHARE = 0.25  # a queue under this share of its lane's length is short
LONG_SHARE = 0.75  # at or over this, it reaches back towards the junction behind (spillback near)
SIGNAL_WORDS = {"G": "green", "Y": "amber", "R": "red"}
CLEARING_FOR = {"devl": "an emergency vehicle", "dbl": "a bus"}  # a lane hold's controller -> who it clears for


def queue(share: float | None) -> str:
    """A queue in words, from its storage ratio (`share` of the lane's length; None or 0: none)."""
    if not share or share <= 0:
        return "no queue"
    if share < SHORT_SHARE:
        return "a short queue"
    if share < LONG_SHARE:
        return "a long queue"
    return "a queue reaching back to the junction behind"


def people(count: float | None) -> str:
    """A number of people in words: nobody, about 1 person, about n people."""
    n = round(count or 0)
    return "nobody" if n <= 0 else "about 1 person" if n == 1 else f"about {n} people"


def blocked(entry: dict) -> bool:
    """Whether a junction ahead blocks the way: a queue reaching back on the vehicle's road, its signal not green
    (green, it is clearing)."""
    return (entry.get("queue_share") or 0) >= LONG_SHARE and entry.get("signal") != "G"


def route(ahead: list[dict], noun: str) -> dict[str, str]:
    """The road ahead on a vehicle's route (Simulation.route_ahead: the next signalised junctions, nearest first), as
    sentences: each junction's queue and signal, then whether the road ahead is clear (`route_clear`) and free of lane
    clearances (`route_free`), the two facts its System 1 questions read."""
    found = {}
    for number, entry in enumerate(ahead, 1):
        found[f"ahead_{number}"] = (
            f"{'The next junction' if number == 1 else 'The junction after that'} on the {noun}'s route, "
            f"{entry['junction']} about {round(entry.get('distance_m') or 0)} m on, has {queue(entry.get('queue_share'))} "
            f"on its road, and its signal is {SIGNAL_WORDS.get(entry.get('signal'), 'red')}.")
    stop = next((entry for entry in ahead if blocked(entry)), None)
    found["route_clear"] = f"The road ahead on the {noun}'s route is clear." if stop is None else (
        f"The road ahead on the {noun}'s route is not clear: at {stop['junction']} a queue reaches back and its signal "
        f"is {SIGNAL_WORDS.get(stop.get('signal'), 'red')}, so it is not clearing.")
    held = next((entry for entry in ahead if entry.get("priority")), None)
    found["route_free"] = f"The {noun}'s route ahead is free of lane clearances." if held is None else (
        f"The {noun}'s route ahead is not free of lane clearances: {held['junction']} is clearing a lane for "
        f"{CLEARING_FOR.get(held['priority'], 'another vehicle')}, so the {noun} will be held there.")
    return found


def text(sentences: dict[str, str]) -> str:
    """The sentences as System 2 reads them: one a line."""
    return "\n".join(f"- {sentence}" for sentence in sentences.values())


if __name__ == "__main__":  # quick self-check: python -m traffic_simulator.road_words
    assert queue(0) == "no queue" and queue(0.1) == "a short queue" and queue(0.5) == "a long queue"
    assert queue(0.75).startswith("a queue reaching back") and people(0.4) == "nobody" and people(40) == "about 40 people"
    ahead = [{"junction": "2,1", "distance_m": 180, "queue_share": 0.9, "signal": "G", "priority": None},
             {"junction": "3,1", "distance_m": 360, "queue_share": 0.8, "signal": "R", "priority": "devl"}]
    words = route(ahead, "bus")
    assert "not clear: at 3,1" in words["route_clear"] and "emergency vehicle" in words["route_free"]
    assert route([], "bus")["route_clear"].endswith("is clear.") and text({"a": "One."}) == "- One."
    print("ok")
