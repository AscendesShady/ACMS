"""The ARCHITECTURE.md index and docs/architecture/. Run with `python tests/test_docs.py` (or the test monitor)."""

import re
import subprocess
import sys
from pathlib import Path

from traffic_simulator import assistant

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")


def test_the_index_stays_short():
    assert len(INDEX.splitlines()) <= 170  # one row per module; the split added ~26


def test_every_module_is_in_the_module_map():
    mapped = set(re.findall(r"^\| ([\w]+\.py) \|", INDEX, re.M))
    modules = {p.name for p in (ROOT / "src" / "traffic_simulator").glob("*.py")} - {"__init__.py"}
    assert modules <= mapped, modules - mapped


def test_every_module_marked_no_qt_imports_without_qt():
    # One interpreter imports them in table order, so an import that only works after another module was imported
    # first is not caught (a known ceiling); it also checks no module under src imports main.py.
    plain = re.findall(r"^\| (\w+)\.py \|.*\| no \|$", INDEX, re.M)
    check = ("import importlib, sys\n"
             f"for name in {plain!r}:\n"
             "    importlib.import_module('traffic_simulator.' + name)\n"
             "    if any(k.startswith('PySide6') for k in sys.modules) or 'traffic_simulator.main' in sys.modules:\n"
             "        print(name); break\n")
    loaded = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, timeout=120, check=True)
    assert plain and not loaded.stdout.strip(), loaded.stdout


def test_every_script_compiles_without_warnings():
    # A "\S" in a plain docstring (".venv\Scripts") is a SyntaxWarning today and an error in a later Python.
    import warnings

    for path in sorted(p for folder in ("src", "extensions", "tools", "tests") for p in (ROOT / folder).rglob("*.py")):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_no_module_passes_on_another_modules_names():
    # A module that imports a project name it never uses is passing it on: importers then reach the name through it
    # (341 uses went through network_tools to 54 names cut out of it), a change to the real module reaches all of
    # them, and no test can be picked by the module touched. Each name is imported from the module that defines it.
    # The same check is the project's lint step for every import (it needs no package): a name imported and never
    # used is an error unless its line says `# noqa: F401` (properties_panel's shared widgets, promised to drop-in
    # extensions; an import made for its effect).
    import ast

    excused = re.compile(r"#\s*noqa(?!:)|#\s*noqa:[^#]*F401")  # not E402 and the like: those excuse something else
    idle = {}
    for path in sorted(p for folder in ("src", "extensions", "tools", "tests") for p in (ROOT / folder).rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree, lines = ast.parse(text), text.splitlines()
        used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and getattr(node, "module", "") != "__future__" \
                    and not excused.search(" ".join(lines[node.lineno - 1:node.end_lineno])):
                unused = {(a.asname or a.name).split(".")[0] for a in node.names} - used
                if unused:
                    idle[f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"] = sorted(unused)
    assert not idle, idle


def test_the_windows_state_is_born_while_it_is_built():
    # MainWindow is one object in 12 files. A mixin method that creates an attribute makes state that exists only
    # once that method has run, declared nowhere: every attribute is first set while the window is built
    # (MainWindow.__init__ and the LayoutMixin methods it calls), and a mixin's methods only set it again.
    import ast

    building = {"__init__", "_build_ui", "_build_menu", "_build_sim_bar", "_connect_signals"}
    born, later = set(), {}
    for path in sorted((ROOT / "src" / "traffic_simulator").glob("main*.py")):
        for owner in ast.parse(path.read_text(encoding="utf-8")).body:
            for method in owner.body if isinstance(owner, ast.ClassDef) else ():
                if not isinstance(method, ast.FunctionDef):
                    continue
                for node in ast.walk(method):
                    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) \
                            and isinstance(node.value, ast.Name) and node.value.id == "self":
                        if method.name in building and path.stem in ("main", "main_layout"):
                            born.add(node.attr)
                        else:
                            later.setdefault(node.attr, f"{path.name}: {method.name}")
    assert len(born) > 80
    assert not set(later) - born, {name: where for name, where in later.items() if name not in born}


def test_each_mixin_declares_what_it_needs_of_the_window_and_the_window_has_it():
    # MainWindow is one object in 12 files; a mixin said nothing of what it used. Each declares it (NEEDS: the
    # window's state and the other mixins' methods it uses and doesn't define), kept exact here, so a new dependency
    # shows in the diff; and the window has every one. LayoutMixin builds the window itself: it needs all of it.
    import ast
    import os

    declared = {}
    for path in sorted((ROOT / "src" / "traffic_simulator").glob("main_*.py")):
        for owner in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(owner, ast.ClassDef) or not owner.name.endswith("Mixin") or owner.name == "LayoutMixin":
                continue
            own = {node.name for node in owner.body if isinstance(node, ast.FunctionDef)}
            own |= {target.id for node in owner.body if isinstance(node, ast.Assign) for target in node.targets
                    if isinstance(target, ast.Name)}
            used = {node.attr for node in ast.walk(owner) if isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name) and node.value.id == "self"
                    and not (node.attr.startswith("__") and node.attr.endswith("__"))}
            needs = next(ast.literal_eval(node.value) for node in owner.body if isinstance(node, ast.Assign)
                         and any(getattr(target, "id", "") == "NEEDS" for target in node.targets))
            assert list(needs) == sorted(used - own - {"NEEDS"}), (owner.name, set(needs) ^ (used - own - {"NEEDS"}))
            declared[owner.name] = needs
    assert len(declared) == 10
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from traffic_simulator.main import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._ask_to_save = False
    missing = {name: [need for need in needs if not hasattr(window, need)] for name, needs in declared.items()}
    window.close()
    assert app and not any(missing.values()), missing


