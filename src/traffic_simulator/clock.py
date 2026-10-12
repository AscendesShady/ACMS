"""Simulation time: Run starts it at 00:00:00, Pause holds it, Play carries on, Stop halts it
and goes back to 00:00:00.

It steps once per display frame (about 60 a second, on a precise timer) by the real time
elapsed times `speed`, measured each step, so a late tick never loses time and the clock never
jumps. The main window steps the traffic (simulation.py) to each tick's time, and if the traffic
can't keep up it holds the clock back to where the traffic is: the clock always shows the time
of what is drawn. Extensions animate from it too.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal

STEP_MS = 16  # one step per display frame (60 Hz)


class SimulationClock(QObject):
    ticked = Signal(float)  # simulation seconds, after every step and on Run
    state_changed = Signal(bool)  # running?

    def __init__(self) -> None:
        super().__init__()
        self.time_s = 0.0
        self.speed = 1.0  # simulated seconds per real second
        self.running = False
        self._last = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(STEP_MS)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)  # Windows' default timer is 15 ms coarse
        self._timer.timeout.connect(self._step)

    def run(self) -> None:
        """Start again from 00:00:00."""
        self.time_s = 0.0
        self.ticked.emit(self.time_s)
        self.play()

    def play(self) -> None:
        if not self.running:
            self.running, self._last = True, time.monotonic()
            self._timer.start()
            self.state_changed.emit(True)

    def pause(self) -> None:
        if self.running:
            self.running = False
            self._timer.stop()
            self.state_changed.emit(False)

    def stop(self) -> None:
        """Halt and go back to 00:00:00."""
        self.pause()
        self.time_s = 0.0
        self.ticked.emit(self.time_s)

    def set_step_ms(self, ms: int) -> None:
        """Milliseconds between steps (STEP_MS; Power Saver's twice that): the time each covers follows."""
        self._timer.setInterval(ms)

    def _step(self) -> None:
        now = time.monotonic()
        self.advance((now - self._last) * self.speed)
        self._last = now

    def advance(self, seconds: float) -> None:
        self.time_s += seconds
        self.ticked.emit(self.time_s)

    def hold(self, seconds: float) -> None:
        """Put the clock back to `seconds`, quietly: the traffic couldn't keep up with the speed."""
        self.time_s = seconds


def clock_text(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"
