"""System 2 assistant: chat about this app and the open network with Grok, Gemini or a local
Ollama model. Also System 1's back end: typed yes/no, choice and score answers from Jev
(TypeSafe's API) or a local Laya (laya.exe), in one encoder pass with no text generated.

Warming up gives the model README.md and ARCHITECTURE.md as its system prompt and asks it
to confirm it has read them. After that it answers questions, streaming its reply, and may
call tools (network_tools.py) to look at or change the network; converse() runs that loop.
Every model API is stateless, so each request resends the whole conversation. Plain HTTPS
and JSON from the standard library: no SDKs to install on another device.

Conversation messages are provider-neutral dicts:
  {"role": "system" | "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments": dict}],
   "raw": Gemini's own parts (replayed as-is: they carry its thought signatures)}
  {"role": "tool", "id": call id, "name": tool name, "content": JSON text}
"""

from __future__ import annotations

import atexit
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
# ponytail: read from the source tree (the app is copied as a folder); package them as
# data files if it is ever installed as a wheel.
DOCS = ("README.md", "ARCHITECTURE.md")
XAI_URL = "https://api.x.ai/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
KEY_NAMES = {"grok": "GROK_API_KEY", "gemini": "GEMINI_API_KEY"}
PROVIDER_NAMES = {"grok": "Grok", "gemini": "Gemini", "local": "Ollama", "jev": "Jev", "laya": "Laya"}
JEV_URL = "https://api.typesafe.ai/v1"  # TypeSafe's System One API, which serves Jev
SYSTEM_ONE_MODEL = "jev-latest"  # Jev's alias; Laya answers to it too
# A free-text yes/no question goes in as the state of one noul (P(yes)). On Laya this got 5 of 6
# general and 5 of 8 traffic cases right (splitting off the last sentence as the instructions did no
# better; the question as both leans to yes). Prose with distances and seconds is its weak spot: a
# structured state with a specific noul did far better, so new callers should use system_one directly.
YES_NO = "Is the answer to the question in `state` yes?"
LAYA_LOG = Path(tempfile.gettempdir()) / f"acms-laya-{os.getpid()}.log"  # what the laya.exe this app started printed
# Model lists change often, so they're fetched; the default is the first listed model
# matching the earliest of these, else the first listed.
PREFERRED = {"grok": ("grok-4",), "gemini": ("gemini-flash-latest", "gemini-2.5-flash")}
NOT_CHAT = ("imagine", "tts", "image", "banana", "lyria", "embedding")  # image, speech, music models
API_TIMEOUT_S, LOCAL_TIMEOUT_S = 180, 600  # a local model may first load from disk
KEEP_ALIVE = "30m"  # Ollama keeps the model loaded this long after a request (its default, 5 min, reloads after a pause)
STALL_TIMEOUT_S = 90  # once a reply streams, a server silent this long has stalled: fail now, not after the above
MAX_TOOL_ROUNDS = 12  # tool calls and replies in one answer before giving up
# Refusals of one tool in one answer before it isn't run again in that answer: llama3.2 3B called set_end_flows 7
# and generate_demand 9 times with new wrong arguments each time, to the round limit (110k input tokens a step)
MAX_REFUSALS = 3
# The context budget, in tokens. Past its window a model fails quietly (Ollama cuts the reply off, or drops the start
# of the prompt: the instructions and documents), so every request is trimmed to fit first (fit). ACMS_NUM_CTX sets a
# local model's window (default 32k, what an 8 GB GPU holds for a 4B model; 16384 for a 12B one).
try:
    LOCAL_CONTEXT_TOKENS = max(4096, int(os.environ.get("ACMS_NUM_CTX") or 32768))
except ValueError:
    LOCAL_CONTEXT_TOKENS = 32768
API_CONTEXT_TOKENS = 131072  # hosted models' windows are larger; this keeps their cost per request bounded too
REPLY_TOKENS = 4096  # kept free for the reply
CHARS_PER_TOKEN = 3.0  # on the safe side: JSON and code run near 3 characters a token, prose 4-5
# A local model's own count, where it was lower (more tokens than CHARS_PER_TOKEN gave): its next num_ctx is sized by
# it, so the window isn't too small for its tokenizer (a cache only lowers the count, so it never lowers this wrongly)
_CHARS_PER_TOKEN_SEEN: dict[str, float] = {}
STUB = '{"note": "an earlier tool result, left out to fit the context window"}'
CUT = " ... (cut to fit the context window: ask for less, e.g. one road, or a line range)"
WARM_UP = ("In one or two sentences, confirm you have read the documents "
           "and say what you can help with.")
RESUME = ("You are joining this conversation now, restored from the project. In one or two "
          "sentences, confirm you have read the documents and the conversation, "
          "and say where we left off.")