def test_the_qt_free_mixins_are_built_and_run_alone_without_qt_or_a_window():
    # The three mixins that import no Qt are built on their own, their NEEDS given by stand-ins, in a Python
    # without PySide6: the run tools answer and a headless run goes through to its workbook.
    check = r"""
import sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from traffic_simulator import experiments
from traffic_simulator.demand import PairDemand
from traffic_simulator.main_agents import AgentsMixin
from traffic_simulator.main_experiments import ExperimentsMixin
from traffic_simulator.main_scenario import ScenarioMixin
from traffic_simulator.network_generator import generate_network
from traffic_simulator.vehicle import VehicleMode

class Alone(ExperimentsMixin, AgentsMixin, ScenarioMixin):
    pass

alone, folder = Alone(), Path(tempfile.mkdtemp())
modes = [VehicleMode("Car", "#3987e5")]
demand = [PairDemand("1W", "1E", 400.0, None, [], "Random", (0, 100, 0))]
given = {"network": generate_network(1, 1), "vehicle_modes": SimpleNamespace(modes=modes), "tests": {},
         "runner": None, "_testing": False, "results_folder": folder, "last_kpis": None, "last_results": None,
         "seed_box": SimpleNamespace(value=lambda: 1), "_pair_demand": lambda: demand,
         "_in_background": lambda work, what: work(lambda text: None, lambda: False),
         "_run_settings": lambda *args, **kwargs: [("Kind", "alone")], "_journal_sheet": lambda: [],
         "_refresh_summary": lambda: None, "_waiting_agent": lambda *args, **kwargs: None, "_results": lambda: None,
         "_one_run": lambda network, demand, modes, seed, duration_s, override, settings, extra, label, progress,
             cancelled, folder=None, **kwargs: experiments.recorded_run(network, demand, modes, seed, duration_s,
             settings, label, folder or alone.results_folder, override=override, extra_inputs=extra,
             **{k: v for k, v in kwargs.items() if k != "model"}),
         "extensions": SimpleNamespace(signal_override=lambda **kwargs: None, extensions=[])}
for name, value in given.items():  # stand-ins for what these tools need of the window; nothing else is there
    setattr(alone, name, value)
assert alone._runs_command("design_audit", {})["rules"]
answer = alone._runs_command("run_simulation", {"duration_min": 2, "seed": 3})
assert answer["run"] == "R1" and Path(answer["file"]).is_file() and alone.tests["R1"]["kpis"]["arrived"] > 0
assert alone._runs_command("read_results", {"id": "R1"})
assert not any(name.startswith("PySide6") for name in sys.modules), "Qt was loaded"
print("alone")
"""
    done = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, timeout=300)
    assert done.stdout.strip().endswith("alone"), done.stdout + done.stderr


def test_every_linked_doc_exists():
    links = re.findall(r"\]\((docs/[^)]+)\)", INDEX)
    assert links and all((ROOT / link).is_file() for link in links), [l for l in links if not (ROOT / l).is_file()]


def test_the_icm_layers_keep_their_budgets_and_their_paths_exist():
    # Interpretable Context Methodology: Layer 0 ~800 tokens, Layer 1 ~300, a stage contract 200-500 (~4 characters a
    # token); each stage has its contract's sections, labels each input Layer 3 or 4, and names only real paths.
    assert len((ROOT / "AGENTS.md").read_text(encoding="utf-8")) <= 3200
    routing = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    assert len(routing) <= 1200
    stages = sorted((ROOT / "stages").glob("*/[0-9][0-9]_*/CONTEXT.md"))
    assert len(stages) >= 10
    for pipeline in {stage.parent.parent.name for stage in stages}:
        assert f"`stages/{pipeline}/" in routing, pipeline  # every pipeline is routed to
    local = ("references/", "reports/")  # kept on this PC only, not in a clone
    for stage in stages:
        text = stage.read_text(encoding="utf-8")
        assert len(text) <= 2000, (stage, len(text))
        assert all(f"\n## {part}\n" in text for part in ("Inputs", "Process", "Outputs", "Done when")), stage
        layers = re.findall(r"^\| (\S+) \| `", text.split("## Inputs")[1].split("## Process")[0], re.M)
        assert layers and set(layers) <= {"3", "4"}, (stage, layers)
        for path in re.findall(r"`((?:src|tests|docs|tools|research|stages|extensions|config)/[^`\s<*]+|[A-Z]+\.md)`", text):
            assert (ROOT / path.rstrip("/")).exists() or "/output/" in path, (stage, path)
        for after in re.findall(r"Next: `([^`]+)`", text):
            assert (stage.parent / after).resolve().is_file(), (stage, after)
    loose = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("CONTEXT.md") if p.parent != ROOT
             and not {".venv", "stages", *(name.rstrip("/") for name in local)} & set(p.relative_to(ROOT).parts)]
    assert not loose, loose  # contracts live in stages/, not in the folders they work on


def test_the_warm_up_reads_the_index_not_the_module_files():
    assert assistant.DOCS == ("README.md", "ARCHITECTURE.md")
    prompt = assistant.system_prompt()
    assert "# ACMS Architecture" in prompt and "docs/architecture/<module>.md" in prompt
    assert "## Setup" not in prompt and "## Setup" in assistant.system_prompt(tools=False)  # README: read_source
    detail = (ROOT / "docs" / "architecture" / "simulation.md").read_text(encoding="utf-8").splitlines()[2]
    assert detail and detail not in prompt  # a module file's own text isn't sent


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
