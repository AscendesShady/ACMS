"""Research Log: every AI Mode step recorded for a paper.

Ticked, each AI Mode answer (a step, or chat), and each batch run (a step of kind "batch": its settings, the
batch's and every run's workbooks with their SHA-256; MainWindow._log_step) is written to a session
folder in the results folder, `ACMS research log <date time> <agent>/` (the AI agent of its first step, e.g. Ollama
llama3.1-8b; "no agent" when a batch came first):

    session.json    when it started and ended, the app and its code's SHA-256, Python, Qt and the OS: what to
                    cite and repeat; your verdict on the session and its note
    steps.csv       one row a step: prompt, what was understood, tools, decisions, changes, timings, tokens,
                    your verdict; the table to analyse
    report.html     the session's verdict, then every step as prompt -> what it understood -> what it did ->
                    the map or demand after (if changed) -> its numbers
    <folder>.odt    once the session ends: all of it as one document to read like a human (Word opens it): the
                    session's facts, then each step's prompt, editor update, understanding, plan, tool calls with
                    their arguments and results, the model's messages, reply, changes, map, files, numbers, verdict
    steps/0001/     prompt.md (with the editor update sent before it), understood.json (the model's own Plan:
                    line and the tools it was offered), plan.md (an approval card's plan),
                    calls.jsonl (each tool call: arguments, decision, result, when), reply.md, messages.json (the
                    model's raw messages, thinking included), changes.md (the network, demand and vehicle types
                    it changed), map.png (the map after, if the network changed), demand.png (the OD matrix
                    after, if the demand changed), artifacts.json (the workbooks it wrote, SHA-256),
                    step.json (all of it, with the timings and tokens, and your verdict), activity.csv (with
                    Watch my edits on: every click, entry, choice and edit the user made since the step before)

and the results folder keeps `sessions.csv`: one row a session (its steps, how many of each verdict, the
session's verdict). The snapshot of a step is its change list and a picture of what changed: the map if the
network did, the OD matrix if the demand did, none if nothing changed. Keys are never
written. Its panel marks the latest step As intended, Partly or Not as intended; End session closes the session
(the next question starts another) and opens History, where any session's chats and the session itself are
ranked. Everything is read back from the folders, so a session from another day can be ranked too. It uses the
app's log() hook (extensions.py).
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import platform
import sys
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
                               QSplitter, QTextBrowser, QVBoxLayout, QWidget)

from traffic_simulator.widgets import hint_label, icon_button

NAME = "Research Log"
ORDER = 5
DESCRIPTION = ("Record every AI Mode step and batch run for research: your prompt, what the model understood, what it did, "
               "the changes and a picture of what changed (the map or the demand), with timings, tokens and your verdicts on each "
               "chat and each session.")
VERDICTS = ("as intended", "partly", "not as intended")
COLUMNS = ["Step", "Kind", "Started", "Ended", "Provider", "Model", "Prompt", "Plan", "Tools offered", "Tools", "Applied", "Skipped", "Refused", "Reads", "Changes", "Model s", "Your decisions s",
           "Compute s", "Total s", "Input tokens", "Output tokens", "Requests", "Error", "Verdict", "Note", "Reply",
           "Folder"]
SESSION_COLUMNS = ["Session", "Started", "Ended", "Steps", "As intended", "Partly", "Not as intended", "Unmarked",
                   "Models", "Verdict", "Note", "Folder"]
PREFIX = "ACMS research log "
PICTURES = (("map.png", "The map after"), ("demand.png", "The demand after"))  # a step's snapshot pictures
_VERDICT_BUTTONS = ((0xE8FB, "As intended", "as intended"), (0xE7BA, "Partly as intended", "partly"),
                    (0xE711, "Not as intended", "not as intended"))

_session: dict = {"folder": None, "root": None}  # the session being recorded, and the results folder it's in
_ui: dict = {}  # the panel's labels and note box, while it exists


def _code_hash() -> str:
    """SHA-256 of the app's source (src/traffic_simulator/*.py, sorted): which code made these results."""
    digest = hashlib.sha256()
    for path in sorted((Path(__file__).resolve().parents[1] / "src" / "traffic_simulator").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _version() -> str:
    try:
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        return next(line.split("=", 1)[1].strip().strip('"') for line in text.splitlines() if line.startswith("version"))
    except (OSError, StopIteration):
        return "unknown"


def agent_name(provider, model) -> str:
    """The AI agent as a folder name may hold it: "Ollama llama3.1-8b" (Windows refuses : / \\ * ? " < > |)."""
    from traffic_simulator.assistant import PROVIDER_NAMES

    text = " ".join(str(part) for part in (PROVIDER_NAMES.get(provider, provider), model) if part)
    return "".join("-" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in text).strip(" .") or "no agent"


def _start(results_folder: str, provider=None, model=None) -> Path:
    import PySide6

    agent = agent_name(provider, model)
    name = Path(results_folder) / f"{PREFIX}{time.strftime('%Y-%m-%d %H-%M-%S')} {agent}"
    folder, count = name, 1
    while folder.exists():  # a session ended and another started within the second: never write into it
        count += 1
        folder = name.with_name(f"{name.name} ({count})")
    (folder / "steps").mkdir(parents=True)
    _session.update(folder=folder, root=folder.parent)
    _write_json(folder / "session.json", {
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "ended": "", "verdict": "", "note": "",
        "agent": {"provider": provider, "model": model},
        "app": "ACMS (Agentic Context-Aware Mobility System)",
        "version": _version(), "code_sha256": _code_hash(), "python": sys.version.split()[0],
        "pyside6": PySide6.__version__, "os": platform.platform(),
        "about": "Each step: steps/NNNN. steps.csv tabulates them; report.html shows them. API keys are never recorded."})
    _refresh_panel()
    return folder


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=1, default=str), encoding="utf-8")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _files_in(value) -> list[str]:
    """The files a tool result names (its workbooks, their GeoJSON), that exist."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "files" and isinstance(item, list):
                found += [name for name in item if isinstance(name, str)]
            elif isinstance(item, (dict, list)):
                found += _files_in(item)
            elif key in ("file", "workbook") and isinstance(item, str):
                found.append(item)
    elif isinstance(value, list):
        for item in value:
            found += _files_in(item)
    paths = []
    for name in found:
        for path in (Path(name), Path(name).with_suffix(".geojson")):
            if path.is_file() and str(path) not in paths:
                paths.append(str(path))
    return paths


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def log(event: dict) -> None:
    """One AI Mode step (MainWindow._research_step): written to its folder, a row of steps.csv, the report."""
    folder = _session["folder"] or _start(event.get("results_folder") or ".", event.get("provider"), event.get("model"))
    number = len(steps(folder)) + 1
    here = folder / "steps" / f"{number:04d}"
    here.mkdir(parents=True, exist_ok=True)
    calls = event.get("calls", [])
    understood = {"plan": event.get("plan") or "", "tools_offered": event.get("tools_offered") or []}
    (here / "prompt.md").write_text(f"# Step {number}: your prompt\n\n{event.get('prompt', '')}\n\n"
                                    f"## Sent with it (the editor update)\n\n{event.get('editor_update', '').strip()}\n",
                                    encoding="utf-8")
    _write_json(here / "understood.json", understood)
    plans = [call["plan"] for call in calls if call.get("plan")]
    if plans:
        (here / "plan.md").write_text("\n\n".join(f"## {call.get('what', call['tool'])}\n\n{call['plan']}"
                                                  for call in calls if call.get("plan")), encoding="utf-8")
    with open(here / "calls.jsonl", "w", encoding="utf-8") as file:
        for call in calls:
            file.write(json.dumps(call, default=str) + "\n")
    (here / "reply.md").write_text(event.get("reply") or "", encoding="utf-8")
    _write_json(here / "messages.json", event.get("messages", []))
    changes = event.get("changes", [])
    (here / "changes.md").write_text("\n".join(f"- {line}" for line in changes) or "No change to the project.",
                                     encoding="utf-8")
    for key, name in (("map_png", "map.png"), ("demand_png", "demand.png")):
        if event.get(key):
            (here / name).write_bytes(event[key])
    artifacts = [{"path": path, "bytes": os.path.getsize(path), "sha256": _sha256(path)}
                 for call in calls for path in _files_in(call.get("result"))]
    _write_json(here / "artifacts.json", artifacts)
    activity = event.get("activity") or []  # Watch my edits: what the user did since the last step
    if activity:
        with open(here / "activity.csv", "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(["Time", "Seconds", "Kind", "Where", "What", "Value"])
            writer.writerows(activity)
    usage = event.get("usage", [])
    decided = [c for c in calls if c.get("proposed_at") and c.get("decided_at")]
    _write_json(here / "step.json", {
        "step": number, "kind": event.get("kind"), "started": event.get("started"), "ended": event.get("ended"),
        "provider": event.get("provider"), "model": event.get("model"),
        "prompt": event.get("prompt"), "understood": understood, "network": event.get("network"),
        "calls": [{k: v for k, v in c.items() if k != "result"} for c in calls], "changes": changes,
        "artifacts": artifacts, "error": event.get("error"), "activity_lines": len(activity),
        "timings_s": {"model": round(sum(u.get("total_s") or 0 for u in usage), 3),
                      "your_decisions": round(sum(c["decided_at"] - c["proposed_at"] for c in decided), 3),
                      "compute": round(sum((c.get("compute_s") or 0) + (c.get("check_s") or 0) for c in calls), 3),
                      "total": round((event.get("ended") or 0) - (event.get("started") or 0), 3)},
        "tokens": {"input": sum(u.get("input_tokens") or 0 for u in usage),
                   "output": sum(u.get("output_tokens") or 0 for u in usage), "requests": len(usage)},
        "verdict": "", "note": ""})
    _write_tables(folder)
    _refresh_panel()


# --- Reading a session back: everything comes from its folder ------------------------------------------------

def sessions(root) -> list[Path]:
    """The research log sessions in a results folder, newest first."""
    root = Path(root)
    found = [p for p in root.glob(f"{PREFIX}*") if (p / "session.json").is_file()] if root.is_dir() else []
    return sorted(found, key=lambda p: p.name, reverse=True)


def session_info(folder) -> dict:
    """A session's session.json (started, ended, verdict, note, the app's version…)."""
    return {"ended": "", "verdict": "", "note": "", **_read_json(Path(folder) / "session.json")}


def steps(folder) -> list[dict]:
    """A session's steps (chats) in order, each its step.json with its folder, reply and its pictures
    ([(file, caption)]: the map and/or the demand after it)."""
    found = []
    for here in sorted((Path(folder) / "steps").glob("[0-9]*")):
        record = _read_json(here / "step.json")
        if not record:
            continue
        try:
            reply = (here / "reply.md").read_text(encoding="utf-8")
        except OSError:
            reply = ""
        record.setdefault("verdict", "")
        record.setdefault("note", "")
        found.append({**record, "folder": f"steps/{here.name}", "reply": reply,
                      "pictures": [(name, caption) for name, caption in PICTURES if (here / name).is_file()]})
    return found


def _understood(step: dict) -> str:
    """What the model understood: its Plan: line (an older session's form, as written then)."""
    understood = step.get("understood") or {}
    if understood.get("plan"):
        return understood["plan"]
    return json.dumps(understood["form"], default=str) if understood.get("form") else ""


def _row(step: dict) -> list:
    calls, understood = step.get("calls") or [], step.get("understood") or {}
    timings, tokens = step.get("timings_s") or {}, step.get("tokens") or {}

    def count(word):
        return sum(1 for c in calls if str(c.get("decision", "")).startswith(word))

    stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""  # noqa: E731
    return [step["step"], step.get("kind"), stamp(step.get("started")), stamp(step.get("ended")), step.get("provider"),
            step.get("model"), step.get("prompt"), _understood(step), len(understood.get("tools_offered") or []) or "",
            ", ".join(c.get("tool", "") for c in calls), count("applied"), count("skipped"), count("refused"),
            count("read"), len(step.get("changes") or []), timings.get("model"), timings.get("your_decisions"),
            timings.get("compute"), timings.get("total"), tokens.get("input"), tokens.get("output"),
            tokens.get("requests"), step.get("error"), step["verdict"], step["note"], step["reply"][:500], step["folder"]]


def _write_tables(folder: Path) -> None:
    """steps.csv and report.html of a session, from its folder (a verdict rewrites them), and the results
    folder's sessions.csv."""
    folder = Path(folder)
    every, info = steps(folder), session_info(folder)
    with open(folder / "steps.csv", "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(COLUMNS)
        writer.writerows(_row(step) for step in every)
    e = html.escape
    parts = []
    for step in every:
        understood = step.get("understood") or {}
        calls = "".join(f"<li><b>{e(c.get('tool', ''))}</b> {e(c.get('what') or '')} <i>({e(str(c.get('decision', '')))})</i>"
                        + (f"<pre>{e(c['plan'])}</pre>" if c.get("plan") else "") + "</li>" for c in step.get("calls") or [])
        changes = "".join(f"<li>{e(line)}</li>" for line in step.get("changes") or []) \
            or "<li>No change to the project.</li>"
        t, k = step.get("timings_s") or {}, step.get("tokens") or {}
        parts.append(
            f"<section><h2>Step {step['step']} <small>{e(str(step.get('kind')))} · {e(str(step.get('model')))}"
            f"</small></h2>"
            f"<h3>1. Prompt</h3><blockquote>{e(step.get('prompt') or '')}</blockquote>"
            f"<h3>2. Understood</h3><p>{e(_understood(step) or '(no Plan: line)')}"
            + (f" <small>({len(understood['tools_offered'])} tools offered)</small>" if understood.get("tools_offered") else "")
            + "</p>"
            + f"<h3>3. Did</h3><ul>{calls or '<li>No tool used.</li>'}</ul>"
            + (f"<p><b>Reply:</b></p><pre>{e(step['reply'])}</pre>" if step["reply"] else "")
            + (f"<p class=error>Error: {e(str(step['error']))}</p>" if step.get("error") else "")
            + f"<h3>4. Snapshot</h3><ul>{changes}</ul>"
            + "".join(f"<p>{caption}</p><img src='{step['folder']}/{name}' alt='{caption} step {step['step']}'>"
                      for name, caption in step["pictures"])
            + f"<p class=numbers>Model {t.get('model')} s · your decisions {t.get('your_decisions')} s · compute "
              f"{t.get('compute')} s · total {t.get('total')} s · tokens {k.get('input')} in, {k.get('output')} out "
              f"({k.get('requests')} requests)"
            + (f" · <b>verdict: {e(step['verdict'])}</b>" if step["verdict"] else "")
            + (f" · note: {e(step['note'])}" if step["note"] else "") + "</p></section>")
    session = (f" · ended {e(info['ended'])}" if info["ended"] else " · going on") \
        + (f" · <b>session verdict: {e(info['verdict'])}</b>" if info["verdict"] else "") \
        + (f" · note: {e(info['note'])}" if info["note"] else "")
    style = ("body{font-family:Segoe UI,system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 16px;"
             "color:#1f2328;background:#fff}section{border-top:1px solid #d0d7de;padding:1em 0}"
             "img{max-width:100%;border:1px solid #d0d7de}pre{white-space:pre-wrap;background:#f6f8fa;padding:.6em}"
             "blockquote{margin:0;padding:.4em .8em;border-left:4px solid #8c959f}.numbers{color:#57606a}"
             ".error{color:#a40e26}@media (prefers-color-scheme:dark){body{color:#e6edf3;background:#0d1117}"
             "pre{background:#161b22}.numbers{color:#8d96a0}.error{color:#ff7b72}}")
    (folder / "report.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>ACMS research log</title><style>{style}</style>"
        f"<h1>ACMS research log</h1><p>{e(folder.name)} · {len(every)} steps{session}</p>"
        + "".join(parts), encoding="utf-8")
    if info["ended"]:
        _write_document(folder, every, info)
    _write_sessions(folder.parent)


DOC_RESULT_CHARS = 4000  # ponytail: a tool result longer than this is cut in the document; calls.jsonl keeps it whole


def _write_document(folder: Path, every: list[dict], info: dict) -> Path:
    """The ended session as one document to read like a human, `<session folder name>.odt` (Word and LibreOffice
    open it): the session's facts and verdict, then each step's prompt with the editor update sent with it, what was
    understood, the plan, every tool call with its arguments, decision and result, the model's messages, the reply,
    the changes, the map or demand after, the files it wrote, timings, tokens and your verdict."""
    from PySide6.QtGui import QImage, QTextDocument, QTextDocumentWriter

    e = html.escape

    def block(value) -> str:
        text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
        if len(text) > DOC_RESULT_CHARS:
            text = text[:DOC_RESULT_CHARS] + "\n… (cut here: the whole of it is in the step's files)"
        return f"<pre>{e(text)}</pre>"

    def read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    document = QTextDocument()
    agent = info.get("agent") if isinstance(info.get("agent"), dict) else {}
    info = {**info, "agent": " ".join(str(part) for part in (agent.get("provider"), agent.get("model")) if part)}
    facts = "".join(f"<tr><td><b>{e(name)}</b></td><td>{e(str(info.get(key) or ''))}</td></tr>" for name, key in (
        ("Started", "started"), ("Ended", "ended"), ("Agent", "agent"), ("App", "app"), ("Version", "version"),
        ("Code SHA-256", "code_sha256"), ("Python", "python"), ("PySide6", "pyside6"), ("OS", "os"),
        ("Your verdict", "verdict"), ("Your note", "note")))
    parts = [f"<h1>ACMS research log</h1><p>{e(folder.name)} · {len(every)} steps</p><table>{facts}</table>"]
    for step in every:
        here = folder / step["folder"]
        understood, t, k = step.get("understood") or {}, step.get("timings_s") or {}, step.get("tokens") or {}
        stamp = lambda s: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s)) if s else ""  # noqa: E731
        calls = [json.loads(line) for line in read(here / "calls.jsonl").splitlines() if line.strip()]
        update = read(here / "prompt.md").partition("## Sent with it (the editor update)")[2].strip()
        parts.append(f"<h2>Step {step['step']}: {e(str(step.get('kind') or ''))}</h2>"
                     f"<p>{e(stamp(step.get('started')))} to {e(stamp(step.get('ended')))} · {e(str(step.get('provider')))} "
                     f"{e(str(step.get('model')))}</p>"
                     f"<h3>Your prompt</h3><p>{e(step.get('prompt') or '')}</p>"
                     + (f"<h3>Sent with it (the editor update)</h3>{block(update)}" if update else ""))
        if _understood(step):
            parts.append(f"<h3>What it understood</h3>{block(_understood(step))}")
        if (here / "plan.md").is_file():
            parts.append(f"<h3>The plan you approved</h3>{block(read(here / 'plan.md'))}")
        parts.append("<h3>What it did</h3>" + ("".join(
            f"<h4>{e(call.get('tool', ''))}: {e(str(call.get('what') or ''))}</h4><p>Decision: {e(str(call.get('decision', '')))}</p>"
            f"<p>Arguments:</p>{block(call.get('arguments'))}" + (f"<p>Result:</p>{block(call['result'])}" if 'result' in call else "")
            for call in calls) or "<p>No tool used.</p>"))
        messages = json.loads(read(here / "messages.json") or "[]")
        if messages:
            parts.append("<h3>The model's messages</h3>" + "".join(
                f"<p><b>{e(str(m.get('role', '')))}{' (' + e(str(m['name'])) + ')' if m.get('name') else ''}</b></p>"
                + (block(m["content"]) if m.get("content") else "")
                + (block(m["tool_calls"]) if m.get("tool_calls") else "") for m in messages))
        if step["reply"]:
            parts.append(f"<h3>Its reply</h3>{block(step['reply'])}")
        if step.get("error"):
            parts.append(f"<p><b>Error:</b> {e(str(step['error']))}</p>")
        parts.append("<h3>What changed</h3><ul>" + ("".join(f"<li>{e(line)}</li>" for line in step.get("changes") or [])
                                                     or "<li>No change to the project.</li>") + "</ul>")
        for name, caption in step["pictures"]:
            image = QImage(str(here / name))
            document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(f"{step['folder']}/{name}"), image)
            parts.append(f"<h3>{caption}</h3><p><img src='{step['folder']}/{name}' width='{min(image.width(), 600)}'></p>")
        if step.get("artifacts"):
            parts.append("<h3>Files it wrote</h3><ul>" + "".join(
                f"<li>{e(a['path'])} ({a['bytes']} bytes, SHA-256 {e(a['sha256'])})</li>" for a in step["artifacts"]) + "</ul>")
        parts.append(f"<h3>Numbers</h3><p>Model {t.get('model')} s · your decisions {t.get('your_decisions')} s · compute "
                     f"{t.get('compute')} s · total {t.get('total')} s · tokens {k.get('input')} in, {k.get('output')} out "
                     f"({k.get('requests')} requests)</p>"
                     f"<p><b>Your verdict:</b> {e(step['verdict'] or 'not marked')}"
                     + (f" · note: {e(step['note'])}" if step["note"] else "") + "</p>")
    document.setHtml("".join(parts))
    path = folder / f"{folder.name}.odt"
    QTextDocumentWriter(str(path), b"ODF").write(document)
    return path


