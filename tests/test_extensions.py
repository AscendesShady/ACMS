"""Extensions tab: drop-in files draw on the map, and a broken one can't hurt the editor; the
simulation clock (Run / Pause / Play) they animate from, offscreen.
Run with `python tests/test_extensions.py` (or the test monitor)."""

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLabel, QLineEdit, QPushButton, QSlider, QSpinBox  # noqa: E402

from traffic_simulator.clock import SimulationClock, clock_text  # noqa: E402
from traffic_simulator.extensions import EXTENSIONS_DIR, ExtensionsPanel  # noqa: E402
from traffic_simulator.mode_behavior import PALETTE, VehicleMode  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_scene import NetworkGraphicsScene  # noqa: E402
from traffic_simulator.properties_panel import ChoiceButtons  # noqa: E402
from traffic_simulator.simulation import PairDemand, Simulation, Turn  # noqa: E402
from traffic_simulator.mode_behavior import VehicleModes  # noqa: E402

MODES = VehicleModes().modes

APP = QApplication.instance() or QApplication([])

GOOD = '''
from PySide6.QtWidgets import QGraphicsEllipseItem
NAME = "Dot"
def draw(network, t):
    network.roads.clear()  # vandalism: only the extensions' copy may change
    return [QGraphicsEllipseItem(0, 0, 5, 5)]
'''
FAILS_LATER = '''
calls = []
def draw(network, t):
    calls.append(t)
    if len(calls) > 1:
        return [1 / 0]
    return []
'''


def test_extensions_draw_and_errors_only_switch_them_off():
    with tempfile.TemporaryDirectory() as folder:
        Path(folder, "dot.py").write_text(GOOD, encoding="utf-8")
        Path(folder, "fails_later.py").write_text(FAILS_LATER, encoding="utf-8")
        Path(folder, "no_draw.py").write_text("NAME = 'Nothing'\n", encoding="utf-8")
        Path(folder, "broken.py").write_text("def draw(:\n", encoding="utf-8")
        Path(folder, "quits.py").write_text("exit()\n", encoding="utf-8")
        Path(folder, "bad_panel.py").write_text("def panel():\n    return 1 / 0\n", encoding="utf-8")
        Path(folder, "_helper.py").write_text("raise RuntimeError\n", encoding="utf-8")
        network = generate_network(2, 2)
        roads = set(network.roads)
        scene = NetworkGraphicsScene(network)
        scene.rebuild()
        before = len(scene.items())
        clock = SimulationClock()
        panel = ExtensionsPanel(scene, clock, Path(folder))
        by_name = {extension.path.name: extension for extension in panel.extensions}
        assert sorted(by_name) == ["bad_panel.py", "broken.py", "dot.py", "fails_later.py", "no_draw.py", "quits.py"]
        assert by_name["broken.py"].error.startswith("Line 1: SyntaxError") and "signal_override()" in by_name["no_draw.py"].error
        assert by_name["bad_panel.py"].error == "Line 2: ZeroDivisionError: division by zero"
        assert not by_name["bad_panel.py"].check.isEnabled()
        assert not by_name["broken.py"].check.isEnabled() and by_name["dot.py"].check.text() == "Dot"
        clock.advance(1)
        assert len(scene.items()) == before  # nothing runs until ticked

        dot = by_name["dot.py"]
        dot.check.setChecked(True)
        clock.advance(1)  # a clock step redraws it: still one item
        assert len(scene.items()) == before + 1
        assert set(network.roads) == roads  # the real network is untouched
        scene.rebuild()  # a map redraw clears the scene: the extension draws again
        assert len(scene.items()) == before + 1
        panel.reload()  # after editing: still on, drawn once
        dot = next(e for e in panel.extensions if e.path.name == "dot.py")
        assert dot.on and len(scene.items()) == before + 1

        failing = next(e for e in panel.extensions if e.path.name == "fails_later.py")
        failing.check.setChecked(True)
        clock.advance(1)  # its second draw raises: switched off with the line, the other carries on
        assert not failing.on and not failing.check.isChecked() and dot.on
        assert failing.error == "Switched off: Line 6: ZeroDivisionError: division by zero"
        dot.check.setChecked(False)
        assert len(scene.items()) == before


def test_the_clock_runs_pauses_and_plays():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    clock, labels = window.clock, window.clock_label
    assert window.menuBar().cornerWidget(Qt.Corner.TopRightCorner) is not None  # small, in the corner
    assert not clock.running and window.play_action.isVisible() and not window.pause_action.isVisible()
    window.speed_combo.setCurrentIndex(window.speed_combo.findData(60))
    window.run_action.trigger()  # Play and Pause share one spot: Pause shows while running
    assert clock.running and window.runner.running and window.pause_action.isVisible()
    QTest.qWait(600)  # 0.6 s of real time at 60x: about 36 s simulated, on the physics' own thread
    assert window.runner.frame.time >= 20 and labels.text() == clock_text(clock.time_s)
    window.pause_action.trigger()
    held = window.runner.frame.time
    QTest.qWait(200)
    assert not clock.running and window.play_action.isVisible() and window.runner.frame.time == held
    assert clock.time_s == held  # the clock shows the physics' time
    window.play_action.trigger()  # carries on, not from the start
    QTest.qWait(300)
    assert window.runner.frame.time > held
    window.stop_action.trigger()  # Stop ends the run and goes back to zero
    assert window.runner is None and not clock.running and clock.time_s == 0 and labels.text() == "00:00:00"
    assert window.play_action.isVisible()
    assert window.last_results is not None and window.last_results.exists()  # Stop saved the run's workbook
    from PySide6.QtWidgets import QFileDialog
    real, QFileDialog.getSaveFileName = QFileDialog.getSaveFileName, lambda *args: ("", "")
    try:
        window.export_action.trigger()  # Export asks where (cancelled here: nothing written)
    finally:
        QFileDialog.getSaveFileName = real
    window.close()


