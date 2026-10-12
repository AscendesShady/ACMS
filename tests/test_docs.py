"""The ARCHITECTURE.md index and docs/architecture/. Run with `python tests/test_docs.py` (or the test monitor)."""

import re
from pathlib import Path

from traffic_simulator import assistant

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")


def test_the_index_stays_short():
    assert len(INDEX.splitlines()) <= 150


def test_every_module_is_in_the_module_map():
    mapped = set(re.findall(r"^\| ([\w]+\.py) \|", INDEX, re.M))
    modules = {p.name for p in (ROOT / "src" / "traffic_simulator").glob("*.py")} - {"__init__.py"}
    assert modules <= mapped, modules - mapped


def test_every_linked_doc_exists():
    links = re.findall(r"\]\((docs/[^)]+)\)", INDEX)
    assert links and all((ROOT / link).is_file() for link in links), [l for l in links if not (ROOT / l).is_file()]


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
