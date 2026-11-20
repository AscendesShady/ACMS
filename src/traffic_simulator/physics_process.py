"""A live run's physics in a process of its own (no Qt).

`ProcessRunner` is runner.Runner for a Simulation that steps in another Python: a thread here shares the one
interpreter lock with the window, so a busy run and a busy map take turns (2,700 vehicles at 10x left the map 4
redraws a second and the GPU idle). The Simulation is pickled across, a Runner steps it there as it would here,
and what the window needs comes back over one connection: each published frame with the pace, each sample of
the run's recorder (which stays here), and, when the run stops, the Simulation itself, as it ended. So the
window ends a run, exports it and reads its results exactly as it does a thread's.

Only a Simulation with nothing of the window in it can go: a signal override is an extension's own code, panel
and agents, so a run with one keeps its physics on a thread (MainWindow._runner_for), and a run whose first
override is ticked while it is away comes home (`come_home`).

The other side is this module run as a script (`python -m traffic_simulator.physics_process`, started by
ProcessRunner): `main` below. It imports no Qt and nothing of the window.
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import threading
import time as clock
from multiprocessing.connection import Client, Listener
from pathlib import Path
from queue import Empty, Queue

from .runner import Runner

START_S = 20.0  # the other process connected and holding the Simulation within this, or the run stays here
_UNANSWERED = object()


class _Away:
    """Stands in for `runner.simulation` while the Simulation is in the other process: the few things the window
    asks of a run under way. Its lane graph is the one the run started with (routes don't change in a run)."""

    signal_override = None  # none can be there (see the module's note); MainWindow._apply_hooks brings the run home

    def __init__(self, runner: "ProcessRunner") -> None:
        self._runner, self.graph, self.dt = runner, runner._home.graph, runner._home.dt

    @property
    def time(self) -> float:
        return self._runner.frame.time

    def set_demand(self, demand: list) -> None:
        self._runner._send(("demand", demand))

    def dispatch(self, *args) -> None:
        problem = self._runner._ask("dispatch", args, wait_s=5.0)
        if isinstance(problem, str):
            raise ValueError(problem)


class ProcessRunner:
    """runner.Runner's interface, the Simulation stepping in a process of its own. Raises (OSError, or whatever
    pickling the Simulation raises) when the run can't go there; the caller then makes a Runner."""

    shares_interpreter = False  # drawing takes nothing from its physics: the map needn't draw less when it is behind

    def __init__(self, simulation, speed: float = 1.0, recorder=None) -> None:
        raw = pickle.dumps(simulation, pickle.HIGHEST_PROTOCOL)
        self.recorder = recorder  # sampled here, with the frames the other side sends for it
        self.frame = simulation.frame()
        self.speed_now, self.error = 0.0, ""
        self._home = simulation  # as the run began: never stepped here
        self._final = None  # the Simulation as the run ended, once it has stopped
        self._pace, self._running, self._behind = (simulation.time, 0.0, speed), False, False
        self._sending = threading.Lock()
        self._answers: dict[int, object] = {}
        self._answered = threading.Condition()
        self._asked, self._stopping, self._gone, self._over = 0, 0, False, False
        self._ready = threading.Event()
        key = os.urandom(32)
        self._listener = Listener(None, authkey=key)  # a named pipe on Windows, a Unix socket elsewhere
        self._conn = None
        source = str(Path(__file__).resolve().parents[1])  # found there too when ACMS isn't installed (an IDE's Run)
        self._process = subprocess.Popen(
            [sys.executable, "-m", "traffic_simulator.physics_process"], stdin=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [source, os.environ.get("PYTHONPATH")]))})
        try:
            self._process.stdin.write(f"{self._listener.address}\n{key.hex()}\n".encode())
            self._process.stdin.close()
            sample_s = getattr(recorder, "sample_s", 0.0)
            threading.Thread(target=self._read, args=(raw, speed, sample_s), name="physics process", daemon=True).start()
            deadline = clock.monotonic() + START_S
            while not self._ready.wait(0.02):
                if self._process.poll() is not None or self._gone or clock.monotonic() > deadline:
                    raise OSError("the physics process didn't start")
        except BaseException:
            self._abandon()
            raise

    # --- what a Runner is asked -----------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def speed(self) -> float:
        return self._pace[2]

    @property
    def simulation(self):
        """The Simulation as the run ended, once stopped; while it runs, a stand-in (_Away). If the other process
        was lost: the Simulation as the run began (its results are what the recorder holds)."""
        if self._final is not None:
            return self._final
        return self._home if self._gone or self._over else self._away

    def target(self) -> float:
        """Where simulated time should be now, by the other side's pace: both read the same clock (time.monotonic
        counts from the machine's start, not the process's)."""
        if not self._running:
            return self.frame.time
        sim0, wall0, speed = self._pace
        return sim0 + (clock.monotonic() - wall0) * speed

    def behind(self) -> bool:
        """As the other side judges it (Runner.behind there): a frame is older here by its journey, not by slowness."""
        return self._running and self._behind

    def play(self) -> None:
        if not self._running and not self.error:
            self._pace, self._running = (self.frame.time, clock.monotonic(), self.speed), True  # until it says so
            self._send(("play",))

    def pause(self) -> None:
        """Pause; returns once the physics there is idle and its last step's frame is here."""
        self._ask("pause", wait_s=1.0)
        self._running = False

    def set_speed(self, speed: float) -> None:
        self._pace = (self.target(), clock.monotonic(), speed)  # until the other side's own pace arrives
        self._send(("speed", speed))

    def stop(self, wait_s: float = 5.0) -> bool:
        """End the physics and bring the Simulation back; whether it is here (False: still coming, ask again)."""
        if self._final is not None or self._gone:
            return True
        if not self._stopping:
            self._stopping = self._send_asking("stop")
        raw = self._wait(self._stopping, wait_s)
        if raw is _UNANSWERED:
            return self._gone
        self._over, self._running = True, False
        if raw is not None:
            self._final = pickle.loads(raw)
        self._abandon()
        return True

    def snapshot(self):
        """The Simulation as it is now (a copy: the run carries on), for what reads more than a frame holds."""
        if self._final is not None:
            return self._final
        raw = self._ask("snapshot", wait_s=10.0)
        return pickle.loads(raw) if isinstance(raw, bytes) else self._home

    def come_home(self) -> Runner:
        """The run carried on by a Runner here, on a thread, from where it stands (an extension is to decide in
        it). If the Simulation can't be brought back, this runner again, its `error` saying so: the run ends."""
        running = self._running
        if not self.stop(30.0) or self._final is None:
            self.error = self.error or "The run's physics couldn't be brought back from its process."
            return self  # type: ignore[return-value]
        runner = Runner(self._final, self.speed, recorder=self.recorder)
        if running:
            runner.play()
        return runner

    # --- the connection -------------------------------------------------------------------------

    @property
    def _away(self) -> _Away:
        return _Away(self)

    def _send(self, message: tuple) -> None:
        with self._sending:
            try:
                if self._conn is not None and not self._gone:
                    self._conn.send(message)
            except (OSError, ValueError):
                self._lost()

    def _send_asking(self, what: str, *args) -> int:
        with self._answered:
            self._asked += 1
            asked = self._asked
        self._send((what, asked, *args))
        return asked

    def _wait(self, asked: int, wait_s: float):
        deadline = clock.monotonic() + wait_s
        with self._answered:
            while asked not in self._answers and not self._gone:
                left = deadline - clock.monotonic()
                if left <= 0:
                    return _UNANSWERED
                self._answered.wait(left)
            return self._answers.pop(asked, _UNANSWERED)

    def _ask(self, what: str, *args, wait_s: float = 1.0):
        return self._wait(self._send_asking(what, *args), wait_s)

    def _lost(self) -> None:
        if not self._over and not self.error:
            self.error = "The run's physics process ended unexpectedly."
        self._running = False
        with self._answered:
            self._gone = True
            self._answered.notify_all()

    def _abandon(self) -> None:
        self._over = True
        for closing in (self._conn, self._listener):
            try:
                if closing is not None:
                    closing.close()
            except OSError:
                pass
        try:
            self._process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def _status(self, status: tuple) -> None:
        self._pace, self._running, self.speed_now, error, self._behind = status
        if error:
            self.error = error

    def _sample(self, frame) -> None:
        recorder = self.recorder
        if recorder is not None:
            try:
                recorder.sample(frame)
            except Exception as exc:  # noqa: BLE001 - results must never stop the traffic (as Runner._record)
                recorder.error = f"Results stopped recording: {exc!r}"
                self.recorder = None

    def _read(self, raw: bytes, speed: float, sample_s: float) -> None:
        """This side's thread: everything the other side sends, in the order it sent it."""
        try:
            self._conn = conn = self._listener.accept()
            conn.send(("load", raw, speed, sample_s))
            del raw
            while True:
                message = conn.recv()
                kind = message[0]
                if kind == "frame":
                    if message[1] is not None:
                        self.frame = message[1]
                    self._status(message[2])
                elif kind == "sample":
                    self._sample(message[1])
                elif kind == "reply":
                    _kind, asked, value, frame, status = message
                    if frame is not None:
                        self.frame = frame
                    self._status(status)
                    with self._answered:
                        self._answers[asked] = value
                        self._answered.notify_all()
                elif kind == "ready":
                    self._ready.set()
        except (EOFError, OSError, ValueError, pickle.UnpicklingError):
            self._lost()