def test_batch_run_takes_seeds_like_pages_and_a_duration():
    panel = ExtensionsPanel(NetworkGraphicsScene(generate_network(1, 1)), SimulationClock(), EXTENSIONS_DIR)
    batch = next(extension for extension in panel.extensions if extension.path.name == "batch_run.py")
    parse = batch.module.parse_seeds
    assert parse("45, 34, 1-3") == [45, 34, 1, 2, 3]  # in the order written
    assert parse(" 3 – 5 , 4, 45 ,") == [3, 4, 5, 45]  # spaces, an en dash, a repeat counted once
    for bad in ("", "5-2", "seven", "1-20000"):
        try:
            parse(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} must be refused")

    controls, arrow = batch.controls, batch.arrow  # its settings: a collapsible section
    assert batch.module and not batch.error and controls.isHidden() and not controls.isEnabled()
    arrow.setChecked(True)  # the arrow opens it; usable only while ticked
    assert not controls.isHidden() and not controls.isEnabled()
    arrow.setChecked(False)
    assert controls.isHidden()
    batch.check.setChecked(True)  # ticking it opens its settings, ready to use
    assert arrow.isChecked() and not controls.isHidden() and controls.isEnabled() and not batch.items
    seeds = next(box for box in controls.findChildren(QLineEdit) if box.placeholderText())
    duration, custom, start = controls.findChild(QComboBox), controls.findChild(QSpinBox), controls.findChild(QPushButton)
    labels = lambda: " | ".join(label.text() for label in controls.findChildren(QLabel))  # noqa: E731
    seeds.setText("45, 34, 1-3")
    assert "5 runs: 30 min × 5 seeds = 2.5 h; plain runs go one at a time" in labels() and start.isEnabled()
    assert custom.isHidden()
    seeds.setText("5-2")  # a bad list says why, and can't start
    assert "low to high" in labels() and not start.isEnabled()
    seeds.setText("7-9")
    duration.setCurrentIndex(duration.findData(None))  # Other…: a minutes box appears
    custom.setValue(90)
    assert not custom.isHidden() and "3 runs: 90 min × 3 seeds = 4.5 h" in labels()
    start.click()  # outside the window there is nothing to run with: it says so
    assert "from the ACMS window" in labels()
    # Only time, seeds and two ticks: TSP's models and how are the TSP tab's
    test_tsp, test_devl = controls.findChildren(QCheckBox)
    assert not controls.findChildren(QSlider) and not controls.findChildren(ChoiceButtons)
    assert [box.isChecked() for box in (test_tsp, test_devl)] == [False, False]

    # In the window: Test TSP reads the TSP tab (Multiple: its models, Decide within, Pace, Runs at once, Times each)
    asked, plan = [], {"agents": ["System 1: Laya", "System 2: grok-4"], "deadline_s": 45, "speed": 1, "at_once": 4,
                       "times": 2, "servers": {"System 1: Laya": "laya", "System 2: grok-4": "grok"}}
    devl_plan = {"agents": ["System 2: llama3"], "deadline_s": 20, "speed": 1, "at_once": 4, "times": 2,
                 "servers": {"System 2: llama3": "local"}}
    panel.app = SimpleNamespace(tsp_batch=lambda: plan, devl_batch=lambda: devl_plan,
                                run_batch=lambda *args, **kwargs: asked.append(
                                    (args, kwargs)) or {"file": "batch.xlsx", "runs": 18, "of": 18, "folder": "runs"})
    test_tsp.setChecked(True)
    # 90 min × 3 seeds × (2 models + No LLM) × 2 times = 27 h; 4 at once, but Laya's 6 and Grok's 6 one after another
    assert "18 runs: 90 min × 3 seeds × 3 agents (with No LLM) × 2 times = 27.0 h; 4 at once at 1×: about 9.0 h " \
           "(one run a model server at a time)" in labels()
    plan.update(speed=5, servers={"System 1: Laya": "laya", "System 2: grok-4": "laya"}, at_once=2)
    test_tsp.setChecked(False)  # changed in the TSP tab: the count follows
    test_tsp.setChecked(True)
    assert "2 at once at 5×: about 3.6 h (one run a model server" in labels()  # 12 on Laya: 12 × 90 min / 5
    plan.update(servers={"System 1: Laya": "laya", "System 2: grok-4": "grok"}, at_once=4, speed=1)
    test_devl.setChecked(True)  # both: every TSP agent with every DEVL agent, 3 × 2
    assert "Different" not in labels() and "36 runs: 90 min × 3 seeds × 6 agents (with No LLM) × 2 times = 54.0 h" in labels()
    devl_plan["speed"] = 2
    test_devl.setChecked(False)
    test_devl.setChecked(True)
    assert "set different Pace: make them the same" in labels() and not start.isEnabled()
    devl_plan["speed"] = 1
    test_devl.setChecked(False)
    test_devl.setChecked(True)
    settings = dict(panel.settings())["Batch Run"]
    assert "Test TSP: yes" in settings and "Test DEVL: yes" in settings
    start.click()
    assert asked == [(([7, 8, 9], 90, 2), {"agents": ["System 1: Laya", "System 2: grok-4"], "deadline_s": 45,
                                           "speed": 1, "at_once": 4, "test_tsp": True, "test_devl": True,
                                           "devl_agents": ["System 2: llama3"], "devl_deadline_s": 20})]
    assert "Saved batch.xlsx" in labels()
    plan["agents"] = []  # nothing picked: it says so, and can't start
    test_tsp.setChecked(False)
    test_tsp.setChecked(True)
    assert "Pick a model in the TSP tab" in labels() and not start.isEnabled()
    panel.set_settings(batch, {"Test TSP": "no", "Test DEVL": "no"})  # as the assistant's set_extension does
    assert start.isEnabled()  # plain runs again
    start.click()
    assert asked[-1][1]["test_tsp"] is False and asked[-1][1]["agents"] is None and asked[-1][1]["devl_agents"] is None
    batch.check.setChecked(False)
    assert not controls.isEnabled()


def test_a_long_list_of_models_scrolls_and_every_one_can_be_ticked():
    from traffic_simulator.properties_panel import CheckList

    models = CheckList()
    labels = [f"System 2: model {i}" for i in range(15)]
    models.set_options(labels, set())
    models.resize(250, 28)
    models.show()
    models.showPopup()
    APP.processEvents()
    view = models.view()
    assert view.isVisible() and view.verticalScrollBar().maximum() > 0  # ten rows at once, the rest scroll
    assert view.viewport().height() // view.sizeHintForRow(0) == CheckList.MAX_SHOWN
    for row in (0, 14):  # the first, and the last, scrolled to
        index = models.model().index(row, 0)
        view.scrollTo(index)
        APP.processEvents()
        centre = view.visualRect(index).center()
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=centre)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=centre)
        APP.processEvents()
    assert models.ticked() == [labels[0], labels[14]] and view.isVisible()  # once each, still open while ticking
    QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=centre)  # a double click: two clicks, not three
    APP.processEvents()
    assert models.ticked() == [labels[0], labels[14]] and view.isVisible()
    models.hidePopup()  # closes at once: nothing holds it open
    APP.processEvents()
    assert not view.isVisible()
    models.close()


class FakeAgent:
    """Answers every question at once, and counts them."""

    def __init__(self, answer: bool) -> None:
        self.answer, self.questions = answer, []

    def decide(self, key, question: str) -> bool:
        self.questions.append((key, question))
        return self.answer