def _write_sessions(root: Path) -> None:
    """sessions.csv in the results folder: one row a session, oldest first."""
    rows = []
    for folder in reversed(sessions(root)):
        info, every = session_info(folder), steps(folder)
        verdicts = [step["verdict"] for step in every]
        rows.append([folder.name[len(PREFIX):], info.get("started", ""), info["ended"], len(every),
                     *(verdicts.count(v) for v in VERDICTS), verdicts.count(""),
                     ", ".join(sorted({str(step.get("model")) for step in every})), info["verdict"], info["note"],
                     folder.name])
    with open(root / "sessions.csv", "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(SESSION_COLUMNS)
        writer.writerows(rows)


# --- Your verdicts ------------------------------------------------------------------------------------------

def mark(verdict: str, note: str = "", step: int | None = None, folder=None) -> bool:
    """Your verdict on a chat (one of VERDICTS), with a note: into its step.json, steps.csv and the report.
    By default the latest step of the session being recorded. False if there is no such step."""
    folder = Path(folder) if folder else _session["folder"]
    if verdict not in VERDICTS or folder is None:
        return False
    every = steps(folder)
    chosen = every[-1] if every and step is None else next((s for s in every if s["step"] == step), None)
    if chosen is None:
        return False
    path = folder / chosen["folder"] / "step.json"
    _write_json(path, {**_read_json(path), "verdict": verdict, "note": note.strip()})
    _write_tables(folder)
    _refresh_panel()
    return True


def rank_session(verdict: str, note: str = "", folder=None) -> bool:
    """Your verdict on a whole session, with a note: into its session.json, its report and sessions.csv."""
    folder = Path(folder) if folder else _session["folder"]
    if verdict not in VERDICTS or folder is None or not (folder / "session.json").is_file():
        return False
    _write_json(folder / "session.json", {**session_info(folder), "verdict": verdict, "note": note.strip()})
    _write_tables(folder)
    return True


def end_session() -> Path | None:
    """End the session being recorded: its end time into session.json; the next question starts another.
    Returns its folder (None if none was going on)."""
    folder = _session["folder"]
    if folder is None:
        return None
    _write_json(folder / "session.json", {**session_info(folder), "ended": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _write_tables(folder)
    _session["folder"] = None
    _refresh_panel()
    return folder


def _results_root() -> Path:
    """Where the sessions are: this session's results folder, else the window's, else Documents\\ACMS results."""
    if _session["root"]:
        return Path(_session["root"])
    window = next((w for w in QApplication.topLevelWidgets() if hasattr(w, "results_folder")), None)
    if window is not None:
        return Path(window.results_folder)
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)) / "ACMS results"


# --- The panel and History --------------------------------------------------------------------------------

def _short(text, width: int = 70) -> str:
    text = str(text or "").replace("\n", " ")
    return text[:width] + ("…" if len(text) > width else "")


def _refresh_panel() -> None:
    if not _ui:
        return
    try:
        folder = _session["folder"]
        _ui["where"].setText(f"Recording to {folder.name}" if folder else "Records from your next AI Mode question.")
        every = steps(folder) if folder else []
        if every:
            last = every[-1]
            _ui["last"].setText(f"Step {last['step']}: {_short(last.get('prompt'))}"
                                + (f" · {last['verdict']}" if last["verdict"] else ""))
        else:
            _ui["last"].setText("No step yet.")
    except RuntimeError:  # the panel was closed (its widgets deleted)
        _ui.clear()


class History(QDialog):
    """Every session in the results folder: pick one, rank each of its chats and the session itself."""

    def __init__(self, parent=None, root=None, folder=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Research Log history")
        self.resize(1000, 640)
        self.root = Path(root) if root else _results_root()
        layout = QVBoxLayout(self)
        self.sessions = QComboBox()
        self.sessions.setToolTip("Session")
        layout.addWidget(self.sessions)
        self.session_note = QLineEdit()
        self.session_note.setPlaceholderText("Note on the session (optional)")
        self.session_note.setToolTip("Note on the session")
        ranks = QHBoxLayout()
        ranks.addWidget(hint_label("Session:"))
        ranks.addWidget(self.session_note, 1)
        self.session_buttons = {}
        for glyph, name, verdict in _VERDICT_BUTTONS:
            button = icon_button(glyph, f"Session {name[0].lower()}{name[1:]}")
            button.clicked.connect(lambda _=False, verdict=verdict: self._rank_session(verdict))
            ranks.addWidget(button)
            self.session_buttons[verdict] = button
        report = icon_button(0xE8A5, "Open report")
        report.clicked.connect(lambda: self.folder() and QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.folder() / "report.html"))))
        ranks.addWidget(report)
        layout.addLayout(ranks)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.chats = QListWidget()
        self.chats.setToolTip("Chats")
        self.chats.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # long prompts end in …
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        split.addWidget(self.chats)
        split.addWidget(self.detail)
        split.setSizes([360, 640])
        layout.addWidget(split, 1)
        self.chat_note = QLineEdit()
        self.chat_note.setPlaceholderText("Note on the chat (optional)")
        self.chat_note.setToolTip("Note on the chat")
        marks = QHBoxLayout()
        marks.addWidget(hint_label("Chat:"))
        marks.addWidget(self.chat_note, 1)
        self.chat_buttons = {}
        for glyph, name, verdict in _VERDICT_BUTTONS:
            button = icon_button(glyph, name)
            button.clicked.connect(lambda _=False, verdict=verdict: self._mark(verdict))
            marks.addWidget(button)
            self.chat_buttons[verdict] = button
        layout.addLayout(marks)
        self.status = hint_label("")
        layout.addWidget(self.status)
        self.sessions.currentIndexChanged.connect(self._show_session)
        self.chats.currentRowChanged.connect(self._show_chat)
        self._fill_sessions(Path(folder) if folder else None)

    def folder(self) -> Path | None:
        data = self.sessions.currentData()
        return Path(data) if data else None

    def _fill_sessions(self, chosen: Path | None = None) -> None:
        self.sessions.blockSignals(True)
        self.sessions.clear()
        for folder in sessions(self.root):
            info, count = session_info(folder), len(steps(folder))
            state = f"ended {info['ended'][11:16]}" if info["ended"] else "going on"
            self.sessions.addItem(f"{folder.name[len(PREFIX):]} · {count} chat{'' if count == 1 else 's'} · {state}"
                                  + (f" · {info['verdict']}" if info["verdict"] else ""), str(folder))
        index = self.sessions.findData(str(chosen)) if chosen else 0
        self.sessions.setCurrentIndex(max(index, 0))
        self.sessions.blockSignals(False)
        self._show_session()

    def _show_session(self) -> None:
        folder = self.folder()
        self.chats.clear()
        if folder is None:
            self.status.setText(f"No research log sessions in {self.root} yet.")
            self.detail.clear()
            return
        info = session_info(folder)
        self.session_note.setText(info["note"])
        for step in steps(folder):
            self.chats.addItem(QListWidgetItem(self._chat_text(step)))
        self.status.setText(f"{folder.name}: " + (f"ranked {info['verdict']}" if info["verdict"] else "not ranked yet"))
        if self.chats.count():
            self.chats.setCurrentRow(0)
        else:
            self.detail.setPlainText("No chats in this session.")

    @staticmethod
    def _chat_text(step: dict) -> str:
        return f"{step['step']}. " + (f"[{step['verdict']}] " if step["verdict"] else "") + _short(step.get("prompt"), 90)

    def _step(self) -> dict | None:
        folder, row = self.folder(), self.chats.currentRow()
        every = steps(folder) if folder else []
        return every[row] if 0 <= row < len(every) else None

    def _show_chat(self) -> None:
        step, folder = self._step(), self.folder()
        if step is None:
            return
        e = html.escape
        understood = _understood(step)
        calls = "".join(f"<li><b>{e(c.get('tool', ''))}</b> {e(c.get('what') or '')} ({e(str(c.get('decision', '')))})</li>"
                        for c in step.get("calls") or []) or "<li>No tool used.</li>"
        changes = "".join(f"<li>{e(line)}</li>" for line in step.get("changes") or []) or "<li>No change.</li>"
        self.detail.setHtml(
            f"<h3>Step {step['step']} · {e(str(step.get('model')))}</h3>"
            f"<p><b>Prompt:</b> {e(step.get('prompt') or '')}</p>"
            + (f"<p><b>Understood:</b> {e(understood)}</p>" if understood else "")
            + f"<p><b>Did:</b></p><ul>{calls}</ul>"
            + (f"<p><b>Reply:</b></p><p>{e(step['reply']).replace(chr(10), '<br>')}</p>" if step["reply"] else "")
            + (f"<p><b>Error:</b> {e(str(step['error']))}</p>" if step.get("error") else "")
            + f"<p><b>Changes:</b></p><ul>{changes}</ul>"
            + "".join(f"<p><b>{caption}:</b></p><p><img src='{QUrl.fromLocalFile(str(folder / step['folder'] / name)).toString()}' "
                      f"width=420></p>" for name, caption in step["pictures"])
            + (f"<p><b>Verdict:</b> {e(step['verdict'])}" + (f" · {e(step['note'])}" if step["note"] else "") + "</p>"
               if step["verdict"] else ""))
        self.chat_note.setText(step["note"])

    def _mark(self, verdict: str) -> None:
        step, row = self._step(), self.chats.currentRow()
        if step is None or not mark(verdict, self.chat_note.text(), step=step["step"], folder=self.folder()):
            return
        self.chats.item(row).setText(self._chat_text(steps(self.folder())[row]))
        self._show_chat()
        self.status.setText(f"Chat {step['step']}: {verdict}.")

    def _rank_session(self, verdict: str) -> None:
        folder = self.folder()
        if folder is not None and rank_session(verdict, self.session_note.text(), folder=folder):
            row = self.chats.currentRow()
            self._fill_sessions(folder)
            self.chats.setCurrentRow(max(row, 0))
            self.status.setText(f"Session ranked {verdict}.")


