"""An agent's TRUE / FALSE answers, for algorithms that run inside the traffic simulation
(the Transit Signal Priority extension asks it whether to give a bus priority). No Qt.

A run steps every 0.5 s and must never wait, while a model takes from tens of milliseconds
(System 1) to seconds (System 2) to answer, so questions wait in a queue that one worker answers
in the background: decide(key, question, urgency) returns the latest answer for that key straight
away (FALSE until one has arrived). The worker takes the most urgent question first (the lowest
`urgency`, e.g. when the bus will reach its stop line), and a question still waiting is replaced by
a newer one for the same key, so the model always reads the freshest telemetry; `once` keys are
asked at most once; withdraw(key) drops a question nobody needs any more (its bus has gone). With
no model connected every answer is FALSE, so nothing is overridden. `deadline_s` is how long an
answer may take, in simulated seconds, to be used: the caller checks it (TSP's agent).
The assistant's own headless tests, run as fast as the CPU allows, make theirs with `wait_s`:
decide() then waits up to that long for the answer, and a question not answered by then falls
back to FALSE for good (its late answer is kept in the log, unused).
Every request is logged with its answer, latency, error, what was asked and what the model
reported (tokens, P(yes), how it answered), for the run's workbook.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable

INSTRUCTIONS = ("You answer yes/no questions for a traffic signal controller in a traffic simulation. "
                "Reply with exactly one word: TRUE or FALSE.")


def parse(reply: str) -> bool:
    """TRUE only when the reply's first yes/no word is TRUE (or YES), wherever it stands ("call decide_tsp:
    activate true" is TRUE), whatever marks surround it; FALSE, NO or no such word: FALSE."""
    found = re.search(r"\b(true|yes|false|no)\b", reply, re.IGNORECASE)
    return bool(found) and found.group(1).upper() in ("TRUE", "YES")