def test_tsp_and_devl_are_decided_by_a_system_and_its_model_or_multiple():
    network = generate_network(3, 4)
    panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), EXTENSIONS_DIR)
    assert [extension.name for extension in panel.extensions] == [
        "Batch Run", "Transit Signal Priority (TSP)", "Dynamic Emergency Vehicle Lane (DEVL)", "Dispatch Emergency Vehicle",
        "Research Log"]
    batch, tsp, devl, dispatch, research = panel.extensions
    assert batch.arrow and tsp.arrow and devl.arrow and dispatch.arrow and research.arrow  # each has settings
    assert research.has("log") and not panel.wants("log")  # the log hook, asked for only while ticked
    assert panel.signal_override() is None  # TSP unticked: runs pass no override at all
    tsp.check.setChecked(True)
    overrides = panel.signal_override()
    assert overrides is not None and [extension for extension, _ in overrides.calls] == [tsp]
    options = [{"label": "No LLM (baseline)", "provider": "", "system": "", "model": "", "name": "no-llm"},
               {"label": "System 1: Jev", "provider": "jev", "system": "1", "model": "jev", "name": "jev"},
               {"label": "System 1: Laya", "provider": "laya", "system": "1", "model": "laya", "name": "laya"},
               {"label": "System 2: llama3", "provider": "local", "system": "2", "model": "llama3", "name": "llama3"},
               {"label": "System 2: grok-4", "provider": "grok", "system": "2", "model": "grok-4", "name": "grok-4"}]
    panel.app = SimpleNamespace(agent_options=lambda: options)
    for extension in (tsp, devl):  # the same choices in both
        extension.check.setChecked(True)  # its settings are usable while ticked
        choice = extension.controls.findChild(ChoiceButtons)
        assert [button.text() for button in choice.group.buttons()] == ["System 1", "System 2", "Multiple"]
        model = next(box for box in extension.controls.findChildren(QComboBox) if box.toolTip() == "Model deciding")
        agents = next(box for box in extension.controls.findChildren(QComboBox) if hasattr(box, "ticked"))
        assert agents.isHidden() and not model.isHidden()  # System 1: its model; Multiple's rows hidden
        model.showPopup()  # lists System 1's models, as AI Mode found them
        model.hidePopup()
        assert [model.itemText(i) for i in range(model.count())] == ["Jev", "Laya"]
        model.setCurrentIndex(1)
        assert extension.module.batch_settings() == {"deadline_s": 30, "speed": 1, "at_once": 4, "times": 1,
                                                     "agents": ["System 1: Laya"], "servers": {"System 1: Laya": "laya"}}
        choice.group.button(1).click()  # System 2: its own models
        assert [model.itemText(i) for i in range(model.count())] == ["llama3", "grok-4"]
        panel.set_settings(extension, {"Model": "grok-4"})  # as the assistant's set_extension does
        assert extension.module.batch_settings()["agents"] == ["System 2: grok-4"]
        panel.set_settings(extension, {"Decided by": "Multiple"})
        slider, pace = extension.controls.findChild(QSlider), extension.controls.findChildren(ChoiceButtons)[1]
        assert not agents.isHidden() and model.isHidden() and agents.labels() == [o["label"] for o in options[1:]]
        assert (slider.minimum(), slider.maximum()) == (1, 280) and [b.text() for b in pace.group.buttons()] == ["1×", "2×", "5×", "10×"]
        panel.set_settings(extension, {"Models": "system 1: laya, System 2: grok-4", "Decide within": 45, "Pace": "5×",
                                       "Times each": 2})
        assert extension.module.batch_settings() == {"deadline_s": 45, "speed": 5, "at_once": 4, "times": 2,
                                                     "agents": ["System 1: Laya", "System 2: grok-4"],
                                                     "servers": {"System 1: Laya": "laya", "System 2: grok-4": "grok"}}
        try:
            agents.set_ticked(["System 9"])
        except ValueError as exc:
            assert "Not listed" in str(exc)
        else:
            raise AssertionError("a model that isn't listed must be refused")
    agent = tsp.module.tsp_agent  # TSP's live run asks the System chosen last, with its model
    assert agent.system == "2" and agent.model["label"] == "System 2: grok-4"
    question = agent.question_for("2", {"request": {"exit_has_room": True, "passengers": 1}, "approaches": {}})
    assert question["choice"]["model"] == "grok-4"
    assert "Models: System 1: Laya, System 2: grok-4" in dict(panel.settings())[tsp.name]
    panel.set_settings(tsp, {"Decided by": "System 1"})
    assert agent.system == "1" and agent.model["label"] == "System 1: Jev"  # its first model
    tsp.check.setChecked(False)
    devl.check.setChecked(False)
    assert panel.signal_override() is None
    devl.check.setChecked(True)  # a signal override too: ticked, the run hands it the arms
    assert [extension for extension, _ in panel.signal_override().calls] == [devl]
    panel.run_algorithms(Simulation(network, [], []).frame())
    assert devl.on and not devl.error


BUS = VehicleMode("Bus", PALETTE[0], length_m=12.0, width_m=2.55, max_speed_kmh=100, accel=1.2, decel=4.0)


def test_tsp_keeps_the_plan_unless_priority_acts_and_returns_to_it_after_the_bus():
    network = generate_network(3, 4)
    panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), EXTENSIONS_DIR)
    tsp = next(extension for extension in panel.extensions if extension.path.name == "transit_signal_priority.py")
    tsp.check.setChecked(True)
    agent = tsp.module.tsp_agent  # agent_transit_signal_priority.py: one TRUE/FALSE per bus request
    assert agent.priority("1,1", {}) is False  # nobody to ask, nothing written: FALSE
    buses = [PairDemand("1W", "1E", 120, None, [], "Poisson", (100,)), PairDemand("1S", "3N", 120, None, [], "Poisson", (100,))]

    def planned(sim, junction):  # the junction's own plan now, whatever is shown
        return {link.road: sim.graph.signals[junction].state(link.id, sim.time) for link in sim.graph.arriving[junction]}

    def shown(sim, junction):
        return {arm: state for (node, arm), state in sim.signal_states().items() if node == junction}

    # Nothing granted: the junctions never leave their plans, so the run is the plain run, vehicle for vehicle
    plain, simulation = Simulation(network, buses, [BUS], seed=3), Simulation(network, buses, [BUS], seed=3)
    simulation.signal_override = overrides = panel.signal_override()
    while simulation.time < 600:
        plain.step()
        simulation.step()
        assert simulation.signal_states() == plain.signal_states(), simulation.time
    assert [(v.id, v.x, v.y) for v in simulation.frame().vehicles] == [(v.id, v.x, v.y) for v in plain.frame().vehicles]
    assert not overrides.failed and all(not here.episodes for here in tsp.module._latest.junctions.values())

    # TRUE at 1,1 only: priority acts there, and once each bus has passed the junction goes back to its plan
    agent.decisions["1,1"] = True
    simulation = Simulation(network, buses, [BUS], seed=3)
    simulation.signal_override = overrides = panel.signal_override()
    red = 0
    while simulation.time < 900:
        before, states = {vehicle.id: vehicle.segment for vehicle in simulation.vehicles}, simulation.signal_states()
        simulation.step()
        red += sum(isinstance(vehicle.segment, Turn) and before.get(vehicle.id) not in (None, vehicle.segment)
                   and states.get((vehicle.segment.node, vehicle.segment.lane_in.link.road)) == "R"
                   for vehicle in simulation.vehicles)
        for junction, here in tsp.module._latest.junctions.items():
            if here.mode == "plan":  # on its plan: exactly the plan
                assert shown(simulation, junction) == planned(simulation, junction), (junction, simulation.time)
    here = tsp.module._latest.junctions
    logs = {junction: {event for _, event, _ in junction_here.controller.log} for junction, junction_here in here.items()}
    assert red == 0 and not overrides.failed and {"CHECKIN", "CHECKOUT", "RELEASE"} <= set().union(*logs.values())
    assert {junction for junction, events in logs.items() if events & {"HOLD", "TRUNCATE"}} == {"1,1"}
    episodes, cycle = here["1,1"].episodes, simulation.graph.signals["1,1"].cycle
    finished = [episode for episode in episodes if episode[2] is not None]
    assert episodes and all(junction_here.episodes == [] for junction, junction_here in here.items() if junction != "1,1")
    assert len(finished) >= len(episodes) - 1  # all back on the plan, but one perhaps still on its way at the end
    assert all(episode[0] <= episode[1] <= episode[2] <= episode[1] + 2 * cycle for episode in finished)  # within 2 cycles
    agent.decisions.clear()