# --- the other side -------------------------------------------------------------------------------


class _Samples:
    """The recorder a Runner samples, there: each sample's frame goes to the recorder here."""

    def __init__(self, out: Queue, sample_s: float) -> None:
        self._out, self.sample_s = out, sample_s

    def sample(self, frame) -> None:
        self._out.put(("sample", frame))


def _send_out(conn, runner: Runner, out: Queue) -> None:
    """The other side's sender: what is queued, in order (samples, replies), and between them the latest
    published frame, at most 60 a second, with the pace, whether it runs, the speed kept and any error."""
    sent, told = None, None
    while True:
        try:
            item = out.get(timeout=Runner.PUBLISH_S)
        except Empty:
            item = ()
        frame = runner.frame
        fresh = frame if frame is not sent else None
        status = (runner._pace, runner.running, runner.speed_now, runner.error, runner.behind())
        try:
            if item is None:
                return
            if item and item[0] == "reply":
                conn.send(("reply", item[1], item[2], fresh, status))  # a reply never overtakes the frame before it
            elif item:
                conn.send(item)
                continue
            elif fresh is not None or status != told:
                conn.send(("frame", fresh, status))
            else:
                continue
        except (OSError, ValueError):
            return  # the window is gone
        sent, told = frame, status


def main() -> None:
    address, key = sys.stdin.readline().strip(), bytes.fromhex(sys.stdin.readline().strip())
    conn = Client(address, authkey=key)
    _load, raw, speed, sample_s = conn.recv()
    simulation = pickle.loads(raw)
    del raw
    out: Queue = Queue()
    runner = Runner(simulation, speed, recorder=_Samples(out, sample_s) if sample_s else None)
    sender = threading.Thread(target=_send_out, args=(conn, runner, out), name="frames out", daemon=True)
    sender.start()
    out.put(("ready",))
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            runner.stop(1.0)  # the window is gone: nothing to hand the run to
            return
        kind = message[0]
        if kind == "play":
            runner.play()
        elif kind == "speed":
            runner.set_speed(message[1])
        elif kind == "demand":
            simulation.set_demand(message[1])
        elif kind == "pause":
            runner.pause()
            out.put(("reply", message[1], None))
        elif kind == "dispatch":
            try:
                simulation.dispatch(*message[2])
                out.put(("reply", message[1], None))
            except Exception as exc:  # noqa: BLE001 - said to the window, as it would have been raised there
                out.put(("reply", message[1], str(exc) or repr(exc)))
        elif kind == "snapshot":
            running = runner.running
            runner.pause()  # between two steps: nothing changes while it is copied
            copy = pickle.dumps(simulation, pickle.HIGHEST_PROTOCOL)
            if running:
                runner.play()
            out.put(("reply", message[1], copy))
        elif kind == "stop":
            stopped = runner.stop(60.0)
            out.put(("reply", message[1], pickle.dumps(simulation, pickle.HIGHEST_PROTOCOL) if stopped else None))
            out.put(None)
            sender.join(30.0)
            return


if __name__ == "__main__":
    main()
