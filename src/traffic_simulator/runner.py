"""Pacing a run (no Qt): Runner, the physics thread that steps a Simulation in time with the wall clock × speed,
publishes its frames, samples the run's recorder, and handles pause and quit. Moved out of simulation.py, which
still imports it (simulation.Runner)."""

from __future__ import annotations

import sys
import threading
import time as clock


class Runner:
    """Runs a Simulation on its own thread, in step with the wall clock × speed.

    The physics never waits for anyone: every step is computed, in order, to keep simulated time
    at run time × speed, whether or not anything is drawn. After each batch of steps it publishes
    `frame`, the run frozen at that moment (every vehicle's position, heading, speed and place; the
    signals), which viewers draw, skipping frames when busy, and an AI can read at any moment
    without disturbing the run. Only if the CPU itself can't keep up at a very high speed does the
    physics fall behind; `speed_now` then says how fast it really runs.
    """

    PUBLISH_S = 1 / 60  # a fresh frame this often at most: one every step at high speed would waste time

    def __init__(self, simulation: Simulation, speed: float = 1.0, recorder=None) -> None:
        self.simulation = simulation
        # Where simulated time stood at a wall-clock moment, and the speed since: (simulated s, wall s, speed), one
        # tuple swapped whole (set_speed on the UI thread), so the physics thread never reads one half changed
        self._pace = (0.0, 0.0, speed)
        # results.RunRecorder: sampled here, on the physics thread, every `sample_s` simulated seconds, so no
        # sample is lost to a busy screen. One that raises is dropped (its `error` says why); the run carries on.
        self.recorder = recorder
        self._next_sample = getattr(recorder, "sample_s", 0.0)
        self.frame = simulation.frame()
        self.speed_now = 0.0
        self._go = threading.Event()
        self._idle = threading.Event()  # set while the physics waits, paused, with its last step published
        self._quit = False
        self.error = ""  # why the physics stopped by itself (it raised): the run is over, paused for good
        self._published, self._unpublished = clock.monotonic(), False
        self._thread = threading.Thread(target=self._loop, name="traffic physics", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._go.is_set()

    @property
    def speed(self) -> float:
        return self._pace[2]

    def target(self) -> float:
        """Where simulated time should be now: run time × speed (held while paused)."""
        if not self._go.is_set():
            return self.frame.time
        sim0, wall0, speed = self._pace
        return sim0 + (clock.monotonic() - wall0) * speed

    def play(self) -> None:
        if not self._go.is_set() and not self.error:
            self._pace = (self.simulation.time, clock.monotonic(), self.speed)  # the physics is idle
            self._go.set()

    def pause(self) -> None:
        """Pause; returns once the physics is idle and its last step is published."""
        self._go.clear()
        if threading.current_thread() is not self._thread:
            self._idle.wait(timeout=1.0)

    def set_speed(self, speed: float) -> None:
        """From now on at `speed`, carrying on from where the run stands (no jump)."""
        self._pace = (self.target(), clock.monotonic(), speed)

    def stop(self, wait_s: float = 5.0) -> bool:
        """End the physics; whether it has (False: a step still going after `wait_s`, which ends the run when
        it returns; ask again before reading the run as finished)."""
        self._quit = True
        self._go.set()
        self._thread.join(timeout=wait_s)
        return not self._thread.is_alive()

    def _record(self) -> None:
        self._next_sample += self.recorder.sample_s
        try:
            self.recorder.sample(self.simulation.frame())
        except Exception as exc:  # noqa: BLE001 - results must never stop the traffic
            self.recorder.error = f"Results stopped recording: {exc!r}"
            self.recorder = None

    def _publish(self) -> None:
        self.frame, self._published, self._unpublished = self.simulation.frame(), clock.monotonic(), False

    def _loop(self) -> None:
        try:
            self._steps()
        except Exception as exc:  # noqa: BLE001 - said, not lost with the thread: the window ends the run
            self.error = f"The physics stopped at {self.simulation.time:.1f} s: {exc!r}"
            sys.excepthook(*sys.exc_info())  # its traceback, on stderr as before
            self._go.clear()  # not running, for good (play is refused)
            self._idle.set()  # pause and stop return at once

    def _steps(self) -> None:
        measured = (clock.monotonic(), self.simulation.time)
        while True:
            if not self._go.is_set():
                self._idle.set()
                self._go.wait()
                self._idle.clear()
                measured = (clock.monotonic(), self.simulation.time)  # a pause isn't slowness
            if self._quit:
                return
            target = self.target()
            while self.simulation.time + self.simulation.dt <= target + 1e-9 and self._go.is_set() and not self._quit:
                self.simulation.step()
                self._unpublished = True
                if self.recorder is not None and self.simulation.time + 1e-9 >= self._next_sample:
                    self._record()
                if clock.monotonic() - self._published >= self.PUBLISH_S:
                    self._publish()
            if self._unpublished and (clock.monotonic() - self._published >= self.PUBLISH_S
                                      or not self._go.is_set()):
                self._publish()  # a paused run shows its last step
            if not self._go.is_set():
                continue  # paused: straight to waiting, no sleep
            now = clock.monotonic()
            if now - measured[0] >= 1.0:
                self.speed_now = (self.simulation.time - measured[1]) / (now - measured[0])
                measured = (now, self.simulation.time)
            due = (self.simulation.time + self.simulation.dt - self.target()) / max(self.speed, 1e-6)
            clock.sleep(min(max(due, 0.001), 0.05))  # until the next step is due (sleeping frees the GIL)