EDITOR_NOTE = (
    "\n\nThe user edits the network alongside you. Their messages may start with an [Editor "
    "update] block that the app adds: the changes they made since your last reply and what is "
    "selected on the map. Treat it as the current state, and don't ask the user to repeat it."
)
TOOLS_NOTE = (
    "\n\nYou also have tools for the network the user has open right now. Use them instead of guessing about it."
    "\n\nHOW TO WORK. Before your first tool call for a request, write one short line starting \"Plan:\": what the "
    "user wants in your own words, then -> and the tools you will call, in order, by name. For example: \"Plan: "
    "count the junctions, set every road to 40 km/h -> list_junctions, update_roads\". Then call them, in that same "
    "reply: never stop at saying what you will do. A request with several parts: do every part, in order. "
    "When you are done, reply in plain words (there is no answer tool). You are offered the tools this request most likely needs; if one "
    "you need isn't there, call find_tools with what you need. When the user leaves a choice to you, pick a sensible "
    "value and say so rather than asking; ask only when the request can be read two very different ways. Edit "
    "tools change the network only after the user approves each one in this chat (Ctrl+Z undoes it); if they "
    "decline, don't retry. Read every result: one with \"error\" means nothing happened; fix the call as its "
    "suggested_fix says, or tell the user. If what was asked can't be done (a service is down, nothing fits), say "
    "so and stop: never put something else in its place (a made-up grid for a real place) without asking. Quote "
    "the results' own numbers, never ones you expect. Leave out "
    "optional arguments the user didn't ask for: the defaults are quick, and route choice, conditions or extra "
    "seeds multiply a test's time."
    "\n\nRECIPES (adapt them; the words in quotes are examples of what users say):"
    "\n- \"a city of W x H metres\", \"a new grid\": generate_grid with vertical = W / spacing_m + 1 columns and "
    "horizontal = H / spacing_m + 1 rows (spacing_m 200-400; a dense city 200-250). It replaces the network; to keep "
    "some roads, edit the network instead (add_road, delete_objects, update_roads)."
    "\n- \"demand from buildings / institutions\", \"a dense city with homes, schools, offices\": deploy_institutions "
    "(count 1-50: about 10 a square km, 30 for a 2 x 1.5 km city; 50 when dense). A run spawns their trips itself, on top of "
    "the OD matrix's demand; there is nothing else to generate for them."
    "\n- \"busy\", \"heavy\", \"rush hour\", \"peak\", \"quiet\" traffic: a level, not a place. generate_demand with "
    "degree_of_saturation: light 0.4, medium 0.65, busy/heavy/peak 0.85, near capacity 0.95. Hourly is the default; "
    "a whole day is demand \"daily\" then shape_daily_curve. \"Busier\", \"quieter\", \"30% more\", \"double it\": "
    "scale_demand on the demand there is (factor 1.3, 0.7, 1.3, 2), not a new build."
    "\n- A real named place (\"Dhanmondi, Dhaka\", \"Times Square\"): place_map with the place, then "
    "network_from_background, then demand. build_scenario does all of a study in one call (place or grid, traffic "
    "level, signals, buses, what-ifs, reports): use it when the user describes a whole study."
    "\n- \"use SCOOT\", \"switch the signals to Webster\", \"make 2,2 actuated\": set_signal_timing with that "
    "method (the junctions named, else every signalised one from list_junctions). optimise_signals is only for "
    "tuning fixed-time greens by many test runs, when the user asks to optimise them."
    "\n- \"simulate\", \"test it\", \"how does it perform\": run_simulation (30 min unless said), then read_results "
    "or analyse_run."
    "\n- \"would X help\", \"compare\", \"what if\": never edit the open network to test. note_plan (goal, "
    "hypothesis), then run_experiment with the network as built first and each other scenario one change "
    "(changes: edit tools applied to a copy; demand_scale to stress it), 3 or more seeds; then read_results and "
    "note_plan with your conclusion. Call a difference only where the comparison says clear; where "
    "replications_needed is above the seeds run, say more seeds are needed."
    "\n- \"why is junction X slow\": run_simulation if there is no run yet, then diagnose_signal (a signal) or "
    "junction_los."
    "\n- \"more people\", \"increase the population\": there is no population figure. With institutions "
    "(list_institutions), update_institution raises their people; else scale_demand. Say which you used and that "
    "trips, not residents, are what changed."
    "\n- Things the app doesn't have (a metro, a harbour, an airport, a stadium, a university, weather on the "
    "open network, deaths): say so in one line. Don't put something else in their place, and don't call a tool "
    "that doesn't exist. When a number is past a tool's limit, tell the user the limit and do what fits only once "
    "they agree."
    "\n- A question about an idea (\"what is a level of service?\"): answer from the documents, no tools."
    "\n\nGrid junction '1,1' is the bottom-left one; positions are metres with x to the right and y downward. Name "
    "only OD pairs the matrix has (read_od_matrix), none for every pair. While the user's run goes, read_traffic shows "
    "it live; a demand change reaches it from its next simulated minute, a network change stops it. Ground "
    "assumptions in data (generate_demand's mean_trip_km, choose_modes for a mode split). Report the workbook file "
    "a test wrote so the user can hand it on."
)


# One record per model request this session, newest last, for View Summary > AI Usage:
# provider, model, at (epoch s), first_s (time to the first words), total_s, and the
# token counts the provider reported (None when it didn't).
# ponytail: kept for this run only; persist it if usage history across sessions matters.
USAGE: list[dict] = []
_last = threading.local()  # each thread's last request record (last_usage)
# What this thread's requests are for (`USAGE_TAG.value`, set by the asker: AI Mode's step), kept in each record as
# "tag", so a step counts its own requests, not ones that ended in its time from another thread (signal agent)
USAGE_TAG = threading.local()


class AssistantError(Exception):
    """A readable reason the model couldn't answer."""


class Cancelled(AssistantError):
    """The user stopped the answer, or started a new conversation."""


class NoAnswer(AssistantError):
    """The model sent nothing: no words, no tool call (a thinking model can stop after its thinking)."""


class ToolsUnsupported(AssistantError):
    """This local model can't call tools; chat without them."""


README_NOTE = ("\n\nREADME.md (setup, keys, the editor's controls, the Research Log) isn't included here: call "
               "read_source with path README.md when the user asks how to use the app.")


def system_prompt(tools: bool = True) -> str:
    """The instructions plus the documents in full: both without tools; with tools only ARCHITECTURE.md (README.md
    is a quarter of every request's input, and read_source fetches it when a question needs it)."""
    parts = []
    for name in DOCS if not tools else DOCS[1:]:
        try:
            parts.append(f"<document name=\"{name}\">\n{(ROOT / name).read_text(encoding='utf-8')}\n</document>")
        except OSError as exc:
            raise AssistantError(f"Could not read {name} next to the app: {exc}") from exc
    return (
        "You are the assistant built into ACMS (Agentic Context-Aware Mobility System). Answer the "
        "user's questions about the app (what it can do, how to use it, how the code works "
        "and where its engineering methods come from) using the documents below. Name the "
        "module, function or source you rely on. If the documents don't cover something, "
        "say so instead of guessing. Quote formulas, numbers and sources exactly as the "
        "documents give them; never make up a formula. Keep answers short unless asked for detail."
        + EDITOR_NOTE + (TOOLS_NOTE + README_NOTE if tools else "") + "\n\n" + "\n\n".join(parts)
    )


def _local_url(variable: str, port: int, path: str) -> str:
    host = os.environ.get(variable, "").strip() or f"127.0.0.1:{port}"
    parts = urlsplit(host if "://" in host else f"http://{host}")
    name = parts.hostname if parts.hostname not in (None, "", "0.0.0.0") else "127.0.0.1"
    return f"{parts.scheme}://{name}:{parts.port or port}{path}"


def ollama_url(path: str) -> str:
    """URL on the local Ollama server (OLLAMA_HOST, default 127.0.0.1:11434)."""
    return _local_url("OLLAMA_HOST", 11434, path)


def laya_url(path: str) -> str:
    """URL on the local Laya server (LAYA_HOST, default 127.0.0.1:8080, laya.exe's own default)."""
    return _local_url("LAYA_HOST", 8080, path)


# --- HTTP ---------------------------------------------------------------------------------

class _Tracking:
    """Handler mixin: each connection's socket goes in `sockets`, so another thread can shut it."""

    def __init__(self, sockets: list):
        super().__init__()
        self.sockets = sockets

    def do_open(self, http_class, req, **kwargs):
        sockets = self.sockets

        class Tracked(http_class):
            def connect(self):
                super().connect()
                sockets.append(self.sock)

        return super().do_open(Tracked, req, **kwargs)


class _TrackingHTTP(_Tracking, urllib.request.HTTPHandler):
    pass


class _TrackingHTTPS(_Tracking, urllib.request.HTTPSHandler):
    pass