def test_tsp_looks_ahead_on_copies_of_the_run_when_asked_to():
    from traffic_simulator.signal_agent import SignalAgent

    network = generate_network(3, 4)
    panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), EXTENSIONS_DIR)
    tsp = next(extension for extension in panel.extensions if extension.path.name == "transit_signal_priority.py")
    tsp.check.setChecked(True)
    chosen = tsp.module.tsp_agent
    questions = []

    def ask(question):
        questions.append(question)
        return {"answer": True, "model": "fake"}

    chosen.system, chosen.lookahead_s = "2", 20.0
    try:
        simulation = Simulation(network, [PairDemand("1W", "1E", 120, None, [], "Poisson", (100,))], [BUS], seed=3)
        simulation.signal_override = panel.signal_override(agent=SignalAgent(ask, wait_s=5))
        while simulation.time < 400 and not questions:
            simulation.step()
        ahead = questions[0]["telemetry"]["lookahead"]  # in the telemetry the model reads
        assert ahead["horizon_s"] == 20.0 and ahead["favours"] in ("priority", "plan")
        assert {"person_delay_s", "vehicle_delay_s", "watched"} <= set(ahead["with_priority"]) == set(ahead["with_plan"])
        assert ahead["person_s_saved_by_priority"] == ahead["with_plan"]["person_delay_s"] - ahead["with_priority"]["person_delay_s"]
        assert "lookahead" in questions[0]["messages"][1]["content"]  # System 2's prompt carries it
    finally:
        chosen.lookahead_s = 0.0
    controls = tsp.controls
    spin = next(box for box in controls.findChildren(QSpinBox) if box.toolTip() == "Rollout horizon")
    spin.setValue(30)
    assert chosen.lookahead_s == 30.0
    spin.setValue(0)
    assert chosen.lookahead_s == 0.0 and spin.text() == "off"


def test_tsp_asks_the_chosen_system_once_per_bus_request_and_reports_it():
    from traffic_simulator.signal_agent import SignalAgent

    network = generate_network(3, 4)
    panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), EXTENSIONS_DIR)
    tsp = next(extension for extension in panel.extensions if extension.path.name == "transit_signal_priority.py")
    tsp.check.setChecked(True)
    module, chosen = tsp.module, tsp.module.tsp_agent
    buses = [PairDemand("1W", "1E", 120, None, [], "Poisson", (100,)), PairDemand("1S", "3N", 120, None, [], "Poisson", (100,))]
    questions = []

    def ask(question):
        questions.append(question)
        return {"answer": True, "model": "fake", "input_tokens": 50, "output_tokens": 1, "reply": "TRUE"}

    for system in ("1", "2"):
        chosen.system = system
        agent = SignalAgent(ask, wait_s=5)  # as a batch run's: it waits for each answer
        questions.clear()
        simulation = Simulation(network, buses, [BUS], seed=3)
        simulation.signal_override = overrides = panel.signal_override(agent=agent)
        while simulation.time < 600:
            simulation.step()
        requests = module._latest.record.requests  # this run's own record
        assert requests and len(questions) == len(requests) == agent.asked and not overrides.failed  # one each
        message = questions[0]["telemetry"]  # both read only the junction's telemetry, JSON
        assert message["schema"] == "acms-tsp-telemetry/1" and message["type"] == "tsp_request"
        assert {"distance_to_stop_line_m", "speed_kmh", "eta_s", "vehicles_ahead"} <= set(message["request"])
        assert message["request"]["passengers"] == 1 and message["request"]["exit_room_m"] > 14.5  # the test bus: 1
        assert message["request"]["exit_has_room"] is True and "people_queued" in next(iter(message["approaches"].values()))
        assert message["spat"]["bus_phase_state"] in ("green", "amber", "red") and message["approaches"]
        assert json.loads(json.dumps(message)) == message  # plain JSON
        request, spat = message["request"], message["spat"]
        assert request["arrives_on"] == ("red" if request["eta_s"] > spat["bus_green_ends_in_s"] else "green") \
            if "bus_green_ends_in_s" in spat else request["arrives_on"] == (
            "red" if request["eta_s"] < spat["bus_green_starts_in_s"] else "green")  # as the signals stand
        if system == "1":  # the facts in words as the state, each read with one typed yes/no
            assert questions[0]["state"] == {"exit_lane": "clear",
                                             "more_people_on": questions[0]["state"]["more_people_on"]}
            assert set(questions[0]["questions"]) == {"exit_clear", "more_on_bus"}
            assert {question["type"] for question in questions[0]["questions"].values()} == {"noul"}
            full = json.loads(json.dumps(message))
            full["request"].update(passengers=40, exit_has_room=False)
            assert chosen.facts(full)["exit_lane"] == "full" and chosen.facts(full)["more_people_on"] == "bus"
        else:  # zero-shot, role-based, the telemetry as JSON, and a boolean tool to answer with
            role, observed = questions[0]["messages"]
            assert role["role"] == "system" and "traffic signal engineer" in role["content"] and "decide_tsp" in role["content"]
            assert json.loads(observed["content"].split("Telemetry now:\n")[1].split("\n\nDecide")[0]) == message
            tool = questions[0]["tools"][0]
            assert tool["name"] == "decide_tsp" and tool["parameters"]["properties"]["activate"]["type"] == "boolean"
            assert questions[0]["answer_from"] == ["decide_tsp", "activate"] and "Example" not in role["content"]
        report = module.report(agent)
        sheets = {name: rows for name, rows, _ in report["sheets"]}
        figures = {row[0]: row[1] for row in sheets["TSP"][1:] if len(row) > 1 and row[0]}
        assert figures["Bus requests"] == len(requests) and figures["Model"] == "fake"
        assert figures["Priority given (TRUE)"] == report["measures"]["tsp_true"] > 0
        assert figures["Tokens in"] == 50 * len(requests) and figures["Green held for buses"] + figures["Greens cut short for buses"] > 0
        assert len(sheets["Agent decisions"]) == 1 + len(requests) and sheets["Agent decisions"][1][7].startswith("TRUE")
        name, feed = report["files"][0]  # the telemetry feed: requests each second while on, then the decisions
        lines = [json.loads(line) for line in feed.splitlines()]
        assert name == "telemetry.jsonl" and {line["type"] for line in lines} == {"tsp_request", "tsp_decision"}
        assert sum(line["type"] == "tsp_decision" for line in lines) == len(requests)
        agent.close()
    chosen.system = "1"


