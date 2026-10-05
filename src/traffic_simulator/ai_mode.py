"""AI Mode tab: System 1's fast typed answers and System 2's chat about the app.

Both take their keys only from this PC's environment variables (JEV_API_KEY for
System 1; GROK_API_KEY, GEMINI_API_KEY for System 2): the app scans for them, for laya.exe
(LAYA_EXE) and for a running Ollama, when it starts (Rescan PC looks again) and offers just
what it found. System 1 (Jev, or Laya started here) answers the signal agent's TRUE/FALSE
questions, else System 2's started model does. System 2 chats through assistant.py:
pick Grok, Gemini or a local Ollama model and it warms up on the app's documents; the prompt box
stays locked (the prompt gate) until the model is ready. Replies stream in, and the
model can look at the open network and propose changes (network_tools.py): reads run
at once, every change waits for Apply or Skip, and applied changes are undoable.
Each message carries an editor update (the user's changes since the last reply and the
map selection), the conversation is saved with the project, and watch mode can look
at the user's edits by itself.
Requests run on a background thread; tools always run here on the UI thread.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import time
from copy import deepcopy
from types import SimpleNamespace

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QProgressBar, QSizePolicy, QTextBrowser, QVBoxLayout, QWidget)

from traffic_simulator import assistant, network_tools, theme
from traffic_simulator.assistant import AssistantError, Cancelled, ToolsUnsupported, ollama_models  # noqa: F401
from traffic_simulator.properties_panel import OpenPageTabs, hint_label, icon_button, set_glyph, styled_button

SYSTEM_1 = {"jev": "JEV (JEV_API_KEY)", "laya": "Laya (local)"}  # what System 1 can use, when found
PROVIDERS = {"grok": "Grok (xAI)", "gemini": "Gemini (Google)", "local": "Local (Ollama)"}
WINDOWS_ENVIRONMENTS = (  # (root, path) as winreg names: user first, then system-wide
    ("HKEY_CURRENT_USER", "Environment"),
    ("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
)
READY = "Ask about the simulator or your network"
WATCH_PAUSE_MS = 2000  # watch mode looks once the user has paused editing this long
WATCH_PROMPT = ("(Watch mode) I just made the changes in the editor update. If something looks wrong "
                "or could work better for the network, say so briefly and propose a fix; otherwise "
                "reply with one short sentence. Don't repeat earlier advice.")


TOOLS_NOTE = "Available tools"  # the transcript's speaker for Available Tools' list (not saved)
START, RESTART = (0xE768, "Start"), (0xE72C, "Restart")


def scan_keys(names: list[str]) -> dict[str, str]:
    """The named keys set on this device: the app's own environment first, then on
    Windows the user and system environment settings, which also hold variables set
    after whatever launched the app (an editor, a terminal) had started."""
    found = {name: os.environ[name].strip() for name in names if os.environ.get(name, "").strip()}
    if sys.platform == "win32":
        import winreg

        for root, path in WINDOWS_ENVIRONMENTS:
            for name in names:
                if name in found:
                    continue
                try:
                    with winreg.OpenKey(getattr(winreg, root), path) as key:
                        value, kind = winreg.QueryValueEx(key, name)
                except OSError:  # not set there
                    continue
                if kind == winreg.REG_EXPAND_SZ:
                    value = winreg.ExpandEnvironmentStrings(value)
                if str(value).strip():
                    found[name] = str(value).strip()
    return found


def history_json(shown: list, record: list[dict], journal: list[dict]) -> str:
    """Export History as JSON: the chat as shown, the full record and the planning journal."""
    return json.dumps({"exported": time.strftime("%Y-%m-%d %H:%M:%S"), "chat": [{"speaker": who, "text": said}
                       for who, said in shown], "record": record, "journal": journal}, indent=1, default=str)


def history_markdown(shown: list, record: list[dict], journal: list[dict]) -> str:
    """Export History as Markdown: the chat as shown; then the record, what the assistant observed (each
    message's editor update, every tool call and what it returned) and its thinking where the model shared
    it; then the planning journal."""
    lines = ["# ACMS chat history", "", f"Exported {time.strftime('%Y-%m-%d %H:%M:%S')}.", "", "## Chat", ""]
    for who, said in shown:
        lines += [f"**{who}**", "", said, ""] if who else [f"*{said}*", ""]
    lines += ["## What the assistant observed and thought", ""]
    if not record:
        lines += ["*Nothing recorded this session (a conversation restored from a project keeps only its chat).*", ""]
    for message in record:
        stamp = f"{message.get('at', '')} · {message.get('model', '')}"
        if message["role"] == "user":
            lines += [f"### You ({stamp})", "", "```text", message["content"], "```", ""]
        elif message["role"] == "assistant":
            lines += [f"### Assistant ({stamp})", ""]
            if message.get("thinking"):
                lines += ["Thinking:", "", *(f"> {line}" for line in message["thinking"].splitlines()), ""]
            if message.get("content"):
                lines += [message["content"], ""]
            for call in message.get("tool_calls", []):
                lines += [f"Tool call **{call['name']}**:", "", "```json", json.dumps(call["arguments"], indent=1), "```", ""]
        elif message["role"] == "tool":
            lines += [f"Result of **{message.get('name', 'tool')}** (observed):", "", "```json", message["content"], "```", ""]
    lines += ["## Planning journal", ""]
    if journal:
        lines += ["| Time | Kind | Tool | What | Outcome |", "|---|---|---|---|---|"]
        lines += [f"| {e['time']} | {e['kind']} | {e['tool']} | {e['what']} | {e['outcome']} |".replace("\n", " ")
                  for e in journal]
    else:
        lines += ["*Empty.*"]
    return "\n".join(lines) + "\n"


def model_label(model: str) -> str:
    """A short name for a model: "llama3.1:8b" -> "Llama", "grok-4.3" -> "Grok"."""
    match = re.match(r"[A-Za-z]+", model)
    return match.group().capitalize() if match else model


class _ToolRequest:
    """A tool call waiting for the UI thread (and, for edits, the user)."""

    def __init__(self, call: dict) -> None:
        self.call, self.result, self.done = call, None, threading.Event()
        self.record: dict | None = None  # its line in the step's record (an edit waiting for the user's decision)


STOPPED = object()  # result of a request the user stopped


def _plain(html_text: str) -> str:
    """The approval card's text without its markup: lines for <br>, no tags."""
    return re.sub(r"<[^>]+>", "", html_text.replace("<br>", "\n")).strip()


class _Relay(QObject):
    """Carries a background request's events back to the UI thread."""

    done = Signal(int, str, object, object)  # session, job ("warm"/"answer"), result, error
    text = Signal(int, str, str)  # session, speaker, streamed text
    tool = Signal(int, object)  # session, _ToolRequest: answer it, then set its done event
    listed = Signal(int, str, object, object)  # session, provider, its chat models, error
    system_1 = Signal(str, object, object)  # System 1 choice, Laya's health, error


class AIModePanel(QWidget):
    """System 1 answers fast yes/no questions; System 2 chats with whatever this PC offers.
    Keys are found on start (see scan_keys) and live in memory for this session only, never
    shown or saved with the network, so the app moves between devices without them."""

    # What System 2 is doing, for the window's status bar: rich text ("" hides it) and a tooltip.
    indicator = Signal(str, str)
    status_line = "Off"  # the same in plain text, e.g. "Llama is watching (llama3.1:8b)"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._found: dict[str, str] = {}  # keys found in the environment by the last scan
        self._laya_exe = ""  # laya.exe found by the last scan
        self._system_1 = ""  # System 1's choice, kept apart from the widget for the agent's thread
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.systems = systems = OpenPageTabs()  # System 1's page no taller than it needs
        page = QWidget()
        form = QFormLayout(page)
        self.system_1_status = hint_label()  # what the scan found on this PC
        form.addRow(self.system_1_status)
        self.system_1 = QComboBox()
        self.system_1.addItem("Off", "")
        self.system_1.setToolTip("Detected System 1 options")
        self.system_1.activated.connect(self._choose_system_1)
        form.addRow("Use", self.system_1)
        self.system_1_state = hint_label()  # what the chosen one is doing (Laya starting, ready, failed)
        form.addRow(self.system_1_state)
        form.addRow(hint_label("System 1 answers the signal agent's TRUE/FALSE questions in one pass, with no "
                               "text generated; while it is Off, System 2's started model answers them."))
        systems.addTab(page, "System 1")
        page = QWidget()
        form = QFormLayout(page)
        self.found_status = hint_label()  # what the scan found on this PC
        form.addRow(self.found_status)
        self._build_chat(form)
        systems.addTab(page, "System 2")
        systems.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(systems)
        layout.addWidget(hint_label(
            "Keys stay in memory for this session and are never shown or saved. On another PC, "
            "save the same environment variables (see README); the app finds them when it starts."))
        layout.addStretch(1)
        self.rescan()

    # --- System 2 chat ---------------------------------------------------------------

    def _build_chat(self, form: QFormLayout) -> None:
        self.provider = QComboBox()
        self.provider.addItem("Off", "")
        self.provider.setToolTip("Detected chat providers")
        self.provider.activated.connect(self._choose)
        form.addRow("Chat with", self.provider)
        self.api_model = QComboBox()
        self.api_model.setToolTip("Provider's chat models")
        self.api_model.activated.connect(self._choose_model)
        self.api_model_label = QLabel("API model")
        form.addRow(self.api_model_label, self.api_model)
        self.local_models = QComboBox()
        self.local_models.setToolTip("Ollama models on this PC")
        self.local_models.activated.connect(self._choose_model)
        self.local_models_label = QLabel("Local model")
        form.addRow(self.local_models_label, self.local_models)
        # One row of icon buttons across the page: Start/Restart, Available Tools, Rescan PC, Watch my edits,
        # Export History, Clear Conversation.
        self.start_button = icon_button(*START, "primary")
        self.start_button.clicked.connect(self.start)
        self.tools_button = icon_button(0xE90F, "Available Tools")
        self.tools_button.clicked.connect(self.show_tools)
        rescan = icon_button(0xE721, "Rescan PC")
        rescan.clicked.connect(self.rescan)
        self.watch = icon_button(0xE7B3, "Watch my edits")
        self.watch.setCheckable(True)
        self.watch.toggled.connect(lambda: self._indicate())
        self.export_button = icon_button(0xE896, "Export History…")
        self.export_button.clicked.connect(self.export_history)
        self.clear_button = icon_button(0xE74D, "Clear Conversation…", "danger")
        self.clear_button.clicked.connect(self.clear_conversation)
        start_row = QHBoxLayout()
        for button in (self.start_button, self.tools_button, rescan, self.watch, self.export_button,
                       self.clear_button):
            start_row.addWidget(button)
        start_row.addStretch(1)
        form.addRow(start_row)
        self.progress_label = QLabel()
        self.progress_label.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # busy: the model gives no progress to report
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        self.transcript = QTextBrowser()
        self.transcript.setOpenExternalLinks(True)
        self.transcript.setMinimumHeight(220)  # the whole chat fits a window 800 px tall
        form.addRow(self.transcript)
        # A change the model wants to make: nothing happens until Apply.
        self.proposal = QWidget()
        proposal = QVBoxLayout(self.proposal)
        proposal.setContentsMargins(0, 0, 0, 0)
        self.proposal_text = QLabel()
        self.proposal_text.setWordWrap(True)
        self.proposal_text.setProperty("role", "banner")
        proposal.addWidget(self.proposal_text)
        decide = QHBoxLayout()
        self.apply_button = styled_button("Apply", "primary", "Make it (Ctrl+Z undoes)")
        self.apply_button.clicked.connect(lambda: self._decide(True))
        self.skip_button = styled_button("Skip", tooltip="Don't make this change")
        self.skip_button.clicked.connect(lambda: self._decide(False))
        decide.addWidget(self.apply_button)
        decide.addWidget(self.skip_button)
        proposal.addLayout(decide)
        self.proposal.hide()
        form.addRow(self.proposal)
        form.addRow(self.progress_label)  # what the model is doing, just above where the user types
        form.addRow(self.progress)
        self.prompt = QLineEdit()
        self.prompt.setToolTip("Ask, or request a change")
        self.prompt.returnPressed.connect(self.send)
        # Ask and Stop share the place beside the box, one at a time; the box is as tall as they are.
        self.send_button = icon_button(0xE724, "Ask", "primary")
        self.send_button.clicked.connect(self.send)
        self.stop_button = icon_button(0xE71A, "Stop")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.hide()
        self.prompt.setMinimumHeight(self.send_button.sizeHint().height())
        prompt_row = QHBoxLayout()
        prompt_row.addWidget(self.prompt, 1)
        prompt_row.addWidget(self.send_button)
        prompt_row.addWidget(self.stop_button)
        form.addRow(prompt_row)
        self._relay = _Relay()
        self._relay.done.connect(self._finished)
        self._relay.text.connect(self._streamed)
        self._relay.tool.connect(self._tool_requested)
        self._relay.listed.connect(self._listed)
        self._relay.system_1.connect(self._system_1_started)
        self._busy_text, self._busy_since = None, 0.0
        self._busy_timer = QTimer(self, interval=1000)  # the seconds count, so a slow model shows it's alive
        self._busy_timer.timeout.connect(self._show_busy)
        self._render_timer = QTimer(self, singleShot=True, interval=60)  # streamed text: redraw at most ~16/s
        self._render_timer.timeout.connect(self._render)
        self._watch_timer = QTimer(self, singleShot=True, interval=WATCH_PAUSE_MS)
        self._watch_timer.timeout.connect(self._watch_look)
        self._host = None  # the window's network and map; see attach()
        self._session = self._turn = 0
        self._running = False  # a warm-up or answer is on its way
        self._model = ""
        self._wanted = ""  # the model picked or being connected, until it has warmed up
        self._connected = ("", "")  # (provider, model) started with Start, even if still warming up
        self._last_api: dict[str, str] = {}  # each provider's last started model, picked again next time
        self._activity = "off"  # see _indicate()
        self._turn_kind = "answering"
        self._system = ""  # the system prompt the connected model warmed up with
        self._tools = None  # tool specs, or None when this chat has none
        self._pending: _ToolRequest | None = None  # an edit waiting for Apply or Skip
        self._step: dict | None = None  # the answer under way, recorded for the window's step(event)
        self._preview: str | None = None  # the Scenario Builder's plan for the approval card
        # The planning journal: the assistant's plans (note_plan), each tool call and what came of it (read,
        # applied, skipped, refused), for studying how the agent plans; kept with experiment workbooks.
        self.journal: list[dict] = []
        self._seen = None  # the network as the assistant last knew it; see _context()
        self.history: list[dict] = []  # the conversation, without the system prompt
        # Every message of every answer as it happened (the user's with its editor update, the model's with
        # its thinking and tool calls, the tool results), for Export History; not saved, not made portable.
        self.record: list[dict] = []
        self.export_folder = None  # where Export History offers to save (the window's results folder)
        self._shown: list[list] = []  # [speaker, or None for a note; markdown] in the transcript
        self._choose()

    def attach(self, network, selection, show, edited, od, traffic=None, types=None, runs=None, state=None,
               step=None) -> None:
        """Give the chat the open network: `network()` returns it, `selection()` describes
        the map selection, `show(id)` points an object out, `edited(tool)` follows a change,
        `od(tool, args)` runs a demand tool on the Demand Generator's controls, `traffic(args)`
        reads the live run, `types(tool, args, check_only)` works Mode Behavior's vehicle types, `state()` gives
        the project's state before a step and `step(event)` takes each answer's record (the Research Log)."""
        self._host = SimpleNamespace(network=network, selection=selection, show=show, edited=edited, od=od,
                                     traffic=traffic or (lambda args: {"error": "No run is going."}),
                                     types=types or (lambda name, args, check_only=False: {"error": "No vehicle types here."}),
                                     runs=runs or (lambda name, args, check_only=False: {"error": "No runs here."}),
                                     state=state or (lambda: None), step=step or (lambda event: None))
        self._seen = self._snapshot()

    def _snapshot(self):
        return deepcopy(self._host.network()) if self._host else None

    def _selection(self) -> tuple[str, str]:
        """(provider, model) picked in the dropdowns; the model is "" while none is picked."""
        provider = self._provider()
        if provider == "local":
            return provider, self.local_model()
        return provider, (self.api_model.currentData() or "") if provider else ""

    def _choose(self) -> None:
        """A pick in Chat with. Nothing starts: Grok and Gemini list their models for the
        API model dropdown (a list request only), and Start connects."""
        provider = self._provider()
        if provider and provider == self._connected[0]:
            self._refresh_start()  # picked the connected one again: keep it
            return
        self._disconnect()
        if provider in ("grok", "gemini"):
            self._list_models(provider)
        self._refresh_start()

    def _choose_model(self) -> None:
        if self._selection() != self._connected:
            self._disconnect()
        self._refresh_start()

    def _disconnect(self) -> None:
        """Let go of the connected model (the conversation stays); nothing runs until Start."""
        self._session += 1  # replies still on their way are ignored
        self._turn += 1
        self._running = False
        self._settle_pending({"error": "The model was changed."})
        self._connected, self._model = ("", ""), ""
        self._stopping(False)
        self._busy(None)
        provider, model = self._selection()
        self._gate(False, "Press Start to connect" if provider else "Choose who to chat with above")
        self._wanted = model or (assistant.PROVIDER_NAMES[provider] if provider else "")
        self._indicate("chosen" if provider else "off")

    def _refresh_start(self) -> None:
        """Show the chosen provider's model dropdown, and Start (or Restart) when a model is picked."""
        provider, model = self._selection()
        for widget in (self.api_model, self.api_model_label):
            widget.setVisible(provider in ("grok", "gemini"))
        for widget in (self.local_models, self.local_models_label):
            widget.setVisible(provider == "local")
        self.start_button.setEnabled(bool(model))
        set_glyph(self.start_button, *(RESTART if provider and (provider, model) == self._connected else START))
        if self._activity == "chosen" and model:
            self._wanted = model
            self._indicate()

    def _list_models(self, provider: str) -> None:
        """Fetch the provider's chat models for the API model dropdown, off the UI thread."""
        session, relay = self._session, self._relay
        key = self.keys().get(assistant.KEY_NAMES[provider], "")
        self.api_model.clear()
        self.api_model.addItem("Loading models…", None)
        self._busy(f"Listing {assistant.PROVIDER_NAMES[provider]} models…")

        def target() -> None:
            try:
                relay.listed.emit(session, provider, assistant.list_models(provider, key), None)
            except AssistantError as exc:
                relay.listed.emit(session, provider, None, str(exc))
            except Exception as exc:  # noqa: BLE001 - say so rather than stay on "Loading"
                relay.listed.emit(session, provider, None, f"Unexpected error: {exc!r}")

        threading.Thread(target=target, daemon=True).start()

    def _listed(self, session: int, provider: str, models, error: str | None) -> None:
        if session != self._session or provider != self._provider():
            return  # the pick has changed since
        self._busy(None)
        self.api_model.clear()
        if models:
            for model in models:
                self.api_model.addItem(model, model)
            last = self._last_api.get(provider)
            self.api_model.setCurrentIndex(self.api_model.findData(
                last if last in models else assistant.default_model(provider, models)))
        else:
            self.api_model.addItem("No models", None)
            self._say("Error", f"Couldn't list {assistant.PROVIDER_NAMES[provider]} models: "
                               f"{error or 'none for this key'}. Rescan PC, then pick it again.")
        self._refresh_start()

    def _gate(self, open_: bool, placeholder: str) -> None:
        self.prompt.setEnabled(open_)
        self.send_button.setEnabled(open_)
        self.prompt.setPlaceholderText(placeholder)

    def _indicate(self, activity: str | None = None) -> None:
        """Tell the window what System 2 is doing, e.g. "Llama is watching". `activity` is
        off, chosen (picked, not started), setup, warming, error, ready, answering, looking
        (watch mode) or approval."""
        self._activity = activity or self._activity
        provider = self._provider()
        if not provider or self._activity == "off":
            self.status_line = "Off"
            self.indicator.emit("", "")
            return
        model = self._model or self._wanted
        watching = self.watch.isChecked()
        state, words = {
            "chosen": ("idle", "is selected: press Start"),
            "setup": ("failed", f"needs {assistant.KEY_NAMES.get(provider, 'a key')}"),
            "warming": ("running", "is warming up…"),
            "error": ("failed", "couldn't start"),
            "ready": ("passed", "is watching" if watching else "is ready"),
            "answering": ("running", "is answering…"),
            "looking": ("running", "is looking at your edits…"),
            "approval": ("waiting", "is waiting for your approval"),
        }[self._activity]
        detail = f" ({html.escape(model)})" if model_label(model) != model else ""
        self.status_line = f"{model_label(model)} {words}" + (f" ({model})" if detail else "")
        text = (f'<a href="system2" style="color:{theme.TEXT}; text-decoration:none">'
                f'<span style="color:{theme.STATUS.get(state, theme.SECONDARY_TEXT)}; font-size:14pt">●</span> '
                f'<b>{html.escape(model_label(model))} {words}</b>'
                f'<span style="color:{theme.SECONDARY_TEXT}">{detail}</span></a>')
        tip = f"{model} via {assistant.PROVIDER_NAMES[provider]}"
        self.indicator.emit(text, tip)

    def show_system_2(self) -> None:
        self.systems.setCurrentIndex(1)

    def _provider(self) -> str:
        """The chosen provider (grok, gemini or local), or "" for Off."""
        return self.provider.currentData() or ""

    def _busy(self, text: str | None) -> None:
        """Show what the model is doing (None: nothing) and how long this has taken, counting from
        when it got busy, through thinking, tool calls and writing."""
        if text and not self._busy_text:
            self._busy_since = time.monotonic()
        self._busy_text = text
        self.progress_label.setVisible(bool(text))
        self.progress.setVisible(bool(text))
        if text:
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
        self._show_busy()

    def _show_busy(self) -> None:
        seconds = int(time.monotonic() - self._busy_since)
        self.progress_label.setText(f"{self._busy_text} {seconds} s" if self._busy_text and seconds else
                                    self._busy_text or "")

    def _say(self, speaker: str | None, text: str) -> None:
        self._shown.append([speaker, text])
        self._render()

    def _render(self) -> None:
        self.transcript.setMarkdown("\n\n".join(f"**{who}**\n\n{said}" if who else f"*{said}*"
                                                for who, said in self._shown))
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _run(self, job: str, work) -> None:
        """Run `work(on_text, run_tool, cancelled)` off the UI thread. Its result returns
        through _finished, streamed text through _streamed, tool calls through _tool_requested."""
        session, turn, relay = self._session, self._turn, self._relay
        self._running = True

        def cancelled() -> bool:  # a new chat, or Stop
            return session != self._session or turn != self._turn

        def run_tool(call: dict):
            if self._host is not None:  # its slow or random part first, here off the UI thread
                try:
                    network_tools.prepare(self._host.network(), call.get("name", ""), call.get("arguments"))
                except Exception:  # noqa: BLE001 - the tool itself reports any problem
                    pass
            request = _ToolRequest(call)
            relay.tool.emit(session, request)
            while not request.done.wait(0.2):
                if cancelled():
                    return {"error": "Stopped by the user."}
            return request.result

        def target() -> None:
            try:
                result = work(lambda text, speaker: relay.text.emit(session, speaker, text), run_tool, cancelled)
                relay.done.emit(session, job, result, None)
            except Cancelled:
                relay.done.emit(session, job, STOPPED, None)
            except AssistantError as exc:
                relay.done.emit(session, job, None, str(exc))
            except Exception as exc:  # noqa: BLE001 - show anything unexpected rather than hang busy
                relay.done.emit(session, job, None, f"Unexpected error: {exc!r}")

        threading.Thread(target=target, daemon=True).start()

    def start(self) -> None:
        """Start (or Restart): connect the picked model. It warms up on the documents and the
        conversation so far (which carries over); the gate stays closed until it's ready."""
        provider, wanted = self._selection()
        if not provider or not wanted:
            return
        self._session += 1  # replies still on their way from the last choice are ignored
        self._turn += 1
        self._running = False
        self._settle_pending({"error": "The chat was restarted."})
        self.history = assistant.portable(self.history)  # plain text any model can carry on from
        self._model = ""
        self._stopping(False)
        self._busy(None)
        name = assistant.PROVIDER_NAMES[provider]
        key = self.keys().get(assistant.KEY_NAMES.get(provider, ""), "")
        if provider != "local" and not key:
            self._gate(False, f"Needs {assistant.KEY_NAMES[provider]}")
            self._say("Setup", f"Save your key as the environment variable **{assistant.KEY_NAMES[provider]}** "
                               "(see README), then Rescan PC.")
            self._wanted = name
            self._indicate("setup")
            return
        self._wanted, self._connected = wanted, (provider, wanted)
        if provider != "local":
            self._last_api[provider] = wanted
        self._refresh_start()  # now Restart
        tools = network_tools.tool_specs() if self._host else None
        history = list(self.history)
        self._gate(False, "Warming up…")
        self._indicate("warming")
        self._busy(f"Warming up: {wanted} is reading README.md and ARCHITECTURE.md"
                   + (" and the conversation…" if history else "…"))

        def warm_up(on_text, run_tool, cancelled):
            model = wanted
            # Tools from the first question on, if this model can use them; warming up only reads.
            offered = tools if tools and (provider != "local" or assistant.supports_tools(model)) else None
            system = assistant.system_prompt(tools=bool(offered))
            messages = [{"role": "system", "content": system}, *history,
                        {"role": "user", "content": assistant.RESUME if history else assistant.WARM_UP}]
            assistant.converse(provider, model, messages, key, None, lambda text: on_text(text, model),
                               run_tool, cancelled)  # the reply is shown, not kept: it's only a check
            return model, offered, system

        self._run("warm", warm_up)

    def send(self) -> None:
        question = self.prompt.text().strip()
        if question and self.prompt.isEnabled():
            self.prompt.clear()
            self._ask(question, question)

    def _context(self) -> tuple[str, list[str]]:
        """The [Editor update] that goes in front of the next message: the user's changes since
        the assistant's last reply and the map selection. Also what it shared, for the transcript."""
        if self._host is None:
            return "", []
        network = self._host.network()
        changes = network_tools.describe_changes(self._seen, network) if self._seen is not None else []
        selection = self._host.selection()
        selected = f"{selection['selected']} {', '.join(selection['ids'])}" if selection["ids"] else "nothing"
        lines = ["[Editor update]"]
        if changes:
            lines += ["The user changed the network since your last reply:", *(f"- {line}" for line in changes)]
        lines += [f"Selected on the map: {selected}", "[End of editor update]", "", ""]
        shared = ([f"your {len(changes)} change{'' if len(changes) == 1 else 's'}"] if changes else []) \
            + ([f"selection {selected}"] if selection["ids"] else [])
        return "\n".join(lines), shared

    def _ask(self, prompt: str, shown: str | None) -> None:
        """Send `prompt` after the editor update. `shown` is what the transcript shows as the
        user's words; None for a watch-mode look, which shows only what was shared."""
        if not self.prompt.isEnabled():
            return
        provider, model, tools = self._provider(), self._model, self._tools
        key = self.keys().get(assistant.KEY_NAMES.get(provider, ""), "")
        context, shared = self._context()
        self._seen = self._snapshot()  # the assistant now knows the network as it is
        if shown:
            self._say("You", shown)
        if shared:
            self._say(None, ("Shared with the assistant: " if shown else "Watch mode is looking at ")
                      + ", ".join(shared))
        self._gate(False, "Answering…")
        self._busy(f"{model} is thinking…")
        self._turn_kind = "answering" if shown else "looking"
        self._indicate(self._turn_kind)
        self._stopping(True)
        messages = [{"role": "system", "content": self._system}, *self.history,
                    {"role": "user", "content": context + prompt}]
        # The tools this request most likely needs (network_tools.tools_for), and those the conversation has used;
        # find_tools adds others mid-answer, and a tool the model names that it wasn't offered is added as it runs.
        used = {call.get("name") for message in self.history for call in message.get("tool_calls") or []}
        offered = network_tools.tools_for(prompt, used) if tools else None
        self._step = {"kind": "prompt" if shown else "watch", "prompt": shown or prompt, "editor_update": context,
                      "provider": provider, "model": model, "started": time.time(), "calls": [],
                      "tools_offered": [spec["name"] for spec in offered or []],
                      "before": self._host.state() if self._host else None}

        def answer(on_text, run_tool, cancelled):
            speak = lambda text: on_text(text, model)  # noqa: E731

            def call_tool(call: dict):
                if call.get("name") == network_tools.FIND_TOOLS:  # answered here, kept in the step's record
                    found = network_tools.expand_tools(call.get("arguments"), offered)
                    self._record_call(call, what="find more tools", decision="read", result=found)
                    return found
                against = network_tools.contradicts(call.get("name", ""), call.get("arguments"), prompt) if shown else None
                if against:  # not run, nor shown to the user: the model is told what they asked for
                    refused = {"error": against, "error_type": "not_what_was_asked"}
                    self._record_call(call, what=network_tools.describe_call(call["name"], call.get("arguments") or {}),
                                      decision="refused: not what was asked", result=refused)
                    return refused
                if offered is not None and call.get("name") not in {spec["name"] for spec in offered}:
                    offered.extend(spec for spec in network_tools.tool_specs() if spec["name"] == call.get("name"))
                return run_tool(call)

            try:
                return assistant.converse(provider, model, messages, key, offered, speak, call_tool, cancelled), tools
            except ToolsUnsupported:  # Ollama didn't say, then refused: carry on without tools
                return assistant.converse(provider, model, messages, key, None, speak, run_tool, cancelled), None

        self._run("answer", answer)

    def editor_changed(self) -> None:
        """The window calls this after each undoable change. Watch mode looks once the user
        pauses; changes made while the assistant is busy go with its next look or message."""
        if self.watch.isChecked() and not self._running:
            self._watch_timer.start()

    def _watch_look(self) -> None:
        if not (self.watch.isChecked() and self.prompt.isEnabled() and self._seen is not None):
            return
        if network_tools.describe_changes(self._seen, self._host.network()):
            self._ask(WATCH_PROMPT, None)

    def _stopping(self, answering: bool) -> None:
        """Stop in Ask's place while an answer comes in."""
        self.stop_button.setVisible(answering)
        self.send_button.setVisible(not answering)

    def stop(self) -> None:
        """Stop the answer in progress; the question and partial answer stay out of the conversation."""
        self._turn += 1
        self._settle_pending({"error": "Stopped by the user."})

    def clear_conversation(self) -> None:
        if (self.history or self._shown) and QMessageBox.question(
                self, "Clear conversation", "Clear the conversation? The assistant forgets it; your network "
                "stays as it is. A saved project keeps its copy until you save again.") \
                != QMessageBox.StandardButton.Yes:
            return
        self._turn += 1  # stops an answer in progress
        self._settle_pending({"error": "The conversation was cleared."})
        self.history, self._shown, self.record = [], [], []
        self._seen = self._snapshot()
        self._render()

    def show_tools(self) -> None:
        """List the assistant's tools and what each does in the transcript, from the app's own list
        (network_tools.TOOLS): no model needed, nothing sent, not saved with the conversation."""
        def line(tool: dict) -> str:
            names = ", ".join(tool.get("parameters", {}).get("properties", {}))
            return f"- **{tool['name']}**({names}): {tool['description']}"

        tools = network_tools.TOOLS
        reads = [line(tool) for tool in tools if not tool["edits"]]
        changes = [line(tool) for tool in tools if tool["edits"]]
        parts = [f"{len(tools)} tools. Reads, run at once:", *reads, "Changes, each waiting for your Apply:", *changes]
        if self._connected[1] and self._tools is None and not self._running:
            parts.append(f"{model_label(self._connected[1])} can't call tools, so it answers from the documents "
                         "and the editor update (your changes and selection) sent with each message.")
        text = "\n\n".join(parts)
        self._say(TOOLS_NOTE, text)

    def export_history(self) -> None:
        """Export History: save the chat, the record and the planning journal (history_markdown, or JSON)."""
        if not (self._shown or self.record or self.journal):
            self._say(None, "Nothing to export yet: ask something first.")
            return
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        folder = Path(self.export_folder) if self.export_folder else Path.home()
        name = f"ACMS chat {time.strftime('%Y-%m-%d %H-%M-%S')}.md"
        path, chosen = QFileDialog.getSaveFileName(self, "Export History", str(folder / name),
                                                   "Markdown (*.md);;JSON (*.json)")
        if not path:
            return
        as_json = path.lower().endswith(".json") or (chosen.startswith("JSON") and not path.lower().endswith(".md"))
        if as_json and not path.lower().endswith(".json"):
            path += ".json"
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(history_json(self._shown, self.record, self.journal) if as_json else
                                  history_markdown(self._shown, self.record, self.journal), encoding="utf-8")
        except OSError as exc:
            self._say("Error", f"Couldn't save the history: {exc}")
            return
        self._say(None, f"History exported to {path}")

    def conversation_state(self) -> dict | None:
        """The conversation to save with the project: plain text, never any keys (nor tool lists)."""
        shown = [item for item in self._shown if item[0] != TOOLS_NOTE]
        if not self.history and not shown:
            return None
        return {"messages": assistant.portable(self.history), "transcript": shown}

    def restore_conversation(self, state: dict | None) -> None:
        """Replace the conversation with one loaded from a project (None: start empty)."""
        self._turn += 1  # stops an answer in progress
        self._settle_pending({"error": "Another project was loaded."})
        state = state or {}
        self.history = [{"role": message["role"], "content": message["content"]}
                        for message in state.get("messages", []) if isinstance(message, dict)
                        and message.get("role") in ("user", "assistant") and isinstance(message.get("content"), str)]
        self._shown = [[who if isinstance(who, str) else None, said] for item in state.get("transcript", [])
                       if isinstance(item, list) and len(item) == 2 for who, said in [item] if isinstance(said, str)]
        if self._shown:
            self._shown.append([None, "Conversation restored from the project file."])
        self._seen = self._snapshot()
        self._render()

    def _streamed(self, session: int, speaker: str, text: str) -> None:
        if session != self._session:
            return
        if not self._shown or self._shown[-1][0] != speaker:
            self._shown.append([speaker, ""])
        self._shown[-1][1] += text
        if self._running and self._pending is None and self._busy_text != f"{speaker} is writing…":
            self._busy(f"{speaker} is writing…")
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _tool_requested(self, session: int, request: _ToolRequest) -> None:
        call = request.call
        if session != self._session:
            request.result = {"error": "The chat was restarted."}
            request.done.set()
            return
        try:  # every tool's arguments in the shapes its schema gives, or refused saying what it takes
            call["arguments"] = network_tools.normalise(call["name"], call["arguments"])
        except network_tools.ToolError as exc:
            self.log(call["name"], call["arguments"], f"refused: {exc}")
            request.result = {"error": f"{exc} Nothing was run."}
            self._record_call(call, decision="refused: arguments", result=request.result)
            self._say(None, f"Not run: {call['name']}. {exc}")
            request.done.set()
            return
        line = network_tools.describe_call(call["name"], call["arguments"])
        problem = self._check(call["name"], call["arguments"]) if call["name"] in network_tools.EDIT_TOOLS else None
        if problem:  # refused before asking: the user only ever approves changes that can work
            self.log(call["name"], call["arguments"], f"refused: {problem}")
            request.result = {"error": f"{problem} Nothing was changed and the user wasn't asked."}
            self._record_call(call, what=line, decision="refused: check", result=request.result)
            self._say(None, f"Not proposed: {line}. {problem}")
            request.done.set()
            return
        if call["name"] in network_tools.EDIT_TOOLS:
            self._pending = request
            if self._preview:  # the Scenario Builder's plan, step by step, with what it assumed
                line = f"{line}<br>{self._preview}"
            self.proposal_text.setText(f"<b>Proposed change</b><br>{line}")
            request.record = self._record_call(call, what=network_tools.describe_call(call["name"], call["arguments"]),
                                               plan=_plain(self._preview) if self._preview else None,
                                               decision="waiting", proposed_at=time.time())
            self.proposal.show()
            self.apply_button.setFocus()
            self._busy("Waiting for you to apply or skip the change…")
            self._indicate("approval")
            return
        started = time.time()
        request.result = self._execute(call["name"], call["arguments"])
        self._record_call(call, what=line, decision="read", result=request.result, at=started,
                          compute_s=round(time.time() - started, 3))
        if call["name"] != "note_plan":
            self.log(call["name"], call["arguments"], f"read (couldn't: {request.result['error']})"
                     if "error" in request.result else "read")
        self._say(None, line + (f" (couldn't: {request.result['error']})" if "error" in request.result else ""))
        self._busy(f"{self._model} is thinking…")  # it reads the result next
        request.done.set()

    def _check(self, name: str, args: dict) -> str | None:
        """Why a proposed change would fail, found without changing anything: network edits
        run on a copy of the network, demand changes run only their checks."""
        if self._host is None:
            return "No network is open."
        self._preview = None
        try:
            if name in network_tools.OD_TOOLS:
                self._host.od(name, args, check_only=True)
            elif name in network_tools.TYPE_TOOLS:
                self._host.types(name, args, check_only=True)
            elif name in network_tools.RUN_TOOLS:
                checked = self._host.runs(name, args, check_only=True)
                self._preview = checked.get("preview") if isinstance(checked, dict) else None
            else:
                network_tools.run_tool(deepcopy(self._host.network()), name, args)
        except (network_tools.ToolError, ValueError) as exc:
            return str(exc)
        return None

    def _decide(self, apply: bool) -> None:
        request = self._pending
        if request is None:
            return
        call = request.call
        line = network_tools.describe_call(call["name"], call["arguments"])
        decided = time.time()
        if apply:
            self.proposal.hide()  # no second Apply while it runs (a test can take a while)
            self._busy(f"Applying: {line}…")
            result = self._execute(call["name"], call["arguments"])
            self._say(None, f"Couldn't apply: {line}. {result['error']}" if "error" in result else f"Applied: {line}")
            self.log(call["name"], call["arguments"], f"applied by the user; couldn't: {result['error']}"
                     if "error" in result else "applied by the user")
        else:
            result = {"declined": "The user skipped this change. Don't make it again unless asked."}
            self._say(None, f"Skipped: {line}")
            self.log(call["name"], call["arguments"], "skipped by the user")
        if request.record is not None:
            request.record.update(decision="applied" if apply else "skipped", decided_at=decided, result=result,
                                  compute_s=round(time.time() - decided, 3) if apply else 0.0)
        self._settle_pending(result)
        self._busy(f"{self._model} is thinking…")
        self._indicate(self._turn_kind)

    def _settle_pending(self, result: dict) -> None:
        """Answer the waiting edit (if any) and put its card away."""
        request, self._pending = self._pending, None
        self.proposal.hide()
        if request is not None:
            if request.record is not None and request.record.get("decision") == "waiting":
                request.record.update(decision="not decided", decided_at=time.time(), result=result)
            request.result = result
            request.done.set()

    def _execute(self, name: str, args: dict) -> dict:
        """Run one tool call on the open network (UI thread)."""
        if self._host is None:
            return {"error": "No network is open."}
        try:
            if name == "get_selection":
                return self._host.selection()
            if name == "show_on_map":
                return self._host.show(str(args.get("id", "")))
            if name == "read_traffic":
                return self._host.traffic(args)
            if name in network_tools.OD_TOOLS:
                return self._host.od(name, args)  # works the Demand Generator's controls
            if name in network_tools.TYPE_TOOLS:
                return self._host.types(name, args)  # Mode Behavior's vehicle types
            if name in network_tools.RUN_TOOLS:
                result = self._host.runs(name, args)  # runs and experiments, headless
                if name in ("build_scenario", "assign_routes", "institution_mode_split"):
                    self._seen = self._snapshot()  # the assistant knows its own change
                return result
            if name == "note_plan":
                return self._note_plan(args)
            result = network_tools.run_tool(self._host.network(), name, args)
        except (network_tools.ToolError, ValueError) as exc:
            return network_tools.explain_error(str(exc), self._host.network())
        if name in network_tools.EDIT_TOOLS and name not in network_tools.OD_TOOLS | network_tools.TYPE_TOOLS \
                | network_tools.RUN_TOOLS:  # network edits
            self._host.edited(name)
            # ponytail: an edit the user makes on the map while the assistant is mid-answer is
            # folded in here too; track per-object authorship if that ever matters.
            self._seen = self._snapshot()  # the assistant knows its own change
        return result

    def _record_call(self, call: dict, **fields) -> dict:
        """One tool call in the step's record: tool, arguments and what happened to it (`fields`)."""
        record = {"tool": call["name"], "arguments": call["arguments"], "at": time.time(), **fields}
        if self._step is not None:
            self._step["calls"].append(record)
        return record

    def _end_step(self, result, error: str | None) -> None:
        """The answer ended: its record, with the model's messages, reply and token use, to the window
        (`step`, the Research Log; the window adds the changes and the map)."""
        step, self._step = self._step, None
        if step is None or self._host is None:
            return
        step["ended"] = time.time()
        messages = []
        if result is not STOPPED and not error and result:
            messages = [{key: value for key, value in message.items() if key != "raw"}
                        for message in result[0][1 + len(self.history):]]
        replies = [m.get("content") for m in messages if m.get("role") == "assistant" and m.get("content")]
        step.update(messages=messages, reply=replies[-1] if replies else "", plan=assistant.plan_line(replies),
                    error="stopped by the user" if result is STOPPED else (error or ""),
                    usage=[dict(u) for u in assistant.USAGE if step["started"] - 1 <= u.get("at", 0) <= step["ended"] + 1])
        self._host.step(step)

    def log(self, tool: str, args, outcome: str, kind: str = "tool call") -> None:
        """One line of the planning journal."""
        what = network_tools.describe_call(tool, args if isinstance(args, dict) else {})
        self.journal.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "tool": tool,
                             "what": what, "arguments": json.dumps(args, default=str)[:2000], "outcome": outcome})

    def _note_plan(self, args: dict) -> dict:
        """note_plan: the assistant's goal, hypothesis, plan, expectation or conclusion, into the journal."""
        parts = [f"{key}: {str(args[key]).strip()}" for key in ("goal", "hypothesis", "plan", "expected", "conclusion")
                 if isinstance(args, dict) and str(args.get(key) or "").strip()]
        if not parts:
            return {"error": "Say at least one of goal, hypothesis, plan, expected or conclusion."}
        self.journal.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "plan", "tool": "note_plan",
                             "what": " · ".join(parts), "arguments": "", "outcome": "noted"})
        return {"noted": len(self.journal), "note": "Kept in the planning journal (each experiment's workbook)."}

    def _finished(self, session: int, job: str, result, error: str | None) -> None:
        if session != self._session:
            return  # from a conversation that has since been replaced
        self._running = False
        self._render_timer.stop()
        self._render()
        self._busy(None)
        self._stopping(False)
        self._settle_pending({"error": "The answer ended."})
        provider = self._provider()
        if job == "answer":
            self._end_step(result, error)
        if result is STOPPED:
            self._say(None, "Stopped. That question is left out of the conversation.")
            self._gate(True, READY)
            self._indicate("ready")
        elif error:
            self._gate(job == "answer", READY if job == "answer" else "Not ready")
            self._indicate("ready" if job == "answer" else "error")
            self._say("Error", f"{error}\n\n" + ("Ask again, or rephrase." if job == "answer" else
                                                "Press Restart to try again."))
        else:
            if job == "warm":
                self._model, self._tools, self._system = result
                if self._host and self._tools is None:
                    self._say(None, f"{self._model} can't use tools, so it answers from the documents only "
                                    "and can't see or change your network. Try llama3.1 or llama3.2.")
                if not self.history:
                    self._seen = self._snapshot()  # a new conversation starts from the network as it is
            else:
                messages, tools = result
                at = time.strftime("%Y-%m-%d %H:%M:%S")
                self.record += [{"at": at, "model": self._model, **{key: value for key, value in message.items()
                                                                    if key != "raw"}}
                                for message in messages[1 + len(self.history):]]
                self.history = messages[1:]  # without the system prompt
                if self._tools and tools is None:
                    self._say(None, f"{self._model} can't use tools after all, so it answers from the "
                                    "documents only.")
                self._tools = tools
            self._gate(True, READY)
            self._indicate("ready")
            self.prompt.setFocus()
        if self.watch.isChecked():
            self._watch_timer.start()  # edits made while it was busy

    # --- Keys and local models -------------------------------------------------------

    def rescan(self) -> None:
        """Scan this PC's environment variables for the keys, and Ollama for its models; offer
        System 2 only what was found. Keeps the chosen option if it is still there."""
        found = scan_keys(["JEV_API_KEY", "LAYA_EXE", *assistant.KEY_NAMES.values()])
        self._found = found
        self._laya_exe = assistant.laya_exe(found.get("LAYA_EXE", ""))
        laya = "laya.exe" if self._laya_exe else "a running Laya" if assistant.laya_health() else ""
        here = {"jev": "JEV_API_KEY" in found, "laya": bool(laya)}
        chosen = self.system_1.currentData() or ""
        self.system_1.clear()
        self.system_1.addItem("Off", "")
        for name, label in SYSTEM_1.items():
            if here[name]:
                self.system_1.addItem(label, name)
        self.system_1.setCurrentIndex(max(0, self.system_1.findData(chosen)))
        if chosen and not here[chosen]:
            self._choose_system_1()  # its key or laya.exe has gone: back to Off
        missing_1 = [what for name, what in (("jev", "JEV_API_KEY"), ("laya", "LAYA_EXE (laya.exe's path)"))
                     if not here[name]]
        self.system_1_status.setText(
            "Found on this PC: " + (", ".join([*(["JEV_API_KEY"] if here["jev"] else []), *([laya] if laya else [])])
                                    or "nothing yet")
            + "." + (f" Not found: {', '.join(missing_1)}. Save it as an environment variable (see "
                     "README), then Rescan PC." if missing_1 else ""))

        models, chosen = ollama_models(), self.local_models.currentText()
        self.local_models.clear()
        self.local_models.addItems(models or [])
        self.local_models.setCurrentIndex(max(0, self.local_models.findText(chosen)))

        offered = [provider for provider in ("grok", "gemini") if assistant.KEY_NAMES[provider] in found]
        offered += ["local"] if models else []
        current = self._provider()
        self.provider.clear()
        self.provider.addItem("Off", "")
        for provider in offered:
            self.provider.addItem(PROVIDERS[provider], provider)
        self.provider.setCurrentIndex(max(0, self.provider.findData(current)))

        have = [name for name in assistant.KEY_NAMES.values() if name in found]
        have += [f"Ollama ({len(models)} model{'' if len(models) == 1 else 's'})"] if models else []
        missing = [name for name in assistant.KEY_NAMES.values() if name not in found]
        missing += ["Ollama" if models is None else "Ollama models (ollama pull <model>)"] if not models else []
        self.found_status.setText(
            "Found on this PC: " + (", ".join(have) or "nothing yet") + "."
            + (f" Not found: {', '.join(missing)}. Save a key as an environment variable "
               "(see README) or start Ollama, then Rescan PC." if missing else ""))
        if current and current not in offered:
            self._choose()  # its key or Ollama has gone: back to Off
        else:
            self._refresh_start()

    def keys(self) -> dict[str, str]:
        """The keys found in this PC's environment variables."""
        return dict(self._found)

    def model_for_agents(self) -> tuple[str, str, str] | None:
        """(provider, model, key) of the System 2 model started with Start, for other features that
        ask a model (the TSP extension's signal agent); None while none is started."""
        provider, model = self._connected
        if not provider or not model:
            return None
        return provider, model, self.keys().get(assistant.KEY_NAMES.get(provider, ""), "")

    def local_model(self) -> str:
        """The chosen Ollama model, or "" when none is available."""
        return self.local_models.currentText()

    # --- System 1 ----------------------------------------------------------------------

    def _choose_system_1(self) -> None:
        """A pick in System 1's Use. Laya starts here, off the UI thread (it loads its model); Jev
        needs nothing until it's asked. Any other pick ends a Laya this app started (its GPU memory)."""
        self._system_1 = choice = self.system_1.currentData() or ""
        if choice != "laya":
            assistant.stop_laya()
            self.system_1_state.setText("Jev answers through TypeSafe's API." if choice == "jev" else "")
            return
        self.system_1_state.setText("Starting Laya…")
        exe, relay = self._laya_exe, self._relay

        def target() -> None:
            try:
                relay.system_1.emit(choice, assistant.start_laya(exe), None)
            except AssistantError as exc:
                relay.system_1.emit(choice, None, str(exc))
            except Exception as exc:  # noqa: BLE001 - say so rather than stay on "Starting"
                relay.system_1.emit(choice, None, f"Unexpected error: {exc!r}")

        threading.Thread(target=target, daemon=True).start()

    def _system_1_started(self, choice: str, health, error: str | None) -> None:
        if choice != self._system_1:
            assistant.stop_laya()  # picked something else while it started
            return
        families = ", ".join(health.get("families") or []) if health else ""
        self.system_1_state.setText(f"Laya couldn't start: {error}" if error else
                                    f"Laya is ready on {health.get('device', 'this PC')}"
                                    + (f" ({families})." if families else "."))

    def agent_options(self) -> list[dict]:
        """Who a batch run can put in charge of TSP, as found by the last scan: No LLM (the baseline), System 1's
        (Jev, Laya) and System 2's (every Ollama model; Grok and Gemini whenever their keys are on this PC, each
        with the model last started in System 2, else the provider's usual one). Each: label, system, provider,
        model and name (for the run's file name)."""
        options = [{"label": "No LLM (baseline)", "system": "", "provider": "", "model": "", "name": "no-llm"}]
        for index in range(1, self.system_1.count()):
            provider = self.system_1.itemData(index)
            options.append({"label": f"System 1: {assistant.PROVIDER_NAMES[provider]}", "system": "1",
                            "provider": provider, "model": assistant.SYSTEM_ONE_MODEL, "name": provider})
        models = [("local", self.local_models.itemText(index)) for index in range(self.local_models.count())]
        for provider in ("grok", "gemini"):
            if assistant.KEY_NAMES[provider] in self._found:
                started = self._connected[1] if self._connected[0] == provider else ""
                models.append((provider, started or self._last_api.get(provider) or assistant.PREFERRED[provider][0]))
        for provider, model in models:
            options.append({"label": f"System 2: {model}", "system": "2", "provider": provider, "model": model,
                            "name": model})
        return options

    def start_laya(self) -> dict:
        """Laya's health, starting the laya.exe found by the last scan if none answers (any thread)."""
        return assistant.start_laya(self._laya_exe)

    def key_for(self, provider: str) -> str:
        """The key this PC holds for a provider (jev, grok, gemini); "" for local ones."""
        name = "JEV_API_KEY" if provider == "jev" else assistant.KEY_NAMES.get(provider, "")
        return self.keys().get(name, "") if name else ""

    def system_one_for_agents(self) -> tuple[str, str] | None:
        """(provider, key) of the System 1 chosen under Use, for features wanting a fast typed answer
        (the signal agent); None while it is Off. Safe from any thread."""
        choice = self._system_1
        return (choice, self.keys().get("JEV_API_KEY", "") if choice == "jev" else "") if choice else None