def _cut_when(cancelled, sockets: list, finished: threading.Event) -> None:
    """Shut the request's sockets once it's cancelled: a read waiting on a silent server (a model
    still loading or stalled) ends now, and the server drops the request instead of finishing it."""
    while not finished.wait(0.2):
        if cancelled():
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def _open(url: str, payload: dict | None, headers: dict | None, timeout_s: float, local: bool,
          sockets: list | None = None):
    """The open response to a GET (or a POST of `payload`); errors become AssistantError.
    `sockets` collects the connection's sockets (_cut_when)."""
    request = urllib.request.Request(url, headers={"Content-Type": "application/json", **(headers or {})},
                                     data=None if payload is None else json.dumps(payload).encode())
    # Local calls skip any proxy: the server is on this machine or the local network.
    handlers = [_TrackingHTTP(sockets), _TrackingHTTPS(sockets)] if sockets is not None else []
    opener = urllib.request.build_opener(*handlers, *([urllib.request.ProxyHandler({})] if local else []))
    try:
        return opener.open(request, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            error = json.loads(body).get("error", body)
            body = error.get("message", body) if isinstance(error, dict) else str(error)
        except (ValueError, AttributeError):
            pass
        raise AssistantError(f"{urlsplit(url).netloc} replied {exc.code}: {body.strip()[:300]}") from exc
    except Exception as exc:  # noqa: BLE001 - mapped to a readable message
        raise _network_error(url, exc, timeout_s) from exc


def _network_error(url: str, exc: Exception, timeout_s: float) -> AssistantError:
    host = urlsplit(url).netloc
    if isinstance(exc, TimeoutError):
        return AssistantError(f"No reply from {host} within {timeout_s:g} s.")
    if isinstance(exc, (urllib.error.URLError, OSError)):
        return AssistantError(f"Could not reach {host}: {getattr(exc, 'reason', exc)}")
    return AssistantError(f"{host}: {exc}")


def _call(url: str, payload: dict | None = None, headers: dict | None = None,
          timeout_s: float = API_TIMEOUT_S, local: bool = False) -> dict:
    """One JSON request and its JSON reply."""
    with _open(url, payload, headers, timeout_s, local) as reply:
        try:
            return json.load(reply)
        except ValueError as exc:
            raise AssistantError(f"{urlsplit(url).netloc} sent a reply that isn't JSON.") from exc
        except OSError as exc:
            raise _network_error(url, exc, timeout_s) from exc


def _events(url: str, payload: dict, headers: dict | None = None, timeout_s: float = API_TIMEOUT_S,
            local: bool = False, cancelled=None):
    """The JSON events of a streamed reply, server-sent events (data: ...) or one JSON per line.
    `cancelled()` stops it within 0.2 s, even while the server sends nothing (_cut_when)."""
    sockets, finished = [], threading.Event()
    if cancelled:
        threading.Thread(target=_cut_when, args=(cancelled, sockets, finished), daemon=True).start()
    limit = timeout_s
    try:
        with _open(url, payload, headers, timeout_s, local, sockets) as reply:
            for raw in reply:
                if limit > STALL_TIMEOUT_S:  # the first words came: a later silence is a stall, not a slow start
                    limit = STALL_TIMEOUT_S
                    for sock in sockets:
                        try:
                            sock.settimeout(limit)
                        except OSError:
                            pass
                if cancelled and cancelled():
                    raise Cancelled("Stopped.")
                line = raw.decode("utf-8").strip()
                if not line or line.startswith((":", "event:")):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    return
                event = json.loads(line)
                if isinstance(event, dict) and event.get("error"):
                    error = event["error"]
                    raise AssistantError(error.get("message", str(error)) if isinstance(error, dict) else str(error))
                yield event
    except (AssistantError, ValueError, OSError, http.client.HTTPException) as exc:
        if cancelled and cancelled():  # whatever the cut socket raised: it was the user's Stop
            raise Cancelled("Stopped.") from exc
        if isinstance(exc, AssistantError):
            raise
        if isinstance(exc, ValueError):
            raise AssistantError(f"{urlsplit(url).netloc} sent a reply that isn't JSON.") from exc
        raise _network_error(url, exc, limit) from exc
    finally:
        finished.set()
    if cancelled and cancelled():  # the cut socket read as the end of the reply
        raise Cancelled("Stopped.")


# --- Models ------------------------------------------------------------------------------

def ollama_loaded(timeout_s: float = 1.0) -> list[dict] | None:
    """Models Ollama has in memory now (name, size, size_vram in bytes, context_length);
    None when it isn't running."""
    try:
        return list(_call(ollama_url("/api/ps"), timeout_s=timeout_s, local=True).get("models", []))
    except (AssistantError, TypeError):
        return None


def ollama_models(timeout_s: float = 1.0) -> list[str] | None:
    """Models pulled into the local Ollama server; None when it isn't running."""
    try:
        reply = _call(ollama_url("/api/tags"), timeout_s=timeout_s, local=True)
        return sorted(model["name"] for model in reply.get("models", []))
    except (AssistantError, KeyError, TypeError):
        return None


def supports_tools(model: str) -> bool:
    """Whether this local Ollama model can call tools (phi3, orca-mini... can't). If Ollama
    doesn't say, assume it can; stream_chat raises ToolsUnsupported if it then refuses."""
    try:
        capabilities = _call(ollama_url("/api/show"), {"model": model}, timeout_s=10, local=True).get("capabilities")
    except AssistantError:
        return True
    return capabilities is None or "tools" in capabilities


def list_models(provider: str, key: str = "") -> list[str]:
    """Chat models this key (or the local Ollama) offers."""
    if provider == "local":
        models = ollama_models()
        if models is None:
            raise AssistantError("Ollama isn't running on this device. Start it, then pick Local again.")
        return models
    if provider == "grok":
        names = [model["id"] for model in _call(f"{XAI_URL}/models", headers={"Authorization": f"Bearer {key}"})
                 .get("data", [])]
    else:
        reply = _call(f"{GEMINI_URL}/models?pageSize=200", headers={"x-goog-api-key": key})
        names = [model["name"].removeprefix("models/") for model in reply.get("models", [])
                 if "generateContent" in model.get("supportedGenerationMethods", [])]
    return [name for name in names if not any(word in name for word in NOT_CHAT)]


def default_model(provider: str, models: list[str]) -> str:
    for wanted in PREFERRED.get(provider, ()):
        for name in models:
            if wanted in name:
                return name
    return models[0] if models else ""


# --- Provider formats --------------------------------------------------------------------

def _openai_messages(messages: list[dict], ollama: bool) -> list[dict]:
    """xAI (OpenAI format) or Ollama messages."""
    out = []
    for message in messages:
        if message["role"] == "tool":
            out.append({"role": "tool", "content": message["content"],
                        **({"tool_name": message["name"]} if ollama else {"tool_call_id": message["id"]})})
        elif message.get("tool_calls"):
            calls = [{"function": {"name": call["name"], "arguments": call["arguments"]}} if ollama else
                     {"id": call["id"], "type": "function",
                      "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
                     for call in message["tool_calls"]]
            out.append({"role": "assistant", "content": message["content"], "tool_calls": calls})
        else:
            out.append({"role": message["role"], "content": message["content"]})
    return out


# Gemini's function declarations take an OpenAPI subset of JSON Schema and refuse anything else (400 "Unknown
# name"), e.g. additionalProperties: only these keywords go to it, at every level
GEMINI_SCHEMA_KEYS = {"type", "format", "description", "nullable", "enum", "properties", "required", "items",
                      "minItems", "maxItems", "minimum", "maximum", "anyOf", "propertyOrdering"}


def _gemini_schema(schema):
    """A tool's parameter schema with only the keywords Gemini accepts (GEMINI_SCHEMA_KEYS), recursively; an
    empty `required` left out. The other providers get the schema as written."""
    if isinstance(schema, list):
        return [_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    kept = {}
    for key, value in schema.items():
        if key not in GEMINI_SCHEMA_KEYS or (key == "required" and not value):
            continue
        if key == "properties":
            value = {name: _gemini_schema(field) for name, field in value.items()}
        elif key in ("items", "anyOf"):
            value = _gemini_schema(value)
        kept[key] = value
    return kept


def _gemini_request(messages: list[dict], tools: list[dict] | None) -> dict:
    system = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
    contents: list[dict] = []
    for message in messages:
        if message["role"] == "tool":
            response = {"name": message["name"], "response": {"result": json.loads(message["content"])}}
            if message.get("id"):
                response["id"] = message["id"]
            if contents and contents[-1]["role"] == "user" and "functionResponse" in contents[-1]["parts"][0]:
                contents[-1]["parts"].append({"functionResponse": response})  # answers to one turn's calls
            else:
                contents.append({"role": "user", "parts": [{"functionResponse": response}]})
        elif message["role"] == "assistant":
            parts = message.get("raw") or (
                ([{"text": message["content"]}] if message["content"] else [])
                + [{"functionCall": {"name": call["name"], "args": call["arguments"]}}
                   for call in message.get("tool_calls", [])])
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif message["role"] == "user":
            contents.append({"role": "user", "parts": [{"text": message["content"]}]})
    request = {"system_instruction": {"parts": [{"text": system}]}, "contents": contents}
    if tools:
        declarations = []
        for tool in tools:
            declaration = {"name": tool["name"], "description": tool["description"]}
            parameters = _gemini_schema(tool["parameters"])
            if parameters.get("properties"):  # Gemini refuses an object with no properties
                declaration["parameters"] = parameters
            declarations.append(declaration)
        request["tools"] = [{"functionDeclarations": declarations}]
    return request


def _arguments(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {"_unreadable_arguments": str(value)}
    return parsed if isinstance(parsed, dict) else {"_unreadable_arguments": str(value)}


def _chars(message: dict) -> int:
    """What a message adds to a request: its words and its tool calls (a model's thinking isn't sent back)."""
    calls = message.get("tool_calls")
    return len(message.get("content") or "") + (len(json.dumps(calls, default=str)) if calls else 0)


def compact(value):
    """A tool result as the model reads it: a list of three or more records becomes {"columns": [...], "rows":
    [[...]]}, each key once rather than once a row, all the way down. With JSON sent without spaces, a long list
    (list_roads) is about half the characters; the result itself (the Research Log's, the app's) is unchanged."""
    if isinstance(value, dict):
        return {key: compact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        items = [compact(item) for item in value]
        if len(items) >= 3 and all(isinstance(item, dict) for item in items):
            columns = list(dict.fromkeys(key for item in items for key in item))
            return {"columns": columns, "rows": [[item.get(key) for key in columns] for item in items]}
        return items
    return value


def fit(messages: list[dict], tokens: int, tools: list[dict] | None = None) -> list[dict]:
    """`messages` kept small and within about `tokens` with the tools' schemas, so a request never overflows the
    model's window and reads no more than it needs (a local model's time grows with what it reads). Always: tool
    results more than one exchange back become a stub (the replies say what they found; the previous exchange's
    stay whole, for a follow-up). Then, only past the budget: the previous exchange's results become a stub too,
    the oldest exchanges go whole (a user message and everything after it up to the next, so each tool call keeps
    its result), then the current exchange's longest tool results are cut. The system prompt and the latest user
    message always stay; the list passed in is never changed. A request that still doesn't fit is sent as it is."""
    budget = tokens * CHARS_PER_TOKEN - len(json.dumps(tools or []))

    def size(part: list[dict]) -> int:
        return sum(_chars(message) for message in part)

    def stubbed(turn: list[dict]) -> list[dict]:
        return [{**m, "content": STUB} if m["role"] == "tool" and len(m.get("content") or "") > len(STUB) else m
                for m in turn]

    system = [message for message in messages[:1] if message["role"] == "system"]
    rest = messages[len(system):]
    starts = [0] + [i for i, message in enumerate(rest) if message["role"] == "user" and i > 0]
    turns = [rest[a:b] for a, b in zip(starts, starts[1:] + [len(rest)])]
    older = [stubbed(turn) for turn in turns[:-2]] + turns[-2:-1]
    current = list(turns[-1]) if turns else []
    if size(system) + sum(size(turn) for turn in older) + size(current) <= budget:
        light = system + [message for turn in older for message in turn] + current
        return messages if light == messages else light
    older = [stubbed(turn) for turn in older]
    dropped = 0
    while older and size(system) + sum(size(turn) for turn in older) + size(current) > budget:
        older.pop(0)
        dropped += 1
    if dropped and system:
        system = [{**system[0], "content": system[0]["content"] + f"\n\n({dropped} earlier exchange"
                   f"{'s were' if dropped > 1 else ' was'} left out to fit the model's context window.)"}]
    kept = [message for turn in older for message in turn]
    over = size(system) + size(kept) + size(current) - budget
    for i in sorted((i for i, m in enumerate(current) if m["role"] == "tool"), key=lambda i: -len(current[i]["content"])):
        if over <= 0:
            break
        text = current[i]["content"]
        keep = max(len(text) - int(over) - len(CUT), 200)
        if keep < len(text):
            current[i] = {**current[i], "content": text[:keep] + CUT}
            over -= len(text) - keep - len(CUT)
    return system + kept + current


def context_size(tokens: int) -> int:
    """Ollama's num_ctx for about `tokens`: the next of 16k, 32k, 64k... A request with another num_ctx reloads the
    model and drops its cache of the prompt read so far, so a size that only steps up now and then lets every
    round of a conversation reuse the system prompt and documents instead of reading them again (measured: 12k
    input tokens a round before)."""
    size = 16384
    while size < tokens:
        size *= 2
    return size


def stream_chat(provider: str, model: str, messages: list[dict], key: str = "", tools: list[dict] | None = None,
                on_text=None, cancelled=None, think: bool | None = None, require: list[str] | None = None) -> dict:
    """The model's next message (see the module docstring), passing text to `on_text` as it
    streams in. `tools` are network_tools.tool_specs() entries; `cancelled()` stops early. `think` False: an
    Ollama thinking model answers without thinking first (None: as the model does by default). `require`: tool
    names it must call one of, for Gemini (function calling mode ANY) and xAI (tool_choice); Ollama has no such
    option and is only asked."""
    require = [name for name in require or [] if any(tool["name"] == name for tool in tools or [])]
    window = LOCAL_CONTEXT_TOKENS if provider == "local" else API_CONTEXT_TOKENS
    messages = fit(messages, window - REPLY_TOKENS, tools)
    started, first = time.monotonic(), None
    tokens = {"input_tokens": None, "output_tokens": None, "cached_tokens": None}

    def say(piece: str) -> None:
        nonlocal first
        if first is None:
            first = time.monotonic() - started
        if on_text:
            on_text(piece)

    text: list[str] = []
    thoughts: list[str] = []  # the model's reasoning, where it streams it apart from its answer (not shown)
    calls: list[dict] = []
    if cancelled and cancelled():
        raise Cancelled("Stopped.")
    if provider == "gemini":
        raw = []
        request = _gemini_request(messages, tools)
        if require:
            request["toolConfig"] = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": require}}
        for event in _events(f"{GEMINI_URL}/models/{model}:streamGenerateContent?alt=sse",
                             request, {"x-goog-api-key": key}, cancelled=cancelled):
            candidate = (event.get("candidates") or [{}])[0]
            for part in candidate.get("content", {}).get("parts", []):
                raw.append(part)
                if part.get("text") and part.get("thought"):  # a thought summary, when Gemini sends one
                    thoughts.append(part["text"])
                elif part.get("text"):
                    text.append(part["text"])
                    say(part["text"])
                if "functionCall" in part:
                    call = part["functionCall"]
                    calls.append({"id": call.get("id", ""), "name": call.get("name", ""),
                                  "arguments": _arguments(call.get("args"))})
            usage = event.get("usageMetadata")
            if usage:  # thinking tokens are billed as output
                tokens.update(input_tokens=usage.get("promptTokenCount"),
                              output_tokens=usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
                              cached_tokens=usage.get("cachedContentTokenCount"))
            if not raw and event.get("promptFeedback", {}).get("blockReason"):
                raise AssistantError(f"Gemini declined: {event['promptFeedback']['blockReason']}.")
        reply = {"role": "assistant", "content": "".join(text), "tool_calls": calls, "raw": raw}
    elif provider == "grok":
        payload = {"model": model, "messages": _openai_messages(messages, ollama=False), "stream": True,
                   "stream_options": {"include_usage": True}}  # else xAI streams no token counts
        if tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in tools]
        if require:
            payload["tool_choice"] = {"type": "function", "function": {"name": require[0]}} if len(require) == 1 \
                else "required"
        pending: dict[int, dict] = {}
        for event in _events(f"{XAI_URL}/chat/completions", payload, {"Authorization": f"Bearer {key}"},
                             cancelled=cancelled):
            usage = event.get("usage")
            if usage:
                tokens.update(input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                              cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
            for choice in event.get("choices", [])[:1]:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):  # xAI's reasoning models stream it apart
                    thoughts.append(delta["reasoning_content"])
                if delta.get("content"):
                    text.append(delta["content"])
                    say(delta["content"])
                for part in delta.get("tool_calls") or []:  # arguments may arrive in pieces
                    slot = pending.setdefault(part.get("index", len(pending)), {"id": "", "name": "", "arguments": ""})
                    function = part.get("function") or {}
                    slot["id"] = part.get("id") or slot["id"]
                    slot["name"] = function.get("name") or slot["name"]
                    slot["arguments"] += function.get("arguments") or ""
        calls = [{"id": slot["id"] or f"call_{index}", "name": slot["name"], "arguments": _arguments(slot["arguments"])}
                 for index, slot in sorted(pending.items())]
        reply = {"role": "assistant", "content": "".join(text), "tool_calls": calls}
    else:
        # Ollama's default context window is a few thousand tokens and silently cuts off the documents; size it to
        # the request (fit has already kept it within LOCAL_CONTEXT_TOKENS) plus room to reply, never past the cap.
        chars = sum(_chars(message) for message in messages) + len(json.dumps(tools or []))
        size = int(chars / _CHARS_PER_TOKEN_SEEN.get(model, CHARS_PER_TOKEN))
        payload = {"model": model, "messages": _openai_messages(messages, ollama=True), "stream": True,
                   "options": {"num_ctx": min(context_size(size + REPLY_TOKENS), LOCAL_CONTEXT_TOKENS)},
                   "keep_alive": KEEP_ALIVE}
        if tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in tools]
        if think is not None:
            payload["think"] = think
        # The long first-words limit is for loading from disk; a model already in memory gets the hosted one
        loaded = ollama_loaded() or []
        warm = any(model in (entry.get("name"), entry.get("model")) for entry in loaded)
        try:
            for event in _events(ollama_url("/api/chat"), payload, timeout_s=API_TIMEOUT_S if warm else LOCAL_TIMEOUT_S,
                                 local=True, cancelled=cancelled):
                if event.get("done"):
                    tokens.update(input_tokens=event.get("prompt_eval_count"), output_tokens=event.get("eval_count"),
                                  cached_tokens=event.get("prompt_eval_cached_count"),
                                  done_reason=event.get("done_reason"), num_ctx=payload["options"]["num_ctx"])
                    if (event.get("prompt_eval_count") or 0) > size:  # more tokens than estimated: size by this
                        _CHARS_PER_TOKEN_SEEN[model] = chars / event["prompt_eval_count"]
                message = event.get("message") or {}
                if message.get("thinking"):  # a thinking model's reasoning, sent apart from its answer
                    thoughts.append(message["thinking"])
                if message.get("content"):
                    text.append(message["content"])
                    say(message["content"])
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    calls.append({"id": call.get("id") or f"call_{len(calls)}", "name": function.get("name", ""),
                                  "arguments": _arguments(function.get("arguments"))})
        except AssistantError as exc:
            if tools and "does not support tools" in str(exc):
                raise ToolsUnsupported(f"{model} can't use tools.") from exc
            raise
        if tokens.get("done_reason") == "length" and text:  # the window filled mid-reply: say so, not a silent cut
            text.append("\n\n(Cut off: the model's context window is full. Start a new chat, or ask for less.)")
            say(text[-1])
        reply = {"role": "assistant", "content": "".join(text), "tool_calls": calls}
    if thoughts:
        reply["thinking"] = "".join(thoughts)
    if not reply["tool_calls"]:
        del reply["tool_calls"]
    total = time.monotonic() - started
    empty = not reply["content"] and "tool_calls" not in reply
    _record({"provider": provider, "model": model, "at": time.time(), "first_s": first if first is not None
             else total, "total_s": total, **tokens,  # tool calls only: "first" is when they came
             **({"no_answer": True} if empty else {})})  # its time counted too (qwen3.5 thinking 40 s, then nothing)
    if empty:
        raise NoAnswer(f"{PROVIDER_NAMES.get(provider, provider)} sent no answer.")
    return reply


def _record(usage: dict) -> None:
    """One request's figures: this thread's own last (never another thread's, read back from the list: a batch's
    runs ask side by side), then the session's list."""
    usage.setdefault("tag", getattr(USAGE_TAG, "value", None))
    _last.usage = usage
    USAGE.append(usage)
    del USAGE[:-500]


def last_usage() -> dict:
    """The usage record (tokens, timings) of this thread's last model request; {} before any."""
    return getattr(_last, "usage", {})


def chat(provider: str, model: str, messages: list[dict], key: str = "") -> str:
    """The model's text reply to `messages`, without tools."""
    return stream_chat(provider, model, messages, key)["content"].strip()


def portable(messages: list[dict]) -> list[dict]:
    """The conversation as plain user and assistant text, for saving and for another model:
    tool calls become a note on the reply that made them, and their results are dropped
    (they described the network as it was then)."""
    turns: list[dict] = []
    for message in messages:
        if message.get("role") == "user":
            turns.append({"role": "user", "content": message["content"]})
        elif message.get("role") == "assistant":
            text = message.get("content", "")
            if message.get("tool_calls"):
                used = "; ".join(f"{call['name']}({json.dumps(call['arguments'])})" for call in message["tool_calls"])
                text = f"{text}\n(Used tools: {used})".strip()
            if turns and turns[-1]["role"] == "assistant":
                turns[-1]["content"] += "\n" + text
            else:
                turns.append({"role": "assistant", "content": text})
    return turns


def plan_line(texts) -> str:
    """The model's own statement of what it understood: the first "Plan:" line it wrote (TOOLS_NOTE asks for one
    before acting), or "" if it wrote none."""
    for text in texts:
        found = re.search(r"(?im)^[\s*_#>-]*plan[\s*_]*:[\s*_]*(.+)$", text or "")
        if found:
            return found.group(1).strip(" *_")
    return ""


def planned_tools(plan: str, names) -> list[str]:
    """The tools a Plan: line names, in its order: words of it that are tool names (`names`)."""
    known, found = set(names), []
    for word in re.findall(r"[a-z_]{3,}", plan or ""):
        if word in known and word not in found:
            found.append(word)
    return found


def calls_in_text(text: str, tools: list[dict]) -> list[dict]:
    """Tool calls a model wrote as text instead of making them (llama3.1 often does: `{"name": "describe",
    "parameters": {"id": "2,2"}}`): each JSON object naming a tool offered, with its arguments as an object; and each
    shaped as a call (a name and a parameters or arguments object) naming a tool that isn't offered or doesn't exist
    (llama3.2 3B wrote `teleport_vehicles` as its answer), so the app refuses it and the model must answer instead."""
    names, calls, decoder, start = {tool["name"] for tool in tools}, [], json.JSONDecoder(), text.find("{")
    while start != -1:
        try:
            found, end = decoder.raw_decode(text, start)
        except ValueError:
            found, end = None, start + 1
        if isinstance(found, dict) and isinstance(found.get("name"), str):
            shaped = "parameters" in found or "arguments" in found
            arguments = found.get("parameters", found.get("arguments", {}))
            if isinstance(arguments, dict) and (found["name"] in names or
                                                (shaped and re.fullmatch(r"[a-z][a-z0-9_]{2,}", found["name"]))):
                calls.append({"id": f"text{len(calls) + 1}", "name": found["name"], "arguments": arguments})
        start = text.find("{", end)
    for match in re.finditer(r"\b([a-z][a-z0-9_]{2,})\(\s*", text):  # name({...}) or name(): llama3.2 3B wrote
        if match.group(1) not in names:                               # network_from_background({...}) as its reply
            continue
        if text.startswith(")", match.end()):
            arguments = {}
        else:
            try:
                arguments, end = decoder.raw_decode(text, match.end())
            except ValueError:
                continue
            if not isinstance(arguments, dict) or not text[end:].lstrip().startswith(")"):
                continue
        calls.append({"id": f"text{len(calls) + 1}", "name": match.group(1), "arguments": arguments})
    return calls


MALFORMED = ("syntax error", "error parsing", "failed to parse")  # Ollama couldn't read the model's tool call
NUDGE = "(The app: carry on. Call the next tool you planned, or answer the user.)"


def _next_reply(provider, model, messages, key, tools, on_text, cancelled, require=None) -> dict | None:
    """The model's next message; for a local model that sends nothing (all of it in its thinking) or a tool call
    Ollama can't parse (qwen3.5 4B: "XML syntax error"), asked again without thinking, then once more with a
    nudge (left in `messages`). None if it still can't: the caller ends the answer. `require`: tool names it must
    call one of (stream_chat)."""
    attempts = [{}] if provider != "local" else [{}, {"think": False}, {"think": False, "nudge": True}]
    for attempt in attempts:
        if attempt.get("nudge"):
            messages.append({"role": "user", "content": NUDGE})
        try:
            return stream_chat(provider, model, messages, key, tools, on_text, cancelled,
                               **({"think": attempt["think"]} if "think" in attempt else {}),
                               **({"require": require} if require else {}))
        except NoAnswer:
            if provider != "local":
                raise
        except AssistantError as exc:
            if provider != "local" or isinstance(exc, (Cancelled, ToolsUnsupported)) or \
                    not any(word in str(exc) for word in MALFORMED):
                raise
    return None


def converse(provider: str, model: str, messages: list[dict], key: str = "", tools: list[dict] | None = None,
             on_text=None, run_tool=None, cancelled=None, edits=frozenset()) -> list[dict]:
    """`messages` plus the model's answer: its tool calls, their results and its final reply.

    `run_tool(call)` returns a JSON-ready result for {"id", "name", "arguments"}; edits
    wait there for the user's approval. `edits`: the tools that change the project (see `unfinished`).
    """
    messages = list(messages)
    start, reminded, require = len(messages), 0, None
    failed = set()  # calls that came back with an error: made again unchanged, they aren't run (small models loop)
    refusals: dict[str, int] = {}  # per tool: at MAX_REFUSALS it isn't run again in this answer
    outcomes = []  # each call's result: True ran, False refused
    refused, succeeded, retried = {}, set(), False  # tools refused and never run: one reminder to try them again
    claimed = False  # a change claimed with no tool run: reminded once (CLAIMED)
    for _ in range(MAX_TOOL_ROUNDS):
        reply = _next_reply(provider, model, messages, key, tools, on_text, cancelled, require)
        require = None
        if reply is None:  # a local model that kept failing to say anything: end with what was done, not an error
            return _ended(messages, "the model stopped without a reply", on_text, start)
        if "tool_calls" not in reply and tools:
            calls = calls_in_text(reply.get("content") or "", tools)
            if calls:
                reply = {**reply, "tool_calls": calls}
        said = [call for call in reply.get("tool_calls", []) if call["name"] in ANSWER_TOOLS]
        if said and len(said) == len(reply["tool_calls"]):  # its reply sent as a call to an "answer" tool
            text = " ".join(str(value) for call in said for value in (call.get("arguments") or {}).values())
            reply = {key: value for key, value in reply.items() if key != "tool_calls"}
            reply["content"] = (reply.get("content") or "") + text
            if on_text:
                on_text(text)
        messages.append(reply)
        if "tool_calls" not in reply:
            # reminded once; a reply that is only a plan, up to 3 times (each such reminder requires the call)
            plan_only = _only_a_plan(reply.get("content"))
            missing = [] if reminded >= (3 if plan_only else 1) or not tools else \
                unfinished(messages[start:], {tool["name"] for tool in tools}, edits)
            if not missing:
                again = next(((name, error) for name, error in refused.items()
                              if name not in succeeded and refusals.get(name, 0) < MAX_REFUSALS), None)
                if again and not retried:  # llama3.2 3B stopped after one refusal that said exactly what to change
                    retried = True
                    messages.append({"role": "user", "content": RETRY.format(again[0], again[1][:300])})
                    continue
                said = reply.get("content") or ""
                if tools and not outcomes and CLAIM.search(said) and not claimed:  # done, it says; nothing ran
                    claimed = True
                    messages.append({"role": "user", "content": CLAIMED})
                    continue
                note = None
                if tools and not outcomes and CLAIM.search(said):  # reminded, and it still says so
                    note = NOTHING_RAN_NOTE
                elif outcomes and not any(outcomes):  # llama3.2 3B claimed success after every call was refused
                    note = REFUSED_NOTE
                elif outcomes and (unchecked := ungrounded(reply.get("content") or "", messages[start - 1:])):
                    note = UNGROUNDED_NOTE.format(unchecked)  # llama3.2 3B made up a run's results it never had
                if note:
                    reply["content"] = (reply.get("content") or "").rstrip() + "\n\n" + note
                    if on_text:
                        on_text("\n\n" + note)
                return messages
            reminded += 1  # its own plan named tools it never called (the second part of a request, often)
            messages.append({"role": "user", "content": FOLLOW_THROUGH.format(", ".join(missing))})
            if not outcomes or plan_only:  # a plan, then the turn ended (Gemini 3.5 Flash Lite, again when only
                require = missing          # reminded): a call, required
            continue
        for call in reply["tool_calls"]:
            same = (call["name"], json.dumps(call.get("arguments"), sort_keys=True, default=str))
            if same in failed:
                result = {"error": "You made this exact call before and it failed. Change it as that error said, "
                                   "try another tool, or ask the user.", "error_type": "repeated"}
            elif refusals.get(call["name"], 0) >= MAX_REFUSALS:
                result = {"error": f"{call['name']} was refused {MAX_REFUSALS} times in this answer, so it isn't run "
                                   "again now. Tell the user in one or two lines what you couldn't do and why (the "
                                   "last refusal says), or ask them for what's missing.", "error_type": "gave_up"}
            else:
                result = run_tool(call) if run_tool else {"error": "Tools aren't available here."}
                if isinstance(result, dict) and "error" in result:
                    failed.add(same)
                    refusals[call["name"]] = refusals.get(call["name"], 0) + 1
            if call["name"] not in ("find_tools", "note_plan"):  # bookkeeping, not doing
                outcomes.append(not (isinstance(result, dict) and "error" in result))
                if outcomes[-1]:
                    succeeded.add(call["name"])
                else:
                    refused.setdefault(call["name"], str(result.get("error") if isinstance(result, dict) else result))
            messages.append({"role": "tool", "id": call["id"], "name": call["name"],
                             "content": json.dumps(compact(result), separators=(",", ":"), default=str)})
    return _ended(messages, f"it made {MAX_TOOL_ROUNDS} rounds of tool calls without answering, so the app stopped it",
                  on_text, start)


ANSWER_TOOLS = ("answer", "respond", "reply", "final_answer", "say")  # tools small models invent for their reply
FOLLOW_THROUGH = ("(The app: your Plan named {} but you haven't called it. Call it now for that part of the "
                  "request, or say in one line why it isn't needed.)")
REFUSED_NOTE = "(The app: every tool call in this answer was refused, so nothing was changed.)"
# A reply saying a change was made when no tool ran in the answer (qwen3.5 4B, 39 of 190 campaign prompts: asked to
# act, it answered as if it had): reminded once, then noted
CLAIM = re.compile(r"(?i)^\s*\**done\b|\b(?:i(?:'ve| have)|(?:has|have) been|(?:is|are) now)\s+(?:now\s+)?(?:added|set|"
                   r"changed|built|created|updated|applied|placed|generated|removed|deleted|widened|signali[sz]ed|"
                   r"made|ran|run|closed|opened|moved|connected|increased|reduced|scaled)\b")
CLAIMED = ("(The app: your reply says a change was made, but no tool ran in this answer, so nothing changed. Call the "
           "tool now, or tell the user plainly that it wasn't done.)")
NOTHING_RAN_NOTE = "(The app: no tool ran in this answer, so nothing was changed.)"
RETRY = ("(The app: {} was refused: {} Do what that says and call it again, or the tool it points to; or tell the user "
         "in one line why it can't be done.)")
UNGROUNDED_NOTE = ("(The app: {} of the figures in this reply come from no tool result in this answer, so they are "
                   "unchecked; ask for the tool's own numbers.)")


def ungrounded(text: str, answer: list[dict]) -> int:
    """How many of a reply's figures no tool result of this answer (nor the user's request, `answer[0]`) gives,
    within 1 % (rounding); 0 unless at least 3 and at least half of them are, or one is 1,000 or more (not a year):
    a headline figure no tool gave (qwen3.5 4B said 1,269 veh/h could enter where its tool said 3,119 was kept).
    Single digits (a list's numbering) aren't counted, and a few small figures worked out from the results (70 % of
    demand) don't reach it."""
    def figures(value: str) -> list[float]:
        return [float(n.replace(",", "")) for n in re.findall(r"(?<![\w.])\d[\d,]*(?:\.\d+)?", value or "")
                if n.replace(",", "").replace(".", "", 1).isdigit()]
    said = [x for x in figures(text) if x >= 10 or x != int(x)]
    known = [y for m in answer if m.get("role") in ("tool", "user") for y in figures(m.get("content") or "")]
    unsourced = [x for x in said if not any(abs(x - y) <= max(0.01 * abs(y), 0.051) for y in known)]
    big = [x for x in unsourced if x >= 1000 and not (x == int(x) and 1900 <= x <= 2099)]
    return len(unsourced) if big or (len(unsourced) >= 3 and len(unsourced) * 2 >= len(said)) else 0


def _only_a_plan(text: str | None) -> bool:
    """Whether a reply is a Plan: line and nothing else (no answer, no reason a step isn't needed); lines of only
    names and arrows ("network_from_background -> place_map": llama3.2 3B's second plan line) are plan too."""
    rest = re.sub(r"(?im)^[\s*_#>-]*plan[\s*_]*:.*$", "", text or "")
    rest = re.sub(r"(?m)^[\w \t,()\"']*->[\w \t,()\"'>-]*$", "", rest)
    return bool(plan_line([text])) and not rest.strip()


def unfinished(answer: list[dict], names, edits=frozenset()) -> list[str]:
    """Tools the answer's own Plan: lines name (`names`: those it could call) that it never called; every line, not
    only the first (Gemini planned list_institutions, called it, then planned scale_demand and stopped). Once one of
    `edits` has run, a later line's tools don't count, the first line's still do: llama3.2 3B built the network, then
    re-planned with update_roads and add_boundary_end, and reminded, called them with made-up arguments."""
    lines, edited = [], False
    for m in answer:
        if m.get("role") == "assistant":
            found = re.findall(r"(?im)^[\s*_#>-]*plan[\s*_]*:[\s*_]*(.+)$", m.get("content") or "")
            if edited:  # an edit ran: only a first plan still counts
                found = [] if lines else found[:1]
            lines += found
        elif m.get("role") == "tool" and m.get("name") in edits and not (m.get("content") or "").startswith('{"error"'):
            edited = True
    plans = " ".join(lines)
    called = {call["name"] for m in answer if m.get("role") == "assistant" for call in m.get("tool_calls") or []}
    return [name for name in planned_tools(plans, names) if name not in called]


def _ended(messages: list[dict], why: str, on_text=None, start: int = 0) -> list[dict]:
    """The answer ended by the app, not the model: a note of why and of the tools that ran in it (`messages[start:]`:
    not the conversation's earlier answers), as the last reply (the step keeps its record and, through `on_text`, the
    user sees what was done)."""
    done = [m["name"] for m in messages[start:]
            if m.get("role") == "tool" and not m.get("content", "").startswith('{"error"')]
    note = f"(The app: {why}." + (f" What ran: {', '.join(done)}.)" if done else " Nothing ran.)")
    messages.append({"role": "assistant", "content": note})
    if on_text:
        on_text("\n\n" + note)
    return messages


# --- System 1: Jev and Laya ----------------------------------------------------------------
# One API (POST /v1/systemone): a state plus typed questions (noul: P(yes); choice: one of the
# criteria; score: a level), answered in one encoder pass, no text generated. Jev is TypeSafe's
# hosted model; Laya is the same API served by laya.exe on this PC (tens of ms on a GPU).

_laya_process: subprocess.Popen | None = None  # the laya.exe this app started, if any
_laya_key = ""  # the key it was started with: it listens on every interface, so it needs one


def laya_exe(path: str = "") -> str:
    """laya.exe: `path` (LAYA_EXE) if that file exists, else `laya` on PATH; "" if neither."""
    return path if path and Path(path).is_file() else shutil.which("laya") or ""


def laya_health(timeout_s: float = 1.0) -> dict | None:
    """What the Laya server on LAYA_HOST says about itself (device, families); None if none answers."""
    try:
        reply = _call(laya_url("/health"), timeout_s=timeout_s, local=True)
    except AssistantError:
        return None
    return reply if isinstance(reply, dict) and reply.get("status") == "ok" else None


def _cuda_folders() -> list[str]:
    """Folders holding the CUDA 12 runtime that laya.exe's GPU build links to (cudart64_12.dll,
    cublas64_12.dll): Ollama's own copy, or the CUDA toolkit's."""
    ollama = shutil.which("ollama")
    root = Path(ollama).parent if ollama else Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"
    folders = [root / "lib" / "ollama" / "cuda_v12", Path(os.environ.get("CUDA_PATH", "")) / "bin"]
    return [str(folder) for folder in folders if (folder / "cudart64_12.dll").is_file()]


def start_laya(exe: str, timeout_s: float = 60) -> dict:
    """The health of the Laya on LAYA_HOST, first starting `exe serve` there (with the GGUFs in its
    folder) if none answers. One the app starts gets a fresh key and ends with the app (stop_laya)."""
    global _laya_process, _laya_key
    health = laya_health()
    if health:
        return health
    stop_laya()
    if not exe:
        raise AssistantError("laya.exe wasn't found. Save its path as LAYA_EXE (see README), then Rescan PC.")
    key = secrets.token_urlsafe(24)
    environment = {**os.environ, "LAYA_API_KEY": key, "TYPESAFE_API_KEY": key,
                   "PATH": os.pathsep.join([*_cuda_folders(), os.environ.get("PATH", "")])}
    port = urlsplit(laya_url("")).port
    try:
        with open(LAYA_LOG, "w", encoding="utf-8") as log:
            _laya_process = subprocess.Popen(
                [exe, "serve", "--models-dir", str(Path(exe).parent), "--port", str(port)], env=environment,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise AssistantError(f"Couldn't run {exe}: {exc}") from exc
    _laya_key = key
    deadline = time.monotonic() + timeout_s
    while _laya_process.poll() is None and time.monotonic() < deadline:
        health = laya_health(0.5)
        if health:
            return health
        time.sleep(0.2)
    code = _laya_process.poll()
    stop_laya()
    if code is not None and code & 0xFFFFFFFF == 0xC0000135:  # Windows: a DLL it links to is missing
        raise AssistantError("laya.exe needs the CUDA 12 runtime (cudart64_12.dll, cublas64_12.dll): install "
                             "Ollama or the CUDA toolkit, or put them beside laya.exe.")
    try:
        said = " ".join(LAYA_LOG.read_text(encoding="utf-8", errors="replace").split()[-40:])
    except OSError:
        said = ""
    raise AssistantError(("laya.exe stopped" if code is not None else f"Laya wasn't ready within {timeout_s:g} s")
                         + (f": …{said}" if said else "."))


def stop_laya() -> None:
    """End the laya.exe this app started (none started: nothing to do)."""
    global _laya_process, _laya_key
    if _laya_process is not None and _laya_process.poll() is None:
        _laya_process.terminate()
        try:
            _laya_process.wait(5)  # its port is free again
        except subprocess.TimeoutExpired:
            pass
    _laya_process, _laya_key = None, ""


atexit.register(stop_laya)
# ponytail: a hard kill of the app leaves its laya.exe running, holding the port with a key nobody
# knows (requests get 401 until it is ended); give it a Windows job object if that ever bites.


def system_one(provider: str, state, questions: dict, key: str = "", timeout_s: float = 30) -> dict:
    """One System 1 request to Jev (`key`: JEV_API_KEY) or Laya: `state` (text or an object) and
    `questions` {name: {"type": "noul" | "choice" | "score", "instructions", "criteria"}}; the
    reply's "answers" hold each question's probabilities. Recorded in USAGE like a chat."""
    local = provider == "laya"
    key = _laya_key if local else key
    started = time.monotonic()
    reply = _call(laya_url("/v1/systemone") if local else f"{JEV_URL}/systemone",
                  {"model": SYSTEM_ONE_MODEL, "state": state, "questions": questions},
                  {"Authorization": f"Bearer {key}"} if key else None, timeout_s, local)
    if not isinstance(reply.get("answers"), dict):
        raise AssistantError(f"{PROVIDER_NAMES[provider]} sent no answers.")
    total, usage = time.monotonic() - started, reply.get("usage") or {}
    _record({"provider": provider, "model": reply.get("model", SYSTEM_ONE_MODEL), "at": time.time(),
             "first_s": total, "total_s": total, "input_tokens": usage.get("input_tokens"),
             "output_tokens": usage.get("output_tokens"), "cached_tokens": None})
    return reply


def system_one_true(provider: str, question: str, key: str = "") -> bool:
    """A free-text yes/no question as one noul: TRUE when System 1 puts yes at 50 % or more."""
    reply = system_one(provider, question, {"answer": {"type": "noul", "instructions": YES_NO}}, key)
    return float(reply["answers"]["answer"]["noul"]) >= 0.5