def test_tsp_rings_its_junctions_shows_live_figures_and_reports_each_minute():
    import threading
    import time

    from PySide6.QtGui import QColor

    from traffic_simulator.signal_agent import SignalAgent
    from traffic_simulator.theme import STATUS

    network = generate_network(3, 4)
    panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), EXTENSIONS_DIR)
    tsp = next(extension for extension in panel.extensions if extension.path.name == "transit_signal_priority.py")
    tsp.check.setChecked(True)
    module = tsp.module
    gate = threading.Event()
    agent = SignalAgent(lambda question: gate.wait(10) and {"answer": True, "model": "fake"})  # a live run's
    buses = [PairDemand("1W", "1E", 120, None, [], "Poisson", (100,)), PairDemand("1S", "3N", 120, None, [], "Poisson", (100,))]
    simulation = Simulation(network, buses, [BUS], seed=3)
    simulation.signal_override = panel.signal_override(agent=agent)

    def states():
        run = module._latest  # none before the first step
        return {module.halo(junction, simulation.time) for junction in (list(run.junctions) if run else [])} - {None}

    while "asking" not in states() and simulation.time < 600:
        simulation.step()
    rings = module.draw(network, simulation.time)  # the agent hasn't answered: amber, dashed
    assert rings and {ring.toolTip() for ring in rings} == {"Asking the agent"}
    assert rings[0].pen().color().name() == QColor(STATUS["waiting"]).name()
    assert rings[0].pen().style() == Qt.PenStyle.DashLine
    module.algorithm(simulation.frame())
    assert module._figures.text().startswith("1 bus requests · 0 waiting · 0 answered")
    gate.set()
    while "granted" not in states() and simulation.time < 900:
        simulation.step()
        time.sleep(0.001)  # the agent answers in the background
    rings = [ring for ring in module.draw(network, simulation.time) if ring.toolTip() == "Priority granted"]
    assert rings and rings[0].pen().color().name() == QColor(STATUS["passed"]).name()
    module.algorithm(simulation.frame())
    assert " TRUE · latency " in module._figures.text() and " · 0 errors · " in module._figures.text()
    assert module._figures.text().endswith(" off the plan")
    assert module.draw(network, 0.0) == []  # the clock back at the start (Stop): no rings left from the run
    while simulation.time < 900:
        simulation.step()
    sheets = {name: rows for name, rows, _ in module.report(agent)["sheets"]}
    minutes = sheets["Agent per minute"]
    assert minutes[0][:3] == ["Minute", "Requests raised", "Answers reaching the controller"]
    assert len(minutes) == 1 + int(simulation.time // 60) + 1  # a row each minute the run reached
    assert sum(row[1] for row in minutes[1:]) == len(module._latest.record.requests)  # every request in its minute
    assert any(row[10] for row in minutes[1:])  # the backlog, sampled each second
    agent.close()


def test_no_priority_for_a_bus_whose_exit_lane_is_full():
    network = generate_network(3, 4)
    panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), EXTENSIONS_DIR)
    tsp = next(extension for extension in panel.extensions if extension.path.name == "transit_signal_priority.py")
    tsp.check.setChecked(True)
    module = tsp.module
    module.tsp_agent.decisions.update({junction: True for junction in network.nodes})  # TRUE everywhere
    buses = [PairDemand("1W", "1E", 120, None, [], "Poisson", (100,))]
    simulation = Simulation(network, buses, [BUS], seed=3)
    simulation.signal_override = panel.signal_override()

    def active():
        return [here for here in (module._latest.junctions.values() if module._latest else []) if here.controller.active]

    while simulation.time < 600 and not active():
        simulation.step()
    here = active()[0]
    request = here.controller.active
    lane = next(vehicle["exit"] for info in here.view["arms"].values() for vehicle in info["approaching"]
                if vehicle["id"] in request.bus_ids)
    assert here._room(request) and here._gate(request, here.controller)  # room for it: TRUE stands
    here.view = {**here.view, "exit_room_m": {**here.view["exit_room_m"], lane: 10.0}}  # a queue to its exit's end
    assert not here._room(request) and here._gate(request, here.controller) is False  # refused, whatever the agent
    assert here.controller.log[-1][1] == "BLOCKED" and here.controller.log[-1][2]["phase"] == request.phase_id
    module.tsp_agent.decisions.clear()


def test_what_came_of_each_request():
    from traffic_simulator.extensions import load

    module = load(EXTENSIONS_DIR / "transit_signal_priority.py").module

    def row(**changes):
        return {"withdrawn": False, "guard": "accepted", "fell_back": False, "late": False, "answer": True,
                "phase": "P1", "asked_s": 100.0, **changes}

    log = [(100, "CHECKIN", {"phase": "P1"}), (120, "RELEASE", {"phase": "P1"}), (130, "HOLD", {"phase": "P1"})]
    assert module.outcome(row(), log) == "not needed"  # TRUE, the plan served it: its HOLD came after its release
    assert module.outcome(row(), [(110, "HOLD", {"phase": "P1"})] + log) == "served"
    assert module.outcome(row(), [(110, "TRUNCATE", {"phase": "P1"})] + log) == "served"
    assert module.outcome(row(), [(110, "TRUNCATE", {"phase": "P2"})] + log) == "not needed"  # another phase's
    assert module.outcome(row(), [(105, "BLOCKED", {"phase": "P1"})] + log) == "blocked"
    assert module.outcome(row(), [(105, "END_EXT", {"phase": "P1"})] + log) == "not feasible"
    assert module.outcome(row(answer=False), log) == "refused"
    assert module.outcome(row(answer=False, guard="rejected: activate was 'maybe'"), log) == "rejected"
    assert module.outcome(row(fell_back=True, answer=False), log) == "fell back"
    assert module.outcome(row(late=True), log) == "too late" and module.outcome(row(withdrawn=True), log) == "withdrawn"
    assert module.outcome(row(answer=None), log) == "no answer" and set(module.OUTCOMES) >= {
        "served", "not needed", "blocked", "not feasible", "refused", "rejected", "fell back", "too late", "withdrawn"}


