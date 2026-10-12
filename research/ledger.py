"""The PDF ledger: tool ideas for the agentic simulator, read chunk by chunk from the PDFs in
references/transportation_engineering_books/ and references/transportation_engineering_papers/ (their text in references/pdf_text/<source>/,
25 pages a chunk, made by research/extract.sh).

Each source keeps two CSV files in research/ledger/ (the truth, one writer a source): <source>.chunks.csv, a row per
chunk read, and <source>.ideas.csv, a row per idea. Every write rebuilds research/tool_ideas.xlsx from all of them
(Ideas, Chunks, Sources sheets; Shortlist and Re-read from research/shortlist.csv and reread.csv, the merged
ranking and what was only skimmed), so the work can be picked up where it stopped.

    .venv/Scripts/python.exe research/ledger.py todo SOURCE          chunks of SOURCE not read yet
    .venv/Scripts/python.exe research/ledger.py idea SOURCE JSON     an idea or a list of them (fields: IDEA_FIELDS;
                                                                     "-": the JSON on stdin)
    .venv/Scripts/python.exe research/ledger.py done SOURCE CHUNK "what it covers" [verdict]
    .venv/Scripts/python.exe research/ledger.py build                the workbook again
    .venv/Scripts/python.exe research/ledger.py status               chunks read and ideas, per source
    .venv/Scripts/python.exe research/ledger.py existing             research/acms_tools.md again, from the app as it is
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from pathlib import Path

from traffic_simulator.xlsx import write_xlsx

HERE = Path(__file__).resolve().parent
TEXT, LEDGER, WORKBOOK = HERE.parent / "references" / "pdf_text", HERE / "ledger", HERE / "tool_ideas.xlsx"
IDEA_FIELDS = ["topic", "idea", "tool", "does", "inputs", "outputs", "fits", "existing", "value", "effort", "pages",
               "cite", "notes"]
IDEA_HELP = {
    "topic": "a few words, e.g. 'HCM signalised intersection LOS'",
    "idea": "what the source says: the method, model or formula, specifically",
    "tool": "a snake_case name for the assistant's tool (or extension/feature) it suggests",
    "does": "what that tool would do for the agentic simulator, in one or two sentences",
    "inputs": "what it takes", "outputs": "what it returns",
    "fits": "where it would live: network_tools / experiments / simulation / vehicle / extension / results / ai_mode",
    "existing": "ACMS today: none, partial: <tool>, or covered: <tool> (research/acms_tools.md)",
    "value": "high / medium / low for an agentic traffic simulator", "effort": "S / M / L",
    "pages": "the PDF pages it is on, e.g. 112-118", "cite": "chapter, section, equation or table",
    "notes": "anything else (caveats, data it needs)"}
CHUNK_FIELDS = ["chunk", "covers", "verdict", "ideas", "read_at"]


def prefix(source: str) -> str:
    """An idea ID's start, one a source: "SRC_011" for a book, the paper's name without spaces ("ChatSUMOV2")."""
    return source[:7] if re.match(r"SRC_\d{3}", source) else re.sub(r"\W", "", source)


def chunks(source: str) -> list[str]:
    return sorted(path.name[:-4] for path in (TEXT / source).glob("p*.txt"))


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def _append(path: Path, fields: list[str], row: dict) -> None:
    new = not path.is_file()
    with open(path, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fields)
        if new:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


def sources() -> list[str]:
    return sorted(path.name for path in TEXT.iterdir() if path.is_dir()) if TEXT.is_dir() else []


def build() -> None:
    """research/tool_ideas.xlsx from every source's CSVs (skipped, saying so, while Excel holds it open)."""
    ideas, read, summary = [], [], []
    for source in sources():
        found, done = _rows(LEDGER / f"{source}.ideas.csv"), _rows(LEDGER / f"{source}.chunks.csv")
        ideas += [[row["id"], source] + [row.get(key, "") for key in IDEA_FIELDS] for row in found]
        read += [[source] + [row.get(key, "") for key in CHUNK_FIELDS] for row in done]
        total = len(chunks(source))
        summary.append([source, total, len(done), total - len({row["chunk"] for row in done}), len(found),
                        sum(1 for row in found if row.get("value") == "high")])
    def table(name: str) -> list[list]:  # research/<name>.csv, the merged and ranked synthesis, as rows
        path = HERE / f"{name}.csv"
        if not path.is_file():
            return []
        with open(path, newline="", encoding="utf-8") as file:
            rows = list(csv.reader(file))
        return [[cell.replace("_", " ").capitalize() for cell in rows[0]]] + rows[1:]

    sheets = [("Shortlist", table("shortlist"))] if table("shortlist") else []
    sheets += [("Ideas", [["ID", "Source"] + [key.capitalize() for key in IDEA_FIELDS]] + ideas),
              ("Chunks", [["Source", "Chunk", "Covers", "Verdict", "Ideas so far", "Read at"]] + read),
              ("Sources", [["Source", "Chunks", "Read", "Left", "Ideas", "High value"]] + summary)]
    sheets += [("Re-read", table("reread"))] if table("reread") else []
    temporary = WORKBOOK.with_name(f"~{os.getpid()}.{WORKBOOK.name}")
    write_xlsx(temporary, sheets)
    try:
        os.replace(temporary, WORKBOOK)
    except PermissionError:  # open in Excel: the CSVs hold everything, the next write tries again
        temporary.unlink(missing_ok=True)
        print("tool_ideas.xlsx is open elsewhere: not rebuilt this time (the CSVs have the row).")