def show_history(folder=None) -> History:
    try:
        parent = _ui["widget"].window() if _ui.get("widget") else None
    except RuntimeError:  # the panel was closed
        parent = None
    dialog = History(parent, folder=folder)
    dialog.show()
    _ui["history"] = dialog  # kept alive while open
    return dialog


def _end_and_rank() -> None:
    folder = end_session()
    show_history(folder)


def panel() -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    _ui["widget"] = widget
    _ui["where"], _ui["last"] = hint_label(""), hint_label("")
    note = QLineEdit()
    note.setPlaceholderText("Note on the latest step (optional)")
    note.setToolTip("Note on the step")
    _ui["note"] = note
    row = QHBoxLayout()
    for glyph, name, verdict in _VERDICT_BUTTONS:
        button = icon_button(glyph, name)
        button.clicked.connect(lambda _=False, verdict=verdict: mark(verdict, note.text()) and note.clear())
        row.addWidget(button)
    end = icon_button(0xE71A, "End session")
    end.clicked.connect(_end_and_rank)
    history = icon_button(0xE81C, "History")
    history.clicked.connect(lambda: show_history(_session["folder"]))
    opener = icon_button(0xE838, "Open log folder")
    opener.clicked.connect(lambda: QDesktopServices.openUrl(
        QUrl.fromLocalFile(str(_session["folder"] or _results_root()))))
    for button in (end, history, opener):
        row.addWidget(button)
    row.addStretch(1)
    for item in (_ui["where"], _ui["last"]):
        layout.addWidget(item)
    layout.addWidget(note)
    layout.addLayout(row)
    _refresh_panel()
    return widget