def test_the_agent_answers_the_soonest_bus_first_with_the_freshest_telemetry():
    import threading
    import time

    from traffic_simulator.signal_agent import SignalAgent

    gate, asked = threading.Event(), []

    def ask(question):
        asked.append(question)
        gate.wait(5)
        return {"answer": question != "no"}

    agent = SignalAgent(ask)
    agent.decide("A", "first", urgency=50, once=True)  # the worker takes it at once, and waits on the gate
    time.sleep(0.1)
    agent.decide("B", "b, old", urgency=30, once=True)
    agent.decide("C", "c", urgency=10, once=True)
    agent.decide("B", "b, fresh", urgency=30, once=True)  # still waiting: the fresher telemetry replaces it
    agent.decide("D", "d", urgency=5, once=True)
    assert agent.withdraw("D") and not agent.withdraw("D") and agent.waiting() == 2  # D's bus has gone
    gate.set()
    for _ in range(100):
        if agent.answered == 3:
            break
        time.sleep(0.02)
    assert asked == ["first", "c", "b, fresh"] and agent.withdrawn == 1  # the soonest first; D never asked
    assert agent.decide("B", "b, newer", once=True) is True and agent.asked == 3  # once: not asked again
    assert agent.details("B")["question"] == "b, fresh"
    agent.close()


def test_an_answer_counts_when_it_reaches_the_controller_and_the_deadline_falls_back_to_the_plan():
    import time

    from traffic_simulator import agent_transit_signal_priority as chosen
    from traffic_simulator.signal_agent import SignalAgent

    controller = SimpleNamespace(ids=["P1", "P2"], cur=1, interval="G", t_green_onset=90.0, green_target=20.0,
                                 planned_onset=lambda phase: 130.0)

    def request(t):
        return SimpleNamespace(phase_id="P1", approach_id="1,1-2,1", bus_ids={"b1"}, t_checkin=t, d=150.0, v=10.0, n=2)

    def at(t):
        return {"time_s": t, "arms": {"1,1-2,1": {"approaching": []}, "1,2-1,1": {"approaching": [
            {"id": "c", "type": "Car", "distance_m": 5, "speed_kmh": 0.0, "lane": 0}]}}}

    chosen.reset()
    slow = SignalAgent(lambda question: time.sleep(0.3) or {"answer": True}, wait_s=5)  # an unpaced test's agent
    assert chosen.priority("1,1", at(100.0), request(100.0), controller, slow) is False  # counts 0.3 s later
    row = next(iter(chosen.requests.values()))
    assert row["answer"] is True and 100.29 < row["answered_s"] < 100.6
    message = slow.details("1,1/P1/100")["question"]["telemetry"]  # what it read: the junction's telemetry
    assert message["spat"] == {"current_phase": "P2", "interval": "green", "bus_phase": "P1", "bus_phase_state": "red",
                               "bus_green_starts_in_s": 30}
    assert message["request"]["eta_s"] == 15 and message["approaches"]["1,2-1,1"] == {
        "queued_vehicles": 1, "approaching_vehicles": 1, "people_queued": 1, "bus_approach": False}
    assert message["request"]["passengers"] == 0 and message["request"]["exit_has_room"] is True  # its bus not seen here
    assert chosen.priority("1,1", at(101.0), request(100.0), controller, slow) is True and slow.asked == 1

    chosen.reset()
    late = SignalAgent(lambda question: time.sleep(0.5) or {"answer": True}, wait_s=0.1)  # deadline 0.1 s: missed
    assert chosen.priority("1,1", at(200.0), request(200.0), controller, late) is False
    row = next(iter(chosen.requests.values()))
    assert row["fell_back"] and row["answer"] is False and row["answered_s"] == 200.1
    time.sleep(0.6)  # the answer comes now: kept for the record, not used
    assert chosen.priority("1,1", at(205.0), request(200.0), controller, late) is False and late.timeouts == 1
    found = late.details("1,1/P1/200")
    assert found["fell_back"] and found["answer"] is True

    chosen.reset()
    live = SignalAgent(lambda question: time.sleep(0.2) or {"answer": True})  # a live run's: never waits
    assert chosen.priority("1,1", at(300.0), request(300.0), controller, live) is False  # asked, FALSE meanwhile
    time.sleep(0.4)
    assert chosen.priority("1,1", at(304.0), request(300.0), controller, live) is True
    assert next(iter(chosen.requests.values()))["answered_s"] == 304.0  # reached the controller then

    chosen.reset()  # a paced batch run's: never waits, and an answer must come within 5 simulated s
    gate = __import__("threading").Event()
    paced = SignalAgent(lambda question: gate.wait(5) and {"answer": True}, deadline_s=5)
    assert chosen.priority("1,1", at(500.0), request(500.0), controller, paced) is False
    assert chosen.priority("1,1", at(503.0), request(500.0), controller, paced) is False  # none yet
    assert chosen.priority("1,1", at(506.0), request(500.0), controller, paced) is False  # past it: the plan
    row = next(iter(chosen.requests.values()))
    assert row["fell_back"] and row["answered_s"] == 505.0
    chosen.priority("1,1", at(506.0), request(506.0), controller, paced)  # another bus, waiting behind the first
    chosen.release("1,1", set(), paced)  # both buses gone: the waiting question is withdrawn, never asked
    assert chosen.requests["1,1/P1/506"]["withdrawn"] and paced.withdrawn == 1
    gate.set()
    feed = [message["type"] for message in chosen.feed]
    assert feed.count("tsp_request") == 4 and feed.count("tsp_decision") == 1  # the fallback is a decision too
    assert chosen.priority("1,1", at(400.0), request(400.0), controller, None) is False  # nobody to ask: the plan
    chosen.decisions["1,1"] = True
    assert chosen.priority("1,1", at(401.0), request(401.0), controller, None) is True  # written decisions win
    chosen.decisions.clear()
    chosen.reset()
    for agent in (slow, late, live, paced):
        agent.close()


def devl_run(network, module, agent=None, steps=1600, demand=None):
    """A run with DEVL's override and traffic on the bottom road, an emergency vehicle sent west to east after
    two minutes: (the simulation, the DEVL run, each step's (time, lane states, the arm states at those junctions))."""
    from traffic_simulator.extensions import Extension, SignalOverrides

    demand = demand if demand is not None else [PairDemand(a, b, flow, None, [], "Poisson", (0, 100, 0))
                                                 for a, b, flow in (("1W", "1E", 900), ("1E", "1W", 600), ("1S", "1N", 600))]
    simulation = Simulation(network, demand, MODES, seed=3)
    simulation.signal_override = SignalOverrides([Extension(Path("devl.py"), module)], agent)
    log = []
    for step in range(steps):
        if step == 240:
            simulation.dispatch("1W", "1E")
        simulation.step()
        lanes = simulation.lane_states()
        if lanes:
            junctions = {junction for junction, *_ in lanes}
            log.append((simulation.time, lanes, {k: v for k, v in simulation.signal_states().items() if k[0] in junctions}))
    return simulation, module._runs[agent if agent is not None else module.NOBODY], log