def existing() -> None:
    """research/acms_tools.md again, from the app as it is: the assistant's tools (network_tools.TOOLS, the first
    sentence of each), the `existing` column's reference. What the app does and lacks beyond its tools stays in
    docs/architecture/capabilities.md and not_built.md (pointed to, not copied, so nothing drifts)."""
    from traffic_simulator.network_tools import TOOLS

    lines = ["# ACMS today: the assistant's tools and what the app does", "",
             "Generated from src/traffic_simulator/network_tools.py (TOOLS) for research/ledger.py's `existing` column "
             "(`ledger.py existing` writes it again).", "", f"## The assistant's {len(TOOLS)} tools", ""]
    for tool in TOOLS:
        first = re.split(r"(?<=\.)\s", tool["description"].strip(), maxsplit=1)[0]
        lines.append(f"- **{tool['name']}**{' (changes)' if tool.get('edits') else ''}: {first}")
    lines += ["", "What the app does beyond its tools, and what it lacks: docs/architecture/capabilities.md and "
              "not_built.md (read those too before marking an idea new)."]
    (HERE / "acms_tools.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"research/acms_tools.md: {len(TOOLS)} tools")


def main(args: list[str]) -> None:
    LEDGER.mkdir(exist_ok=True)
    command = args[0] if args else "status"
    if command == "todo":
        done = {row["chunk"] for row in _rows(LEDGER / f"{args[1]}.chunks.csv")}
        left = [chunk for chunk in chunks(args[1]) if chunk not in done]
        print("\n".join(left) or "all read")
    elif command == "idea":
        source = args[1]
        given = json.loads(sys.stdin.read() if args[2] == "-" else args[2])  # "-": the JSON on stdin (a heredoc)
        rows = given if isinstance(given, list) else [given]
        for row in rows:  # all checked before any is written
            unknown = set(row) - set(IDEA_FIELDS)
            if unknown or not row.get("tool") or not row.get("idea"):
                sys.exit(f"Fields: {', '.join(IDEA_FIELDS)} (tool and idea needed); unknown: {', '.join(unknown) or 'none'}.")
        path = LEDGER / f"{source}.ideas.csv"
        for row in rows:
            row["id"] = f"{prefix(source)}-{len(_rows(path)) + 1:03d}"
            _append(path, ["id"] + IDEA_FIELDS, row)
            print(f"added {row['id']}")
        build()
    elif command == "done":
        source, chunk, covers = args[1], args[2], args[3]
        if chunk not in chunks(source):
            sys.exit(f"No chunk {chunk} in {source}: {', '.join(chunks(source)[:3])}...")
        ideas = len(_rows(LEDGER / f"{source}.ideas.csv"))  # the source's ideas so far
        _append(LEDGER / f"{source}.chunks.csv", CHUNK_FIELDS, {
            "chunk": chunk, "covers": covers, "verdict": args[4] if len(args) > 4 else "", "ideas": ideas,
            "read_at": time.strftime("%Y-%m-%d %H:%M")})
        build()
        print(f"{source} {chunk} done; {len(chunks(source)) - len(_rows(LEDGER / f'{source}.chunks.csv'))} left")
    elif command == "build":
        build()
    elif command == "help":
        print(json.dumps(IDEA_HELP, indent=1))
    elif command == "existing":
        existing()
    else:
        for source in sources():
            print(f"{len(_rows(LEDGER / f'{source}.chunks.csv')):3d}/{len(chunks(source)):3d} read, "
                  f"{len(_rows(LEDGER / f'{source}.ideas.csv')):3d} ideas  {source}")


if __name__ == "__main__":
    main(sys.argv[1:])