class SignalAgent:
    def __init__(self, ask: Callable[[object], object] | None = None, wait_s: float | None = None,
                 deadline_s: float | None = None) -> None:
        """`ask(question)` returns the model's reply: its text, or a dict with "answer" (or "reply", the
        text) and anything else worth keeping (model, tokens, P(yes)); it raises (or `ask` is None) when no
        model is connected. `wait_s`: decide() waits this long for an answer (unpaced headless runs); None:
        never. `deadline_s`: how long an answer may take to count, in simulated seconds (None: any)."""
        self._ask, self.wait_s, self.deadline_s = ask, wait_s, deadline_s
        self._lock = threading.Condition()
        self._answers: dict = {}  # key -> latest answer
        self._asked: dict = {}  # key -> the question last sent
        self._pending: dict = {}  # key -> [urgency, question]: waiting for the worker
        self._flight: set = set()  # keys being asked now
        self._once: set = set()  # keys asked at most once
        self._details: dict = {}  # key -> the latest answer's record
        self._fell_back: set = set()  # keys not answered by the deadline: FALSE for good
        self._generation = 0  # reset() moves it on: answers to an earlier run's questions are dropped
        self._worker: threading.Thread | None = None
        self._closed = False
        self.log: list[dict] = []  # every answered request, in the order answered
        self.asked = self.answered = self.timeouts = self.withdrawn = 0
        self.last_error = ""

    def decide(self, key, question, urgency: float = 0.0, once: bool = False) -> bool:
        """The latest answer for `key`. A new question (`once`: only if never asked) goes in the queue,
        replacing one of the key's still waiting. Never waits, unless made with wait_s: then a question not
        answered within it falls back to FALSE for good."""
        with self._lock:
            if key in self._fell_back:
                return False
            if self._ask is not None and not self._closed:
                if once:
                    self._once.add(key)
                if key in self._pending:
                    self._pending[key] = [urgency, question]  # not sent yet: the newer one goes instead
                elif key not in self._flight and (key not in self._asked if key in self._once
                                                  else self._asked.get(key) != question):
                    self._pending[key] = [urgency, question]
                    self._start()
                    self._lock.notify_all()
            if self.wait_s is not None and (key in self._pending or key in self._flight):
                until = time.monotonic() + self.wait_s
                while (key in self._pending or key in self._flight) and time.monotonic() < until:
                    self._lock.wait(until - time.monotonic())
                if key not in self._answers and (key in self._pending or key in self._flight):
                    self._pending.pop(key, None)  # past the deadline: the plan, whatever comes later
                    self._fell_back.add(key)
                    self._answers[key] = False
                    self.timeouts += 1
                    self.last_error = f"No answer within {self.wait_s:g} s: fell back to the plan."
            return self._answers.get(key, False)

    @property
    def asks(self) -> bool:
        """Whether it has anyone to ask (a batch's No LLM agent hasn't)."""
        return self._ask is not None

    def withdraw(self, key) -> bool:
        """Drop `key`'s question if it is still waiting (nobody needs it now); whether one was."""
        with self._lock:
            if self._pending.pop(key, None) is None:
                return False
            self.withdrawn += 1
            return True

    def answer_of(self, key) -> bool | None:
        """The answer for `key`, or None while none has arrived."""
        with self._lock:
            return self._answers.get(key)

    def fell_back(self, key) -> bool:
        """Whether `key` missed the wait and fell back to FALSE."""
        with self._lock:
            return key in self._fell_back

    def details(self, key) -> dict | None:
        """The record of the latest answer for `key`: answer, latency_s, error, the question and what the
        model reported."""
        with self._lock:
            found = self._details.get(key)
            return dict(found) if found else None

    def waiting(self) -> int:
        """Questions in the queue now (not counting the one being asked)."""
        with self._lock:
            return len(self._pending)

    def _start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="signal agent", daemon=True)
            self._worker.start()

    def _work(self) -> None:
        while True:
            with self._lock:
                while not self._pending and not self._closed:
                    self._lock.wait()
                if self._closed:
                    return
                key = min(self._pending, key=lambda k: self._pending[k][0])  # the most urgent first
                _, question = self._pending.pop(key)
                generation = self._generation
                self._flight.add(key)
                self._asked[key] = question
                self.asked += 1
            started = time.monotonic()
            try:
                reply = self._ask(question)
                record = dict(reply) if isinstance(reply, dict) else {"reply": str(reply)}
                record["answer"] = bool(record["answer"]) if "answer" in record else parse(str(record.get("reply", "")))
                error = ""
            except Exception as exc:  # no model, a network error...: FALSE, nothing overridden
                record, error = {"answer": False}, str(exc)
            record.update(latency_s=round(time.monotonic() - started, 3), error=error, question=question)
            with self._lock:
                self._flight.discard(key)
                if generation == self._generation:  # else: asked before a reset, another run's question
                    if key in self._fell_back:  # too late: kept for the record, not used
                        record["fell_back"] = True
                    else:
                        self._answers[key] = record["answer"]
                    self._details[key] = record
                    self.log.append({"key": key, **record})
                    self.answered += 1
                    self.last_error = error
                self._lock.notify_all()

    def close(self) -> None:
        """Let its worker go (a batch run's agent, done); questions still waiting are dropped."""
        with self._lock:
            self._closed = True
            self._pending.clear()
            self._lock.notify_all()

    def reset(self) -> None:
        """Forget every question, answer and the log (a new run)."""
        with self._lock:
            self._generation += 1
            for found in (self._answers, self._asked, self._pending, self._details):
                found.clear()
            self._once.clear()
            self._fell_back.clear()
            self.log.clear()
            self.asked = self.answered = self.timeouts = self.withdrawn = 0


if __name__ == "__main__":  # quick self-check: python -m traffic_simulator.signal_agent
    agent = SignalAgent(lambda question: "TRUE" if "bus" in question else "FALSE")
    assert agent.decide(("1,1", "1W"), "bus approaching?") is False  # asked; no answer yet
    time.sleep(0.2)
    assert agent.decide(("1,1", "1W"), "bus approaching?") is True and agent.asked == 1  # not asked twice
    waiting = SignalAgent(lambda question: {"answer": True, "model": "m"}, wait_s=5)
    assert waiting.decide("k", "?") is True and waiting.details("k")["model"] == "m"  # waited for it
    assert parse(" true.") and parse("YES") and not parse("False") and not parse("")
    print("ok")