def test_devl_clears_the_kerbside_lane_for_an_emergency_vehicle_and_flashes_its_signal():
    network = generate_network(3, 3)  # two lanes each way
    panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), EXTENSIONS_DIR)
    devl = next(extension for extension in panel.extensions if extension.path.name == "dynamic_emergency_lane.py")
    module = devl.module
    assert not devl.items  # unticked: nothing drawn
    devl.check.setChecked(True)
    signalised = [node for node in network.nodes.values() if node.control == "signalised"]
    arms = sum(node.id in (road.from_node, road.to_node) for node in signalised for road in network.all_roads())
    assert signalised and len(devl.items) == 2 * arms  # a head and its lamp per arriving arm
    for head in devl.items[::2]:  # beside a junction's signals, not along the road
        centre = head.rect().center()
        assert min(((node.x - centre.x()) ** 2 + (node.y - centre.y()) ** 2) ** 0.5 for node in signalised) < 30

    simulation, run, log = devl_run(network, module)  # nobody to ask: the rule, every request TRUE
    keys = list(run.requests)
    vehicle = run.requests[keys[0]]["vehicle"]  # checked in at each junction in turn
    assert keys == [f"devl/{j}/{a}/{vehicle}" for j, a in (("1,1", "1W"), ("2,1", "1,1-2,1"), ("3,1", "2,1-3,1"))]
    trip = next(iter(simulation.emergency_trips.values()))
    assert trip["arrived_s"]  # through all three, on to the end
    for row in run.requests.values():
        assert row["answer"] is True and row["no_agent"] and row["green_s"] >= row["asked_s"] and row["lane"] == 0
    # only the kerbside lane of its arm: every other lane and arm red while it is green or amber
    assert log and all(set(arms.values()) == {"R"} for _, _, arms in log)
    assert all(len(lanes) == 1 and next(iter(lanes))[2] == 0 for _, lanes, _ in log)
    shown = [next(iter(lanes.items())) for _, lanes, _ in log]
    for junction, arm in {(j, a) for (j, a, _), _ in shown}:  # green, then amber, then the plan again
        states = [state for (j, a, _), state in shown if (j, a) == (junction, arm)]
        assert states[0] == "G" and states[-1] == "Y" and "G" not in states[states.index("Y"):]
    assert {junction: [entry[:2] for entry in here.cleared] for junction, here in run.junctions.items() if here.cleared} == {
        "1,1": [("1W", 0)], "2,1": [("1,1-2,1", 0)], "3,1": [("2,1-3,1", 0)]}
    report = module.report(None)
    summary = dict(row for row in report["sheets"][0][1][1:])
    assert summary["Lanes cleared"] == 3 and summary["Decided by nobody (the rule)"] == 3
    assert report["measures"]["devl_cleared"] == 3 and report["files"][0][0] == "devl telemetry.jsonl"
    feed = [json.loads(line) for line in report["files"][0][1].splitlines()]
    assert feed[0]["schema"] == "acms-devl-telemetry/1" and feed[0]["emergency_vehicle"]["eta_s"] > 0

    def lamps(t):
        return [lamp.brush().color().name() for lamp in module.draw(network, t)[1::2]]

    green, dark = module.GREEN.name(), module.UNLIT.name()
    junction = run.junctions["1,1"]
    junction.mode, junction.serving, run.last_t = "clear", keys[0], 0.0  # as it was, clearing 1W at 1,1
    assert lamps(0.2).count(green) == 1 and set(lamps(0.7)) == {dark}  # its lane signal flashing, half a second on
    junction.mode = "plan"
    assert set(lamps(0.2)) == {dark}
    devl.check.setChecked(False)
    assert not devl.items  # unticked: gone


def test_devl_asks_the_agent_once_per_junction_and_clears_only_on_true():
    from traffic_simulator.signal_agent import SignalAgent

    network = generate_network(3, 3)
    panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), EXTENSIONS_DIR)
    module = next(extension for extension in panel.extensions if extension.path.name == "dynamic_emergency_lane.py").module
    asked = []

    def ask(question):  # FALSE at 1,1, TRUE at the others
        asked.append(question)
        return {"answer": question["telemetry"]["junction"] != "1,1", "model": "fake"}

    def slow(question):
        __import__("time").sleep(0.3)
        return ask(question)

    for agent, cleared in ((SignalAgent(ask, wait_s=5), {"2,1", "3,1"}),
                           (SignalAgent(None), {"1,1", "2,1", "3,1"}),  # No LLM: the rule
                           (SignalAgent(slow, deadline_s=0.5), set())):  # every answer too late: the plan
        simulation, run, log = devl_run(network, module, agent, demand=[])
        assert {junction for junction, here in run.junctions.items() if here.cleared} == cleared, agent
        assert len(run.requests) == 3 and all(row["checked_out_s"] for row in run.requests.values())
        rows = module.report(agent)["sheets"][1][1]
        assert rows[0][:8] == ["Asked", "Junction", "Approach", "Lane", "Vehicle", "System", "Model", "Answer"]
        if agent.deadline_s:
            assert all(row["fell_back"] and row["answer"] is False for row in run.requests.values())
        agent.close()
    question = asked[0]  # both systems' forms: System 1's facts in words, System 2's tool
    message = question["telemetry"]
    assert message["approach"] == "1W" and message["lane"] == 0 and message["emergency_vehicle"]["in_the_lane"] is not None
    assert question["state"]["traffic_in_lane"] == "none" and question["answer_from"] == ["decide_devl", "activate"]
    assert set(question["questions"]) == {"in_the_lane"} and question["system"] == "1"


