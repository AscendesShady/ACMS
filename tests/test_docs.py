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
