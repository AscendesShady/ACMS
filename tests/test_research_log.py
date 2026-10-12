"""The Research Log extension and the log() hook: each AI Mode step (prompt, what was understood, what it did,
the change list and the map after) written to a session folder, steps.csv and report.html; the verdict; nothing
recorded while unticked; a failing log() switched off. Against the real AI Mode panel and a fake Ollama.
Run with `python tests/test_research_log.py` (or the test monitor)."""

import csv
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])


class _Ollama(BaseHTTPRequestHandler):
    """A model with tools: asked to build, it plans in one line and calls build_scenario, then answers from the
    result; anything else it answers "ok"."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.end_headers()
        if self.path == "/api/show":
            self.wfile.write(json.dumps({"capabilities": ["completion", "tools"]}).encode())
            return
        last = body["messages"][-1]
        message = {"role": "assistant", "content": "ok"}
        if last["role"] == "user" and "Build me" in last["content"]:
            message = {"role": "assistant", "content": "Plan: build a 3x2 grid and run it 5 minutes with build_scenario.",
                       "tool_calls": [{"function": {"name": "build_scenario",
                                                    "arguments": {"intent": "build", "grid": "3x2", "minutes": 5}}}]}
        elif last["role"] == "tool":
            message = {"role": "assistant", "content": "Built and run."}
        for event in ({"message": message, "done": False},
                      {"message": {"content": ""}, "done": True, "prompt_eval_count": 40, "eval_count": 6}):
            self.wfile.write((json.dumps(event) + "\n").encode())

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"models": [{"name": "small:3b"}]}).encode())

    def log_message(self, *args):
        pass


def test_each_step_is_recorded_for_research():
    from traffic_simulator.main import MainWindow

    server = HTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    before = os.environ.get("OLLAMA_HOST")
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{server.server_address[1]}"
    window = MainWindow()
    window.results_folder = Path(tempfile.mkdtemp())
    window._ask_to_save = False
    panel = window.ai_mode
    extension = window.extensions.find("Research Log")
    log = extension.module

    def settle(condition, limit=120.0):
        start = time.time()
        while not condition() and time.time() - start < limit:
            APP.processEvents()
            time.sleep(0.01)
        assert condition(), "timed out"

    def idle():
        return panel.prompt.isEnabled() and not panel._running

    def ask(text):
        panel.prompt.setText(text)
        panel.send()

    try:
        assert "log" in [hook for hook in ("draw", "panel", "log") if extension.has(hook)]
        extension.check.setChecked(True)
        panel.rescan()
        panel.local_models.setCurrentText("small:3b")
        panel.provider.setCurrentIndex(panel.provider.findData("local"))
        panel.provider.activated.emit(panel.provider.currentIndex())
        panel.start_button.click()
        settle(idle)

        window.watch_action.setChecked(True)  # Watch my edits: what the user did before the step goes with it
        window.activity.log("entry", "Seed", "7")
        ask("Build me a 3x2 grid, run it 5 minutes")
        settle(lambda: not panel.proposal.isHidden())
        panel.apply_button.click()
        settle(idle)
        window.watch_action.setChecked(False)
        folders = list(window.results_folder.glob("ACMS research log *"))
        assert len(folders) == 1
        session, step = folders[0], folders[0] / "steps" / "0001"
        meta = json.loads((session / "session.json").read_text(encoding="utf-8"))
        assert len(meta["code_sha256"]) == 64 and meta["python"] and meta["agent"] == {"provider": "local", "model": "small:3b"}
        assert re.fullmatch(r"ACMS research log \d{4}-\d\d-\d\d \d\d-\d\d-\d\d Ollama small-3b", session.name), session.name
        assert log.agent_name("gemini", "gemini-2.5-flash") == "Gemini gemini-2.5-flash" and log.agent_name(None, None) == "no agent"
        assert "Build me a 3x2 grid" in (step / "prompt.md").read_text(encoding="utf-8")  # 1. the prompt
        with open(step / "activity.csv", encoding="utf-8-sig") as file:  # and what the user did before it
            done = list(csv.DictReader(file))
        assert any(row["Kind"] == "entry" and row["What"] == "Seed" and row["Value"] == "7" for row in done)
        understood = json.loads((step / "understood.json").read_text(encoding="utf-8"))  # 2. what it understood
        assert understood["plan"] == "build a 3x2 grid and run it 5 minutes with build_scenario."  # its own Plan: line
        assert "build_scenario" in understood["tools_offered"] and "find_tools" in understood["tools_offered"]
        calls = [json.loads(line) for line in (step / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        build = next(c for c in calls if c["tool"] == "build_scenario")  # 3. what it did, and your decision
        assert build["decision"] == "applied" and build["decided_at"] >= build["proposed_at"] and "report" in build["result"]
        assert "Build a 3 x 2 grid" in (step / "plan.md").read_text(encoding="utf-8")
        changes = (step / "changes.md").read_text(encoding="utf-8")  # 4. the snapshot: changes and the map
        assert "a 3 x 2 grid (3 columns, 2 rows)" in changes and "Demand 1W->1E: hourly none" in changes
        assert (step / "map.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"  # the network changed: the map
        assert (step / "demand.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"  # and the demand: the OD matrix
        artifacts = json.loads((step / "artifacts.json").read_text(encoding="utf-8"))
        workbook = next(a for a in artifacts if a["path"].endswith(".xlsx"))
        assert workbook["sha256"] == hashlib.sha256(Path(workbook["path"]).read_bytes()).hexdigest()
        record = json.loads((step / "step.json").read_text(encoding="utf-8"))
        assert "guided" not in record and record["tokens"]["requests"] >= 1 and record["timings_s"]["total"] > 0

        ask("what is a travel time index?")
        settle(idle)
        assert log.mark("as intended", "clear answer")  # 5. your verdict, on the latest step
        with open(session / "steps.csv", encoding="utf-8-sig") as file:
            rows = list(csv.DictReader(file))
        assert [row["Step"] for row in rows] == ["1", "2"] and rows[0]["Applied"] == "1" and rows[0]["Plan"].startswith("build a 3x2")
        assert int(rows[0]["Tools offered"]) > 5 and "Guided" not in rows[0]
        assert rows[1]["Tools"] == "" and rows[1]["Reply"] == "ok" and rows[1]["Verdict"] == "as intended"
        assert rows[1]["Note"] == "clear answer"
        assert json.loads((session / "steps" / "0002" / "step.json").read_text(encoding="utf-8"))["verdict"] == "as intended"
        assert not list((session / "steps" / "0002").glob("*.png"))  # nothing changed: no picture
        report = (session / "report.html").read_text(encoding="utf-8")
        assert "steps/0001/map.png" in report and "verdict: as intended" in report and "Build me a 3x2 grid" in report

        # 6. End session, then History: any chat ranked, and the session itself
        assert log.mark("not as intended", "wrong grid", step=1)  # an earlier chat, by its number
        assert json.loads((step / "step.json").read_text(encoding="utf-8"))["note"] == "wrong grid"
        assert not log.mark("partly", step=9) and not log.rank_session("great", folder=session)  # no such chat, verdict
        assert log.end_session() == session and log._session["folder"] is None
        assert json.loads((session / "session.json").read_text(encoding="utf-8"))["ended"]
        with zipfile.ZipFile(session / f"{session.name}.odt") as document:  # ended: one document to read
            content = document.read("content.xml").decode("utf-8")
            assert "Build me a 3x2 grid" in content and "wrong grid" in content and "What it did" in content
            assert any(name.startswith("Pictures/") for name in document.namelist())  # the map after, inside
        history = log.History(window, root=window.results_folder, folder=session)
        assert history.sessions.count() == 1 and "2 chats · ended" in history.sessions.currentText()
        assert history.chats.count() == 2 and "[not as intended]" in history.chats.item(0).text()
        history.chats.setCurrentRow(0)
        assert "Build me a 3x2 grid" in history.detail.toPlainText() and history.chat_note.text() == "wrong grid"
        history.chat_note.setText("grid right, run short")
        history.chat_buttons["partly"].click()
        assert log.steps(session)[0]["verdict"] == "partly" and "[partly]" in history.chats.item(0).text()
        history.session_note.setText("good for a first try")
        history.session_buttons["as intended"].click()
        meta = log.session_info(session)
        assert (meta["verdict"], meta["note"]) == ("as intended", "good for a first try")
        assert "as intended" in history.sessions.currentText() and history.chats.count() == 2
        with open(window.results_folder / "sessions.csv", encoding="utf-8-sig") as file:
            sessions = list(csv.DictReader(file))
        assert len(sessions) == 1 and sessions[0]["Steps"] == "2" and sessions[0]["Partly"] == "1"
        assert sessions[0]["As intended"] == "1" and sessions[0]["Verdict"] == "as intended" and sessions[0]["Ended"]
        assert "session verdict: as intended" in (session / "report.html").read_text(encoding="utf-8")
        history.close()
        ask("what is a level of service?")  # the next question starts another session
        settle(idle)
        assert len(log.sessions(window.results_folder)) == 2 and log._session["folder"] != session
        session = log._session["folder"]
        assert [s["step"] for s in log.steps(session)] == [1]

        extension.check.setChecked(False)  # unticked: nothing recorded
        ask("what is a planning time index?")
        settle(idle)
        assert not (session / "steps" / "0002").exists()

        extension.check.setChecked(True)
        log.log = lambda event: 1 / 0  # a log() that raises is switched off; the chat carries on
        ask("what is a gamma index?")
        settle(idle)
        assert not extension.on and "Switched off" in extension.error and panel.history[-1]["content"] == "ok"
    finally:
        window.close()
        server.shutdown()
        if before is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = before


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