def test_emergency_vehicles_are_dispatched_once_or_on_a_loop():
    network = generate_network(2, 2)
    panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), EXTENSIONS_DIR)
    dispatch = next(extension for extension in panel.extensions if extension.path.name == "dispatch_emergency.py")
    dispatch.check.setChecked(True)
    controls = dispatch.controls
    sent, places = [], ["1E", "1W", "1,1", "2,1"]

    def dispatch_emergency(origin, destination, every_min, check_only=False, after_s=0):
        if destination == "nowhere" or (origin, destination) == ("1E", "2,1"):
            raise ValueError(f"No route from {origin} to {destination}.")
        if not check_only:
            sent.append((origin, destination, every_min) + ((after_s,) if after_s else ()))

    panel.app = SimpleNamespace(places=lambda: places, dispatch_emergency=dispatch_emergency)
    labels = lambda: " | ".join(label.text() for label in controls.findChildren(QLabel))  # noqa: E731
    places_boxes = lambda: [box for box in controls.findChildren(QComboBox) if box.isEditable()]  # noqa: E731
    start, end = places_boxes()[:2]
    boxes = lambda tip: [box for box in controls.findChildren(QSpinBox) if box.toolTip() == tip]  # noqa: E731
    modes = lambda: [box for box in controls.findChildren(QComboBox) if not box.isEditable()]  # noqa: E731
    ticks = lambda: controls.findChildren(QCheckBox)  # noqa: E731
    minutes = boxes("Minutes; Every 0: once")[0]
    assert minutes.suffix() == " min" and minutes.value() == 0 and modes()[0].currentText() == "Every"  # 0: once
    buttons = {button.toolTip(): button for button in controls.findChildren(QPushButton)}
    assert {"Random", "Add Schedule", "Delete Schedule", "Dispatch ticked schedules", "Dispatch every vehicle"} <= set(buttons)
    start.setCurrentText("1W")
    picks = [button for button in controls.findChildren(QPushButton) if button.toolTip() == "Random"]
    assert all(button.property("role") == "mini" for button in picks)  # small and flat beside its box
    random_to = picks[1]
    random_to.click()  # a random end or junction, never where it starts
    assert end.currentText() in places and end.currentText() != "1W"
    end.setCurrentText("1E")
    buttons["Dispatch ticked schedules"].click()  # nothing ticked: nothing goes
    assert sent == [] and "Tick a schedule" in labels()
    ticks()[0].setChecked(True)
    buttons["Dispatch ticked schedules"].click()
    assert sent == [("1W", "1E", 0)] and "Sent 1W → 1E" in labels()
    buttons["Add Schedule"].click()  # another schedule
    assert dispatch.module.fleet() == [("1W", "1E", 0, 0), ("", "", 0, 0)]
    start2, end2 = places_boxes()[2:4]
    start2.setCurrentText("2,1")
    end2.setCurrentText("nowhere")
    boxes("Minutes; Every 0: once")[1].setValue(5)
    buttons["Dispatch every vehicle"].click()  # one can't go: none goes
    assert len(sent) == 1 and "nowhere" in labels()
    end2.setCurrentText("1W")
    buttons["Dispatch every vehicle"].click()
    assert sent[1:] == [("1W", "1E", 0), ("2,1", "1W", 5)] and "every 5 min" in labels()
    modes()[1].setCurrentText("After")  # once, 5 minutes from now
    assert dispatch.module.fleet()[1] == ("2,1", "1W", 0, 5)
    del sent[:]
    buttons["Dispatch every vehicle"].click()
    assert sent == [("1W", "1E", 0), ("2,1", "1W", 0, 300)] and "in 5 min" in labels()
    del sent[:]
    buttons["Delete Schedule"].click()  # the ticked one goes
    assert dispatch.module.fleet() == [("2,1", "1W", 0, 5)]

    # Random Emergency: the seed's own vehicle, the same each time, from one place to another it can reach, later
    seed = boxes("Random emergency seed")[0]
    module, drawn = dispatch.module, []
    for value in (7, 7, 8):
        seed.setValue(value)
        buttons["Random Emergency"].click()
        drawn.append(sent.pop())
    assert drawn[0] == drawn[1] != drawn[2]  # a fixed seed, a fixed vehicle
    for start_at, end_at, every_min, *after in drawn:
        assert start_at in places and end_at in places and start_at != end_at and every_min == 0
        assert 0 <= (after or [0])[0] <= module.MAX_DELAY_MIN * 60
    assert f"Seed 8: an emergency vehicle from {drawn[2][0]} to {drawn[2][1]} in" in labels()
    assert all(module.random_emergency(seed_, places, lambda a, b: dispatch_emergency(a, b, 0, True))[:2] != ("1E", "2,1")
               for seed_ in range(50))  # never one that can't go
    try:
        module.random_emergency(1, places, lambda a, b: (_ for _ in ()).throw(ValueError("Start a run first.")))
    except ValueError as exc:
        assert "Start a run first" in str(exc)  # nothing can go: why not
    else:
        raise AssertionError("with nothing that can go, there's no random emergency")


FORCE_GREEN = '''
def signal_override(junction, arm, state, context, agent):
    return "G" if agent.decide(junction, "force?") else state
'''
BROKEN_OVERRIDE = '''
def signal_override(junction, arm, state, context, agent):
    return state if context["time_s"] < 5 else 1 / 0
'''


def test_signal_overrides_act_inside_the_signals_and_errors_switch_them_off():
    with tempfile.TemporaryDirectory() as folder:
        Path(folder, "force.py").write_text(FORCE_GREEN, encoding="utf-8")
        Path(folder, "broken.py").write_text(BROKEN_OVERRIDE, encoding="utf-8")
        network = generate_network(2, 2)
        panel = ExtensionsPanel(NetworkGraphicsScene(network), SimulationClock(), Path(folder))
        broken, force = panel.extensions
        simulation = Simulation(network, [], [])
        assert "R" in simulation.signal_states().values()  # the plans: one direction red
        force.check.setChecked(True)
        panel.agent = FakeAgent(True)
        simulation.signal_override = panel.signal_override()
        simulation.step()
        assert set(simulation.signal_states().values()) == {"G"}  # overridden inside every signal
        panel.agent = FakeAgent(False)
        simulation.signal_override = panel.signal_override()
        simulation.step()
        assert "R" in simulation.signal_states().values()  # FALSE: the plans again
        broken.check.setChecked(True)
        overrides = simulation.signal_override = panel.signal_override()
        while simulation.time < 10:
            simulation.step()  # raises from 5 s: dropped at once, the run carries on
        assert [extension for extension, _ in overrides.calls] == [force] and len(overrides.failed) == 1
        panel.fail(*overrides.failed.pop())
        assert not broken.on and broken.error == "Switched off: Line 3: ZeroDivisionError: division by zero"


def test_the_signal_agent_never_waits():
    import time

    from traffic_simulator.signal_agent import SignalAgent, parse

    replies = []
    agent = SignalAgent(lambda question: replies.append(question) or ("TRUE" if "bus" in question else "false"))
    assert agent.decide("A", "a bus?") is False  # asked in the background: FALSE until it answers
    for _ in range(50):
        if agent.answered:
            break
        time.sleep(0.01)
    assert agent.decide("A", "a bus?") is True and replies == ["a bus?"]  # the same question isn't asked again
    agent.decide("A", "a car?")
    time.sleep(0.1)
    assert agent.decide("A", "a car?") is False and len(replies) == 2
    failing = SignalAgent(lambda question: 1 / 0)  # no model, a network error...: FALSE
    failing.decide("B", "anything?")
    time.sleep(0.1)
    assert failing.decide("B", "anything?") is False and "division" in failing.last_error
    assert SignalAgent(None).decide("C", "?") is False  # nobody to ask
    assert parse("TRUE") and parse(" yes, override") and not parse("FALSE") and not parse("maybe")
    assert parse("call decide_tsp: activate true") and not parse("No, keep the plan") and not parse("nothing")


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
