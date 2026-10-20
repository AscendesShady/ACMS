"""AI Mode: key discovery and the assistant's provider APIs, against a local fake server.
Run with `python tests/test_ai_mode.py` (or the test monitor)."""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from traffic_simulator import assistant, network_tools
from traffic_simulator.ai_mode import model_label, ollama_models, scan_keys


def test_model_labels_are_short():
    assert [model_label(m) for m in ("llama3.1:8b", "grok-4.20-0309-non-reasoning", "gemini-flash-latest",
                                     "gemma4:12b")] == ["Llama", "Grok", "Gemini", "Gemma"]


def test_keys_come_from_the_environment():
    os.environ["VTS_TEST_KEY"] = "  secret \n"
    try:
        assert scan_keys(["VTS_TEST_KEY", "VTS_TEST_UNSET_KEY"]) == {"VTS_TEST_KEY": "secret"}
    finally:
        del os.environ["VTS_TEST_KEY"]


class _Tags(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"models": [{"name": "phi3:3.8b"}, {"name": "llama3:latest", "capabilities": ["completion"]},
                                      {"name": "laya:421m-english-bf16", "capabilities": ["decision"]}]}).encode()
        self.send_response(200 if self.path == "/api/tags" else 404)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_ollama_models_are_listed_from_its_host():
    server = HTTPServer(("127.0.0.1", 0), _Tags)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port, before = server.server_address[1], os.environ.get("OLLAMA_HOST")
    try:
        for host in (f"127.0.0.1:{port}", f"http://localhost:{port}", f"0.0.0.0:{port}"):
            os.environ["OLLAMA_HOST"] = host
            assert ollama_models() == ["llama3:latest", "phi3:3.8b"], host  # chat models only
            assert ollama_models(capability="decision") == ["laya:421m-english-bf16"], host
        server.shutdown()
        server.server_close()
        assert ollama_models(timeout_s=0.5) is None  # nothing listening: not running
    finally:
        if before is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = before


class _Providers(BaseHTTPRequestHandler):
    """Streams like xAI (/xai, server-sent events), Gemini (/gemini, server-sent events) and
    Ollama (/api, one JSON per line). With tools offered and no tool result yet, each asks for
    the describe tool; otherwise it answers "hi <provider>" in two pieces. System 1 (Jev's /jev/systemone,
    Ollama's /v1/systemone): each noul is P(yes) 0.8 when the state mentions a bus, else 0.1."""
    seen = []  # (path, headers, body); headers match case-insensitively

    def _stream(self, events, sse=True):
        self.send_response(200)
        self.end_headers()
        for event in events:
            line = json.dumps(event)
            self.wfile.write((f"data: {line}\n\n" if sse else f"{line}\n").encode())
        if sse and self.path.startswith("/xai/"):
            self.wfile.write(b"data: [DONE]\n\n")

    def do_GET(self):
        _Providers.seen.append((self.path, self.headers, None))
        body = {"data": [{"id": "grok-4.3"}, {"id": "grok-imagine-image"}]} if self.path == "/xai/models" else \
            {"models": [{"name": "llama3:latest"}, {"name": "laya:421m-english-bf16", "capabilities": ["decision"]}]} \
            if self.path == "/api/tags" else {
            "models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                       {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
                       {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]}]}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Providers.seen.append((self.path, self.headers, body))
        if self.path == "/api/show":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"capabilities": ["completion", "tools"]}).encode())
            return
        if self.headers.get("Authorization") == "Bearer bad":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "Incorrect API key"}}).encode())
            return
        if self.path.endswith("/systemone"):
            yes = 0.8 if "bus" in json.dumps(body["state"]) else 0.1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({
                "model": "jev-1.13.0" if self.path.startswith("/jev/") else body["model"],
                "answers": {name: {"type": "noul", "noul": yes} for name in body["questions"]},
                "usage": {"input_tokens": 12, "output_tokens": 0}}).encode())
            return
        if self.path.startswith("/xai/"):
            wants_tool = body.get("tools") and body["messages"][-1]["role"] != "tool"
            self._stream([{"choices": [{"delta": {"tool_calls": [
                              {"index": 0, "id": "c1", "function": {"name": "describe", "arguments": '{"id": '}}]}}]},
                          {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"2,2"}'}}]}}]}]
                         if wants_tool else
                         [{"choices": [{"delta": {"reasoning_content": "weighing it"}}]},
                          {"choices": [{"delta": {"content": "hi "}}]}, {"choices": [{"delta": {"content": "grok"}}]},
                          {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 2,
                                                     "prompt_tokens_details": {"cached_tokens": 32}}}])
        elif self.path.startswith("/gemini/"):
            wants_tool = body.get("tools") and "functionResponse" not in body["contents"][-1]["parts"][0]
            self._stream([{"candidates": [{"content": {"role": "model", "parts": [
                             {"functionCall": {"name": "describe", "args": {"id": "2,2"}}, "thoughtSignature": "sig"}]}}]}]
                         if wants_tool else
                         [{"candidates": [{"content": {"parts": [{"text": "plan", "thought": True}, {"text": "hi "}]}}]},
                          {"candidates": [{"content": {"parts": [{"text": "gemini"}]}}],
                           "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 3,
                                             "thoughtsTokenCount": 7}}])
        elif body["model"] == "silent":  # thinks, then says nothing
            self._stream([{"message": {"content": "", "thinking": "hmm"}, "done": False},
                          {"message": {"content": ""}, "done": True, "prompt_eval_count": 9, "eval_count": 3}], sse=False)
        elif body["model"] == "no-tools" and body.get("tools"):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({"error": "registry.ollama.ai/library/no-tools does not support tools"}).encode())
        else:
            wants_tool = body.get("tools") and body["messages"][-1]["role"] != "tool"
            deciding = any(tool["function"]["name"] == "decide_tsp" for tool in body.get("tools") or [])
            self._stream([{"message": {"role": "assistant", "content": "", "tool_calls": [
                              {"function": {"name": "decide_tsp", "arguments": {"activate": True, "reason": "bus at red"}}}
                              if deciding else
                              {"function": {"name": "describe", "arguments": {"id": "2,2"}}}]}, "done": False},
                          {"message": {"content": ""}, "done": True}]
                         if wants_tool else
                         [{"message": {"content": "", "thinking": "mulling"}, "done": False},
                          {"message": {"content": "hi "}, "done": False}, {"message": {"content": "ollama"}, "done": False},
                          {"message": {"content": ""}, "done": True, "eval_count": 4,
                           "prompt_eval_count": 100_000 if body["model"] == "dense" else 60}],  # dense: more tokens
                         sse=False)

    def log_message(self, *args):
        pass


def _serve_providers():
    """Point the assistant at a fake server; returns a function that undoes it."""
    server = HTTPServer(("127.0.0.1", 0), _Providers)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    saved = assistant.XAI_URL, assistant.GEMINI_URL, assistant.JEV_URL, os.environ.get("OLLAMA_HOST")
    assistant.XAI_URL, assistant.GEMINI_URL, assistant.JEV_URL = f"{base}/xai", f"{base}/gemini", f"{base}/jev"
    os.environ["OLLAMA_HOST"] = base

    def restore():
        server.shutdown()
        server.server_close()
        assistant.XAI_URL, assistant.GEMINI_URL, assistant.JEV_URL = saved[:3]
        if saved[3] is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = saved[3]
    return restore


def test_gemini_gets_only_the_schema_keywords_it_accepts():
    """Gemini refuses JSON Schema it doesn't know (400 "Unknown name additionalProperties"): every tool's
    parameters go to it with only GEMINI_SCHEMA_KEYS, at every level; the other providers get them as written."""
    request = assistant._gemini_request([{"role": "user", "content": "hi"}], network_tools.tool_specs())
    unknown = set()

    def walk(schema):
        for key, value in schema.items():
            if key not in assistant.GEMINI_SCHEMA_KEYS:
                unknown.add(key)
            if key == "properties":
                for field in value.values():
                    walk(field)
            elif key == "items":
                walk(value)

    declarations = request["tools"][0]["functionDeclarations"]
    for declaration in declarations:
        walk(declaration.get("parameters", {}))
    assert not unknown and len(declarations) == len(network_tools.TOOLS)
    built = next(d for d in declarations if d["name"] == "build_scenario")["parameters"]
    assert built["properties"]["what_if"]["items"]["required"] == ["change"]  # the structure is kept
    assert "required" not in built  # an empty one is left out
    assert network_tools.TOOLS[[t["name"] for t in network_tools.TOOLS].index("build_scenario")][
        "parameters"]["additionalProperties"] is False  # Ollama and xAI still get the strict form


def test_assistant_streams_each_providers_api():
    restore = _serve_providers()
    try:
        prompt = assistant.system_prompt()  # the index in full, plus how to use the tools; README on demand
        readme = "# ACMS — Agentic Context-Aware Mobility System"
        assert "# ACMS Architecture" in prompt and readme not in prompt and assistant.README_NOTE in prompt
        assert readme in assistant.system_prompt(tools=False)  # a model without read_source gets both
        assert assistant.TOOLS_NOTE in prompt and assistant.TOOLS_NOTE not in assistant.system_prompt(tools=False)
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"}, {"role": "user", "content": "roundabouts?"}]

        assert assistant.list_models("grok", "k") == ["grok-4.3"]  # image models left out
        pieces = []
        reply = assistant.stream_chat("grok", "grok-4.3", messages, "k", on_text=pieces.append)
        assert pieces == ["hi ", "grok"] and reply == {"role": "assistant", "content": "hi grok", "thinking": "weighing it"}
        path, headers, body = _Providers.seen[-1]
        assert headers["Authorization"] == "Bearer k" and body["messages"] == messages and body["stream"]

        assert assistant.list_models("gemini", "g") == ["gemini-2.5-flash"]  # speech and embeddings out
        assert assistant.chat("gemini", "gemini-2.5-flash", messages, "g") == "hi gemini"  # thoughts kept apart
        path, headers, body = _Providers.seen[-1]
        assert path == "/gemini/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
        assert headers["x-goog-api-key"] == "g" and body["system_instruction"]["parts"][0]["text"] == prompt
        assert [turn["role"] for turn in body["contents"]] == ["user", "model", "user"]

        assert assistant.chat("local", "llama3:latest", messages) == "hi ollama"
        grok, gemini, local = assistant.USAGE[-3:]  # each request's tokens and timings are recorded
        # Reasoning streamed apart from the answer is kept with the reply (not shown, not sent back)
        assert assistant.stream_chat("gemini", "gemini-2.5-flash", messages, "g")["thinking"] == "plan"
        assert assistant.stream_chat("local", "llama3:latest", messages)["thinking"] == "mulling"
        assert (grok["input_tokens"], grok["output_tokens"], grok["cached_tokens"]) == (40, 2, 32)
        assert (gemini["input_tokens"], gemini["output_tokens"]) == (50, 10)  # thinking counts as output
        assert (local["input_tokens"], local["output_tokens"], local["model"]) == (60, 4, "llama3:latest")
        assistant.USAGE_TAG.value = 7.0  # a step's requests carry its tag; another thread's (signal agent) don't
        assistant._record({"provider": "local"})
        other = threading.Thread(target=assistant._record, args=({"provider": "local"},))
        other.start()
        other.join()
        assistant.USAGE_TAG.value = None
        assert [u["tag"] for u in assistant.USAGE[-2:]] == [7.0, None]
        assert 0 <= local["first_s"] <= local["total_s"]
        path, _, body = _Providers.seen[-1]
        assert path == "/api/chat" and body["stream"] is True
        assert body["options"]["num_ctx"] * 4 > len(prompt)  # the documents fit the context window
        assert body["keep_alive"] == assistant.KEEP_ALIVE  # loaded between questions, not reloaded after 5 min
        sizes = [(assistant.stream_chat("local", "dense", messages), _Providers.seen[-1][2]["options"]["num_ctx"])[1]
                 for _ in range(2)]  # its tokenizer counts more than estimated: the next window is sized by its count
        assert sizes[0] < sizes[1] == assistant.LOCAL_CONTEXT_TOKENS
        try:
            assistant.stream_chat("local", "silent", messages)
        except assistant.NoAnswer:
            assert assistant.USAGE[-1]["no_answer"] and assistant.USAGE[-1]["output_tokens"] == 3  # its time counted
        else:
            raise AssertionError("a reply of nothing is no answer")

        assert assistant.default_model("gemini", ["gemini-2.5-pro", "gemini-flash-latest"]) == "gemini-flash-latest"
        assert assistant.default_model("grok", ["grok-build-0.1", "grok-4.3"]) == "grok-4.3"
        try:
            assistant.chat("grok", "grok-4.3", messages, "bad")
        except assistant.AssistantError as exc:
            assert "401" in str(exc) and "Incorrect API key" in str(exc)
        else:
            raise AssertionError("a rejected key must raise AssistantError")
    finally:
        restore()


def test_a_tool_call_written_as_text_is_made():
    tools = [{"name": "describe"}, {"name": "network_summary"}]
    text = 'I will call describe. {"name": "describe", "parameters": {"id": "2,2"}}'
    assert assistant.calls_in_text(text, tools) == [{"id": "text1", "name": "describe", "arguments": {"id": "2,2"}}]
    assert assistant.calls_in_text('{"name": "network_summary"} {broken', tools)[0]["arguments"] == {}
    assert assistant.calls_in_text("a set {1, 2} and no call", tools) == []
    # a call-shaped object naming a tool that doesn't exist is made too, so the app refuses it (llama3.2 3B wrote
    # {"name": "teleport_vehicles", ...} as its whole answer); a bare {"name": ...} that isn't a tool is just text
    # written as a function call (llama3.2 3B: network_from_background({...}) as its whole reply): made too, offered
    # tools only; a name in prose without its brackets, or brackets that aren't one object, is just text
    called = 'Plan: ... network_from_background({"road_classes": "all", "road_colour": null}) then network_summary()'
    assert [(c["name"], c["arguments"]) for c in assistant.calls_in_text(called, tools + [{"name": "network_from_background"}])] \
        == [("network_from_background", {"road_classes": "all", "road_colour": None}), ("network_summary", {})]
    assert assistant.calls_in_text("I used describe to look, and list_roads(2) too", tools) == []
    invented = '{"name": "teleport_vehicles", "parameters": {"junctions": "1,1"}} {"name": "Bob"}'
    assert [c["name"] for c in assistant.calls_in_text(invented, tools)] == ["teleport_vehicles"]
    replies = iter([{"role": "assistant", "content": text}, {"role": "assistant", "content": "2,2 is a roundabout."}])
    saved, assistant.stream_chat = assistant.stream_chat, lambda *args: next(replies)
    try:  # llama3.1 wrote the call as text: it is made, and the answer goes on
        calls = []
        messages = assistant.converse("local", "m", [{"role": "user", "content": "what is 2,2?"}], "", tools,
                                      None, lambda call: calls.append(call) or {"control": "roundabout"})
        assert [c["name"] for c in calls] == ["describe"] and messages[-1]["content"] == "2,2 is a roundabout."
    finally:
        assistant.stream_chat = saved


def test_the_conversation_loop_recovers_from_small_models_habits():
    call = {"id": "c1", "name": "describe", "arguments": {"id": "9,9"}}
    asked = []

    def replies():
        yield {"role": "assistant", "content": "", "tool_calls": [call]}
        yield {"role": "assistant", "content": "", "tool_calls": [call]}  # the same failing call again
        raise assistant.NoAnswer("Ollama sent no answer.")  # it thought, then said nothing
        yield None

    stream = replies()

    def fake(*args, **kwargs):
        asked.append(kwargs.get("think"))
        if kwargs.get("think") is False or "was refused" in args[2][-1]["content"]:  # reminded once: it says why again
            return {"role": "assistant", "content": "There is no junction 9,9."}
        return next(stream)

    saved, assistant.stream_chat = assistant.stream_chat, fake
    try:
        ran = []
        messages = assistant.converse("local", "m", [{"role": "user", "content": "what is 9,9?"}], "", [{"name": "describe"}],
                                      None, lambda c: ran.append(c) or {"error": "Unknown id '9,9'."})
        assert len(ran) == 1  # the repeat isn't run: it is told so
        assert json.loads(messages[4]["content"])["error_type"] == "repeated"
        assert False in asked  # asked again, not thinking (then reminded of the refusal once)
        assert messages[-1]["content"] == "There is no junction 9,9.\n\n" + assistant.REFUSED_NOTE  # its one call refused
    finally:
        assistant.stream_chat = saved

    def broken(*args, **kwargs):  # Ollama can't parse its tool call, then it says nothing, nudged or not
        asked.append(kwargs.get("think"))
        if len(asked) == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "g", "name": "describe", "arguments": {}}]}
        if len(asked) == 2:
            raise assistant.AssistantError("Ollama: XML syntax error on line 4: element <function> closed by </parameter>")
        raise assistant.NoAnswer("Ollama sent no answer.")

    asked.clear()
    assistant.stream_chat = broken
    try:
        messages = assistant.converse("local", "m", [{"role": "user", "content": "describe"}], "", [{"name": "describe"}],
                                      None, lambda c: {"id": "2,2"})
        assert asked == [None, None, False, False] and messages[-2]["content"] == assistant.NUDGE
        assert messages[-1]["content"] == "(The app: the model stopped without a reply. What ran: describe.)"
        try:
            assistant.converse("grok", "m", [{"role": "user", "content": "x"}], "", None, None, None)
        except assistant.AssistantError:
            pass  # only a local model is asked again
        else:
            raise AssertionError("an API model's error is the answer's error")
    finally:
        assistant.stream_chat = saved
    loops = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "answer",
                                                                  "arguments": {"text": "LOS grades delay A to F."}}]}]
    assistant.stream_chat = lambda *args, **kwargs: loops[0]  # qwen3.5 4B replied by calling an "answer" tool
    try:
        messages = assistant.converse("local", "m", [{"role": "user", "content": "what is LOS?"}], "",
                                      [{"name": "describe"}], None, lambda c: {"error": "no such tool"})
        assert messages[-1] == {"role": "assistant", "content": "LOS grades delay A to F."}
        n = [0]

        def busy(*args, **kwargs):  # a call every round, never an answer
            n[0] += 1
            return {"role": "assistant", "content": "", "tool_calls": [{"id": str(n[0]), "name": "describe",
                                                                         "arguments": {"id": str(n[0])}}]}

        assistant.stream_chat = busy
        messages = assistant.converse("local", "m", [{"role": "user", "content": "x"}], "", [{"name": "describe"}],
                                      None, lambda c: {"id": c["arguments"]["id"]})
        assert n[0] == assistant.MAX_TOOL_ROUNDS and messages[-1]["content"].startswith("(The app: it made 12 rounds")
    finally:
        assistant.stream_chat = saved
    # a two-part request: the model plans both, does the first, answers; its own plan reminds it of the second, once
    tools = [{"name": "list_junctions"}, {"name": "update_roads"}]
    script = iter([
        {"role": "assistant", "content": "Plan: count the junctions, set every road to 40 km/h -> list_junctions, "
                                         "update_roads", "tool_calls": [{"id": "1", "name": "list_junctions", "arguments": {}}]},
        {"role": "assistant", "content": "You have 16 junctions."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "2", "name": "update_roads",
                                                              "arguments": {"roads": "all", "speed_limit_kmh": 40}}]},
        {"role": "assistant", "content": "16 junctions; every road is now 40 km/h."}])
    assistant.stream_chat = lambda *args, **kwargs: next(script)
    try:
        ran = []
        messages = assistant.converse("local", "m", [{"role": "user", "content": "how many junctions, and all roads 40"}],
                                      "", tools, None, lambda c: ran.append(c["name"]) or {"ok": True})
        assert ran == ["list_junctions", "update_roads"] and messages[-1]["content"].startswith("16 junctions; every")
        assert "update_roads" in messages[4]["content"] and messages[4]["role"] == "user"  # the reminder
        done = iter([{"role": "assistant", "content": "Plan: count them -> list_junctions, update_roads"},
                     {"role": "assistant", "content": "update_roads isn't needed: they're 40 already."}])
        assistant.stream_chat = lambda *args, **kwargs: next(done)
        messages = assistant.converse("local", "m", [{"role": "user", "content": "x"}], "", tools, None, lambda c: {})
        assert messages[-1]["content"].startswith("update_roads isn't needed")  # reminded once, then its word stands
        # a plan and nothing done (Gemini 3.5 Flash Lite sent its Plan: line twice): the reminder requires a call
        asked = []
        plan_only = iter([{"role": "assistant", "content": "Plan: widen it -> update_roads"},
                          {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "update_roads",
                                                                                "arguments": {"roads": ["9,9-9,8"]}}]},
                          {"role": "assistant", "content": "Done: road 9,9-9,8 now has 6 lanes."},
                          {"role": "assistant", "content": "Done: road 9,9-9,8 now has 6 lanes."}])  # reminded: claims again

        def fake(*args, **kwargs):
            asked.append(kwargs.get("require"))
            return next(plan_only)

        assistant.stream_chat = fake
        shown = []
        messages = assistant.converse("gemini", "m", [{"role": "user", "content": "x"}], "", tools, shown.append,
                                      lambda c: {"error": "Unknown id '9,9-9,8'."})
        assert asked == [None, ["update_roads"], None, None]  # required once, on the plan reminder only
        # every call refused, yet it claims success: the app says nothing changed, in the reply and on screen
        assert messages[-1]["content"].endswith(assistant.REFUSED_NOTE) and assistant.REFUSED_NOTE in shown[-1]
        # a read, then a reply that is only a Plan: line (Gemini, asked to raise the population): required too
        asked.clear()
        plan_only = iter([{"role": "assistant", "content": "Plan: look -> list_junctions"},  # a plan, nothing done
                          {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "list_junctions",
                                                                                "arguments": {}}]},
                          {"role": "assistant", "content": "Plan: widen it -> update_roads"},  # a second plan, again
                          {"role": "assistant", "content": "", "tool_calls": [{"id": "2", "name": "update_roads",
                                                                                "arguments": {}}]},
                          {"role": "assistant", "content": "Done."}])
        assistant.converse("gemini", "m", [{"role": "user", "content": "x"}], "", tools, None, lambda c: {"ok": True})
        assert asked == [None, ["list_junctions"], None, ["update_roads"], None]  # each plan-only reply, a call required
        assert assistant._only_a_plan("**Plan:** x -> y") and not assistant._only_a_plan("Plan: x -> y\nNot needed: ok.")
        assert assistant._only_a_plan("Plan: x -> y\ny -> x, y")  # llama3.2 3B's arrow lines are plan too
        assert not assistant._only_a_plan("Plan: x -> y\nThis built it.\ny -> x")
        ended = []
        assistant._ended([], "it stopped", ended.append)
        assert ended == ["\n\n(The app: it stopped. Nothing ran.)"]  # the app's own note reaches the transcript
        earlier = [{"role": "tool", "name": "place_map", "content": "{}"}, {"role": "user", "content": "now demand"}]
        assert assistant._ended(earlier, "it stopped", None, 2)[-1]["content"].endswith("Nothing ran.)")  # this answer's
    finally:
        assistant.stream_chat = saved
    assert assistant.planned_tools("set roads -> list_junctions, update_roads, nope", {"list_junctions", "update_roads"}) \
        == ["list_junctions", "update_roads"]
    assert assistant.plan_line(["Sure.\n**Plan:** generate_grid 6x5, then run_simulation"]) == "generate_grid 6x5, then run_simulation"
    assert assistant.plan_line(["no plan here"]) == ""
    # Ollama's context size steps up rarely, so its cache of the prompt survives from one round to the next
    assert [assistant.context_size(n) for n in (1000, 16384, 16385, 40000)] == [16384, 16384, 32768, 65536]


def test_figures_no_tool_gave_are_flagged():
    asked = {"role": "user", "content": "Run a headless simulation for 20 minutes"}
    refused = [asked, {"role": "tool", "content": '{"error": "There is no demand to test with"}'}]
    made_up = ("The results are: total vehicle-km 123456.7, average speed 36.2 km/h, total travel time 3412.5 min, "
               "average travel time 136.5 min, total stops 1234")  # llama3.2 3B, after its run was refused
    assert assistant.ungrounded(made_up, refused) == 5
    measured = [asked, {"role": "tool", "content": '{"throughput_vph": 5970.4, "demand_vph": 8494, "mean_speed_kmh": 23.81}'}]
    assert assistant.ungrounded("Throughput 5,970 veh/h (70% of 8,494), mean speed 23.8 km/h over 20 minutes", measured) == 0
    assert assistant.ungrounded("1. place the map 2. trace its roads 3. run it", measured) == 0  # numbering isn't figures
    kept = [asked, {"role": "tool", "content": '{"total_now": 3119, "kept_after_check": "38% of 7516"}'}]
    assert assistant.ungrounded("Only 1,269 vehicles per hour could enter (38% of what was asked).", kept) == 1
    assert assistant.ungrounded("Kept 3,119 veh/h (38 %), per the HCM 2000 and FHWA 2019.", kept) == 0  # years aren't
    assert "unchecked" in assistant.UNGROUNDED_NOTE.format(5)


def test_a_refused_call_is_tried_again_once_when_reminded():
    # llama3.2 3B sent arrival_pattern "peak", was told to use degree_of_saturation, and stopped; reminded once, it can
    wrong = {"id": "c1", "name": "generate_demand", "arguments": {"arrival_pattern": "peak"}}
    right = {"id": "c2", "name": "generate_demand", "arguments": {"degree_of_saturation": 0.85}}
    script = iter([{"role": "assistant", "content": "", "tool_calls": [wrong]},
                   {"role": "assistant", "content": "Plan: set the peak demand -> generate_demand"},  # gives up
                   {"role": "assistant", "content": "", "tool_calls": [right]},
                   {"role": "assistant", "content": "Peak-hour demand is set."}])
    sent = []
    saved, assistant.stream_chat = assistant.stream_chat, lambda *args, **kwargs: (sent.append(args[2][-1]), next(script))[1]

    def run(call):
        return {"error": "'peak' is how busy traffic is: give degree_of_saturation"} if "arrival_pattern" in call["arguments"] \
            else {"total_now": 6420}
    try:
        messages = assistant.converse("local", "m", [{"role": "user", "content": "Set the demand for the peak hour."}], "",
                                      [{"name": "generate_demand"}], None, run)
    finally:
        assistant.stream_chat = saved
    reminder = sent[2]["content"]
    assert "generate_demand was refused" in reminder and "degree_of_saturation" in reminder  # the error, quoted
    assert messages[-1]["content"] == "Peak-hour demand is set."  # it ran this time: no note
    assert sum("was refused" in (m.get("content") or "") for m in messages if m["role"] == "user") == 1  # once only


def test_a_change_claimed_with_no_tool_run_is_questioned_then_noted():
    sent = []

    def converse(*replies):
        script = iter({"role": "assistant", "content": text} for text in replies)
        saved, assistant.stream_chat = assistant.stream_chat, \
            lambda *args, **kwargs: (sent.append(args[2][-1]["content"]), next(script))[1]
        try:
            return assistant.converse("local", "m", [{"role": "user", "content": "x"}], "", [{"name": "update_roads"}],
                                      None, lambda call: {"ok": True})
        finally:
            assistant.stream_chat = saved

    messages = converse("I've set every road to 40 km/h.", "Every road has been set to 40 km/h.")
    assert sent[-1] == assistant.CLAIMED and messages[-1]["content"].endswith(assistant.NOTHING_RAN_NOTE)
    messages = converse("Done.", "Sorry: I didn't change anything; say which roads.")
    assert messages[-1]["content"] == "Sorry: I didn't change anything; say which roads."  # owned up: no note
    sent.clear()
    assert converse("There are 16 junctions.")[-1]["content"] == "There are 16 junctions." and len(sent) == 1


def test_tools_a_plan_adds_after_an_edit_ran_are_not_pushed():
    # llama3.2 3B: placed the map, planned, built the network, then re-planned with update_roads (no one asked)
    ok = lambda name: {"role": "tool", "name": name, "content": "{}"}  # noqa: E731
    answer = [{"role": "assistant", "content": "", "tool_calls": [{"name": "place_map"}]}, ok("place_map"),
              {"role": "assistant", "content": "Plan: build it -> place_map, network_from_background"},
              {"role": "assistant", "content": "", "tool_calls": [{"name": "network_from_background"}]},
              ok("network_from_background"),
              {"role": "assistant", "content": "Plan: build it -> network_from_background, update_roads"}]
    names, edits = {"place_map", "network_from_background", "update_roads"}, {"place_map", "network_from_background"}
    assert assistant.unfinished(answer, names, edits) == []  # the first plan is done; the later addition isn't pushed
    assert assistant.unfinished(answer, names) == ["update_roads"]  # told nothing of edits: every line counts
    assert assistant.unfinished(answer[:3], names, edits) == ["network_from_background"]  # the first plan still counts


def test_a_tool_refused_again_and_again_is_not_run_again_in_that_answer():
    # llama3.2 3B called set_end_flows 7 times, each with new wrong arguments, until the round limit
    calls = [{"id": f"c{i}", "name": "set_end_flows", "arguments": {"supply": i}} for i in range(5)]
    script = iter([*({"role": "assistant", "content": "", "tool_calls": [call]} for call in calls),
                   {"role": "assistant", "content": "I couldn't set the flows: no end was named."}])
    saved, assistant.stream_chat = assistant.stream_chat, lambda *args, **kwargs: next(script)
    ran = []
    try:
        messages = assistant.converse("local", "m", [{"role": "user", "content": "x"}], "", [{"name": "set_end_flows"}],
                                      None, lambda call: ran.append(call) or {"error": "set_end_flows needs end."})
    finally:
        assistant.stream_chat = saved
    assert len(ran) == assistant.MAX_REFUSALS  # the rest weren't run, and the model was told why
    assert sum('"gave_up"' in m["content"] for m in messages if m["role"] == "tool") == 5 - assistant.MAX_REFUSALS
    assert messages[-1]["content"].startswith("I couldn't set the flows")


def test_tool_results_reach_the_model_compact():
    roads = {"roads": [{"id": f"r{i}", "lanes": 2, **({"overpass": True} if i == 1 else {})} for i in range(4)], "count": 4}
    sent = assistant.compact(roads)
    assert sent == {"roads": {"columns": ["id", "lanes", "overpass"],
                              "rows": [["r0", 2, None], ["r1", 2, True], ["r2", 2, None], ["r3", 2, None]]}, "count": 4}
    assert roads["roads"][1] == {"id": "r1", "lanes": 2, "overpass": True}  # the result itself is untouched
    assert assistant.compact([{"a": 1}, {"a": 2}]) == [{"a": 1}, {"a": 2}]  # two records stay as they are
    from traffic_simulator.network_generator import generate_network

    big = network_tools.run_tool(generate_network(8, 8), "list_roads", {})
    assert len(json.dumps(assistant.compact(big), separators=(",", ":"))) < len(json.dumps(big)) / 2


def test_a_long_conversation_is_cut_to_fit_the_context_window():
    def exchange(n, result_chars):
        return [{"role": "user", "content": f"ask {n}"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{n}", "name": "list_roads", "arguments": {}}]},
                {"role": "tool", "id": f"c{n}", "name": "list_roads", "content": "r" * result_chars},
                {"role": "assistant", "content": f"answer {n}"}]

    system = {"role": "system", "content": "s" * 3000}
    short = [system, *exchange(1, 100)]
    assert assistant.fit(short, 10000) is short  # within the budget: sent as it is
    three = [system, *exchange(1, 5000), *exchange(2, 5000), {"role": "user", "content": "and now?"}]
    light = assistant.fit(three, 100000)  # room to spare, yet results two exchanges back are a stub: less to read
    results = [m["content"] for m in light if m["role"] == "tool"]
    assert results == [assistant.STUB, "r" * 5000] and len(light) == len(three)  # the previous exchange's stay whole
    messages = [system, *(m for n in range(40) for m in exchange(n, 6000)), {"role": "user", "content": "latest"}]
    copy = json.loads(json.dumps(messages))
    sent = assistant.fit(messages, 2000)  # the stubs alone are too long: the oldest exchanges go too
    assert messages == copy  # the conversation itself is never changed
    assert sum(assistant._chars(m) for m in sent) <= 2000 * assistant.CHARS_PER_TOKEN and len(sent) < len(messages)
    assert sent[-1]["content"] == "latest" and sent[0]["content"].startswith("s" * 3000)
    assert "left out to fit" in sent[0]["content"]  # says what went
    assert all(m["content"] == assistant.STUB for m in sent if m["role"] == "tool")  # older results: a stub
    calls = {c["id"] for m in sent for c in m.get("tool_calls") or []}
    assert calls == {m["id"] for m in sent if m["role"] == "tool"}  # every call kept keeps its result, and back
    huge = [system, *exchange(0, 100000)]  # one result bigger than the window: cut, the call kept
    cut = assistant.fit(huge, 8000)
    assert len(cut) == len(huge) and cut[3]["content"].endswith(assistant.CUT)
    assert sum(assistant._chars(m) for m in cut) <= 8000 * assistant.CHARS_PER_TOKEN


def test_tool_calls_round_trip_for_each_provider():
    restore = _serve_providers()
    tools = [{"name": "describe", "description": "Describe an object.",
              "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
             {"name": "network_summary", "description": "Overview.",
              "parameters": {"type": "object", "properties": {}, "required": []}}]
    try:
        for provider, model in (("grok", "grok-4.3"), ("gemini", "gemini-2.5-flash"), ("local", "llama3:latest")):
            calls, pieces = [], []

            def run_tool(call):
                calls.append(call)
                return {"id": call["arguments"]["id"], "control": "roundabout"}

            start = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "What is 2,2?"}]
            messages = assistant.converse(provider, model, start, "k", tools, pieces.append, run_tool)
            assert [call["name"] for call in calls] == ["describe"] and calls[0]["arguments"] == {"id": "2,2"}, provider
            assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant"], provider
            assert json.loads(messages[3]["content"]) == {"id": "2,2", "control": "roundabout"}
            assert messages[-1]["content"] == "".join(pieces) == f"hi {assistant.PROVIDER_NAMES[provider].lower()}"
            _, _, body = _Providers.seen[-1]  # the request that sent the tool result back
            if provider == "grok":
                assert body["messages"][2]["tool_calls"][0]["function"] == {"name": "describe", "arguments": '{"id": "2,2"}'}
                assert body["messages"][3] == {"role": "tool", "content": messages[3]["content"], "tool_call_id": "c1"}
            elif provider == "gemini":
                assert body["contents"][1]["parts"][0]["thoughtSignature"] == "sig"  # replayed as received
                assert body["contents"][2]["parts"][0]["functionResponse"]["name"] == "describe"
                declarations = body["tools"][0]["functionDeclarations"]
                assert "parameters" not in declarations[1]  # Gemini refuses empty objects
            else:
                assert body["messages"][3]["tool_name"] == "describe"
        # a call required (the plan-only reminder): Gemini's mode ANY, xAI's tool_choice; a name not offered is dropped
        ask = [{"role": "user", "content": "What is 2,2?"}]
        assistant.stream_chat("gemini", "gemini-2.5-flash", ask, "k", tools, require=["describe", "nope"])
        assert _Providers.seen[-1][2]["toolConfig"] == {"functionCallingConfig": {"mode": "ANY",
                                                                                   "allowedFunctionNames": ["describe"]}}
        assistant.stream_chat("grok", "grok-4.3", ask, "k", tools, require=["describe"])
        assert _Providers.seen[-1][2]["tool_choice"] == {"type": "function", "function": {"name": "describe"}}
        assistant.stream_chat("grok", "grok-4.3", ask, "k", tools)
        assert "tool_choice" not in _Providers.seen[-1][2]
        try:
            assistant.converse("local", "no-tools", [{"role": "user", "content": "hi"}], tools=tools)
        except assistant.ToolsUnsupported:
            pass
        else:
            raise AssertionError("a local model without tool support must raise ToolsUnsupported")
        try:
            assistant.converse("grok", "grok-4.3", [{"role": "user", "content": "hi"}], "k", cancelled=lambda: True)
        except assistant.Cancelled:
            pass
        else:
            raise AssertionError("a stopped answer must raise Cancelled")
    finally:
        restore()


class _Silent(BaseHTTPRequestHandler):
    """A model server gone quiet: /before stalls before its reply starts, /during after one event.
    Each waits for the client to hang up and notes it in `dropped`."""
    dropped = []
    timeout = 10

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/during":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"message": {"content": "hi"}}\n')
            self.wfile.flush()
        if self.rfile.read(1) == b"":
            type(self).dropped.append(self.path)

    def log_message(self, *args):
        pass


def test_stop_cuts_a_silent_server():
    server = HTTPServer(("127.0.0.1", 0), _Silent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for path, events in (("/before", 0), ("/during", 1)):
            started, got = time.monotonic(), []
            try:
                for event in assistant._events(f"http://127.0.0.1:{server.server_address[1]}{path}", {}, local=True,
                                               cancelled=lambda: time.monotonic() - started > 0.5):
                    got.append(event)
            except assistant.Cancelled:
                pass
            else:
                raise AssertionError(f"{path}: a stopped answer must raise Cancelled")
            assert len(got) == events and time.monotonic() - started < 2, path  # not the 600 s timeout
        for _ in range(100):  # the server saw each request dropped
            if len(_Silent.dropped) == 2:
                break
            time.sleep(0.05)
        assert _Silent.dropped == ["/before", "/during"]
    finally:
        server.shutdown()
        server.server_close()


class _Stalled(_Silent):
    dropped = []  # its own, so Stop's test counts only its own requests


def test_a_reply_that_stalls_fails_at_the_stall_limit_not_the_start_one():
    server = HTTPServer(("127.0.0.1", 0), _Stalled)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    saved, assistant.STALL_TIMEOUT_S = assistant.STALL_TIMEOUT_S, 0.5
    started, got = time.monotonic(), []
    try:  # one event, then silence: the first words came, so the long start-up limit no longer applies
        for event in assistant._events(f"http://127.0.0.1:{server.server_address[1]}/during", {}, local=True):
            got.append(event)
    except assistant.Cancelled:
        raise AssertionError("nobody stopped it")
    except assistant.AssistantError:
        pass
    else:
        raise AssertionError("a stalled reply must fail")
    finally:
        assistant.STALL_TIMEOUT_S = saved
        server.shutdown()
        server.server_close()
    assert len(got) == 1 and time.monotonic() - started < 5  # not LOCAL_TIMEOUT_S (600 s)


def test_system_one_asks_jev_and_ollama_through_one_api():
    restore = _serve_providers()
    try:
        questions = {"switch": {"type": "noul", "instructions": "Should the phase change?"}}
        reply = assistant.system_one("jev", {"queue": 17}, questions, "k")
        path, headers, body = _Providers.seen[-1]
        assert path == "/jev/systemone" and headers["Authorization"] == "Bearer k"
        assert body == {"model": "jev-latest", "state": {"queue": 17}, "questions": questions}
        assert reply["answers"]["switch"]["noul"] == 0.1
        assert assistant.USAGE[-1]["provider"] == "jev" and assistant.USAGE[-1]["input_tokens"] == 12
        assert assistant.system_one_true("local", "Is a bus waiting on 1W?", model="laya:421m-english-bf16")  # a yes/no as one noul
        assert not assistant.system_one_true("local", "Is the queue on 1W empty?", "k", "laya:421m-english-bf16")
        path, headers, body = _Providers.seen[-1]
        assert path == "/v1/systemone" and "Authorization" not in headers  # Ollama: never a key
        assert body["model"] == "laya:421m-english-bf16" and assistant.USAGE[-1]["model"] == "laya:421m-english-bf16"
        assert body["state"] == "Is the queue on 1W empty?" and body["questions"]["answer"]["type"] == "noul"
        try:
            assistant.system_one("jev", "x", questions, "bad")
        except assistant.AssistantError as exc:
            assert "401" in str(exc) and "Incorrect API key" in str(exc)
        else:
            raise AssertionError("a rejected key must raise AssistantError")
    finally:
        restore()


def test_system_1_answers_the_signal_agent():
    """The real panel: Ollama's decision models are offered in System 1 (and not in System 2), picking one says
    so, and the signal agent's questions then go to System 1 instead of System 2."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from traffic_simulator.main import MainWindow

    restore = _serve_providers()
    app = QApplication.instance() or QApplication([])  # noqa: F841
    window = MainWindow()
    panel = window.ai_mode
    try:
        panel.rescan()
        offered = [panel.system_1.itemData(i) for i in range(panel.system_1.count())]
        assert offered[0] == "" and "laya:421m-english-bf16" in offered and "decision model (Laya)" not in panel.system_1_status.text()
        assert [panel.local_models.itemText(i) for i in range(panel.local_models.count())] == ["llama3:latest"]
        try:
            window._ask_agent("Is a bus waiting?")
        except RuntimeError as exc:  # System 1 Off and no System 2 model started
            assert "every answer is FALSE" in str(exc)
        panel.system_1.setCurrentIndex(panel.system_1.findData("laya:421m-english-bf16"))
        panel.system_1.activated.emit(panel.system_1.currentIndex())
        assert panel.system_1_state.text() == "laya:421m-english-bf16 answers through Ollama."
        assert panel.system_one_for_agents() == ("local", "", "laya:421m-english-bf16")
        assert window._ask_agent("Is a bus waiting?") == "TRUE" and window._ask_agent("Is it empty?") == "FALSE"
        assert any(line.startswith("System 1: laya:421m-english-bf16 (Ollama) · laya:421m-english-bf16 answers") for line in window._summary_inputs()["ai"])
        panel.system_1.setCurrentIndex(0)
        panel.system_1.activated.emit(0)
        assert panel.system_one_for_agents() is None and panel.system_1_state.text() == ""
    finally:
        window.close()
        restore()


def test_a_batch_compares_no_llm_system_1_and_system_2_deciding_tsp():
    """The window's batch with TSP ticked, paced at 60x and never pausing: every seed × agent, each run's workbook
    named by seed and model with its TSP sheets and its telemetry feed beside it, and the batch's comparing each
    agent with No LLM (fake Jev says TRUE to buses; fake Ollama calls decide_tsp(activate=true))."""
    import json
    import time
    from pathlib import Path

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from test_results import read_xlsx
    from traffic_simulator.main import MainWindow

    restore = _serve_providers()
    app = QApplication.instance() or QApplication([])  # noqa: F841
    window = MainWindow()
    try:
        saved_keys = {name: os.environ.get(name) for name in ("GROK_API_KEY", "JEV_API_KEY")}
        os.environ.update(GROK_API_KEY="k", JEV_API_KEY="k")  # keys on this PC: Grok and Jev are offered
        try:
            window.ai_mode.rescan()
        finally:
            for name, saved_key in saved_keys.items():
                if saved_key is None:
                    os.environ.pop(name)
                else:
                    os.environ[name] = saved_key
        labels = [option["label"] for option in window.ai_mode.agent_options()]
        assert labels[:2] == ["Never preempt (baseline)", "Always preempt (baseline)"] and "System 1: Jev" in labels and "System 1: laya:421m-english-bf16" in labels and "System 2: llama3:latest" in labels
        assert "System 2: grok-4" in labels  # the provider's usual model, none started
        window._od_command("set_od_demand", {"pairs": ["1W->1E", "1E->1W"], "demand": "hourly", "value": 300,
                                             "mode_split": [{"mode": "Bus", "percent": 40}, {"mode": "Car", "percent": 60}]})
        tsp = window._tsp()
        assert not tsp.on  # Test TSP runs it, ticked in the Extensions tab or not
        for agents, words in ((["System 9"], "Not found on this PC"),):
            try:
                window.run_batch([1], 3, agents=agents, test_tsp=True)
            except ValueError as exc:
                assert words in str(exc)
            else:
                raise AssertionError("a batch with no model, or one not found, must be refused before it runs")
        chosen = ["Never preempt (baseline)", "Always preempt (baseline)", "System 1: Jev", "System 2: llama3:latest"]
        started = time.monotonic()  # the models only: the baseline always runs, first
        done = window.run_batch([1, 2], 3, 1, agents=chosen[2:], deadline_s=5, speed=60, at_once=4, test_tsp=True)
        took = time.monotonic() - started
        # 3 s a run at 60x, side by side; Jev's two and llama's two one after another: about 6 s, not 18
        assert 2 * 3 * 0.9 <= took < 6 * 3 * 0.8, took
        assert done["runs"] == done["of"] == 8 and tsp.module.tsp_agent.system == "1"  # its choice untouched

        def runs_of(book):
            rows = read_xlsx(book)["Runs"]
            return {(row[1], row[2]): dict(zip(rows[0], row)) for row in rows[1:]}

        together = runs_of(done["file"])
        paces = [run["pace_achieved"] for (agent, _), run in together.items() if "baseline" not in agent]
        assert len(paces) == 4  # 4 at 60x can outrun one process's CPU; the baseline isn't paced at all:
        assert all(run.get("pace_achieved") is None for (agent, _), run in together.items() if "baseline" in agent)
        assert all(0 < pace <= 61 for pace in paces)  # recorded, never above the pace asked
        assert ("note" in done) == any(pace < 0.9 * 60 for pace in paces)  # and falling behind is said
        alone = runs_of(window.run_batch([1, 2], 3, 1, agents=["System 1: Jev"], deadline_s=5, speed=60,
                                         at_once=1, test_tsp=True)["file"])
        for seed in (1.0, 2.0):  # each run its own state: side by side as alone, to the vehicle
            side, own = together[("Never preempt (baseline)", seed)], alone[("Never preempt (baseline)", seed)]
            assert all(side[key] == own[key] for key in ("people_per_h", "mean_trip_delay_s", "queued_mean",
                                                         "tsp_requests", "tsp_greens_cut")), seed
        sheets = read_xlsx(done["file"])
        assert list(sheets)[:5] == ["Read me", "Headline", "Comparison", "Agents", "Runs"]
        headline = [row for row in sheets["Headline"][1:] if row[0] == "System 1: Jev"]
        assert {"Passenger throughput increase", "Passenger delay reduction",
                "Vehicle wait time reduction (per trip)"} <= {row[1] for row in headline}
        agents = {row[0]: row for row in sheets["Agents"][1:]}
        assert set(agents) == set(chosen) and agents["System 1: Jev"][1:4] == ["System 1", "jev", "jev-latest"]
        names = sorted(path.name for path in Path(done["folder"]).glob("*.xlsx"))
        assert len(names) == 8 and any(" seed 1 jev" in name for name in names)
        assert any(" seed 2 llama3-latest" in name for name in names) and any(" seed 2 never" in name for name in names)
        # Seed 2's runs: seed 1's go first, while each model is cold, and at 60x most of their answers miss the deadline
        jev = read_xlsx(next(Path(done["folder"]).glob("* seed 2 jev.xlsx")))
        figures = {row[0]: row[1] for row in jev["TSP"][1:] if len(row) > 1 and row[0]}
        assert figures["Bus requests"] > 0 and figures["Priority given (TRUE)"] > 0 and figures["Model"] == "jev-1.13.0"
        run = {row[0]: row[1] for row in jev["Run"][1:]}
        assert run["Decision deadline (simulated s)"] == 5 and run["Pace (simulated s a real s)"] == 60
        decided = [row for row in jev["Agent decisions"][1:] if str(row[7]).startswith("TRUE")]
        read = jev["Agent decisions"][0].index("Telemetry read")
        assert decided and json.loads(decided[0][read])["schema"] == "acms-tsp-telemetry/1"  # what Jev read
        out = jev["Agent decisions"][0].index("Outcome")
        assert {row[out] for row in jev["Agent decisions"][1:] if len(row) > out} <= set(tsp.module.OUTCOMES)
        feed = [json.loads(line) for line in next(Path(done["folder"]).glob("* seed 2 jev telemetry.jsonl"))
                .read_text(encoding="utf-8").splitlines()]
        kinds = {message["type"] for message in feed}
        assert {"tsp_request", "tsp_decision"} <= kinds <= {"tsp_request", "tsp_decision", "tsp_withdrawn", "tsp_late_answer"}
        assert {"request", "spat", "approaches"} <= set(feed[0])
        decision = next(message for message in feed if message["type"] == "tsp_decision" and not message["fell_back"])
        assert decision["model"] == "jev-1.13.0" and decision["guard"] == "accepted"  # what the agent reported
        llama = read_xlsx(next(Path(done["folder"]).glob("* seed 2 llama3-latest.xlsx")))
        figures = {row[0]: row[1] for row in llama["TSP"][1:] if len(row) > 1 and row[0]}
        assert 0 < figures["Answered by the tool"] <= figures["Bus requests"] - figures["Bus gone before it was asked"]
        assert figures["Priority given (TRUE)"] > 0  # decide_tsp(activate=true), read as data
        assert any(len(row) > 12 and row[11] == "tool" and row[12] == "bus at red" for row in llama["Agent decisions"][1:])
        plain = read_xlsx(next(Path(done["folder"]).glob("* seed 1 never.xlsx")))
        assert {row[0]: row[1] for row in plain["Run"][1:]}["Pace (simulated s a real s)"] == "as fast as the CPU allows"
        assert {row[0]: row[1] for row in plain["TSP"][1:] if len(row) > 1 and row[0]}["Priority given (TRUE)"] == 0  # Never preempt
        always = read_xlsx(next(Path(done["folder"]).glob("* seed 1 always.xlsx")))
        assert {row[0]: row[1] for row in always["TSP"][1:] if len(row) > 1 and row[0]}["Priority given (TRUE)"] > 0
        for name in ("jev", "llama3-latest", "never", "always"):  # replayed from its feed, no model asked: the run again, exactly
            book = next(Path(done["folder"]).glob(f"* seed 1 {name}.xlsx"))
            replayed = window.replay_run(book)
            assert replayed["same"] and f"{name}-replay.xlsx" in replayed["file"], replayed
            first, again = read_xlsx(book), read_xlsx(replayed["file"])
            assert first["Summary"][2:] == again["Summary"][2:] and first["Trips"] == again["Trips"]  # to the vehicle
            columns = [first["Agent decisions"][0].index(column) for column in ("Answer", "Model", "Outcome", "Guard")]
            was, now = ([[row[c] for c in columns if c < len(row)] for row in sheet["Agent decisions"]]
                        for sheet in (first, again))
            assert was == now, (name, [(a, b) for a, b in zip(was, now) if a != b][:4], len(was), len(now))
        last = window.run_batch([3], 1, test_tsp=False)
        assert last["of"] == 1  # without Test TSP: plain runs
        try:
            window.replay_run(next(Path(last["folder"]).glob("*.xlsx")))
        except ValueError as exc:
            assert "telemetry feed beside it" in str(exc)
        else:
            raise AssertionError("a run with no telemetry feed can't be replayed")
    finally:
        window.close()
        restore()


def test_the_guard_takes_only_a_decision():
    """TSP's answers: a decide_tsp call with a boolean activate (or the word, as small models send it), a bare TRUE
    or FALSE, or System 1's P(yes) in 0-1. Anything else is the plan, counted as rejected, never read into."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from traffic_simulator import agent_transit_signal_priority as tsp
    from traffic_simulator.main import MainWindow

    app = QApplication.instance() or QApplication([])  # noqa: F841
    window = MainWindow()
    question = tsp.question_for("2", {"schema": tsp.SCHEMA, "approaches": {},
                                      "request": {"arrives_on": "red", "exit_has_room": True, "passengers": 30}})
    saved = assistant.stream_chat, assistant.system_one

    def call(**arguments):
        return {"content": "", "tool_calls": [{"id": "c", "name": "decide_tsp", "arguments": arguments}]}

    try:
        for reply, answer, guard in ((call(activate=True, reason="red ahead"), True, "accepted"),
                                     (call(activate="false"), False, "accepted"),
                                     (call(activate="maybe"), False, "rejected"),
                                     (call(reason="no activate"), False, "rejected"),
                                     ({"content": " TRUE. "}, True, "accepted"),
                                     ({"content": "call decide_tsp: activate true"}, False, "rejected"),
                                     ({"content": "It is not true that the bus is late"}, False, "rejected")):
            assistant.stream_chat = lambda *args, reply=reply: reply
            found = window._ask_agent(question, {"system": "2", "provider": "local", "model": "m"})
            assert found["answer"] is answer and found["guard"].startswith(guard), (reply, found)
        assert window._ask_agent(question, {"system": "2", "provider": "local", "model": "m"})["via"] == "text"
        # System 1: TRUE when every fact is yes; a probability missing or out of 0-1 is the plan, rejected
        for p_yes, answer, guard in ((0.8, True, "accepted"), (0.2, False, "accepted"), (1.7, False, "rejected"),
                                     ("yes", False, "rejected"), (None, False, "rejected")):
            assistant.system_one = lambda *args, p_yes=p_yes, **kwargs: {"answers": {
                "exit_clear": {"noul": p_yes}, "more_on_bus": {"noul": 0.7}}, "usage": {}}
            found = window._ask_agent(question, {"system": "1", "provider": "local", "model": "laya:421m-english-bf16"})
            assert found["answer"] is answer and found["guard"].startswith(guard), (p_yes, found)
        assert found["guard"] == "rejected: P(yes) for exit_clear was None"
        assistant.system_one = lambda *args, **kwargs: {"answers": {"exit_clear": {"noul": 0.8},
                                                          "more_on_bus": {"noul": 0.3}}, "usage": {}}
        found = window._ask_agent(question, {"system": "1", "provider": "local", "model": "laya:421m-english-bf16"})
        assert found["answer"] is False and found["p_yes"] == 0.3  # the lowest decides
        assert found["reason"] == "exit_clear 0.80, more_on_bus 0.30"
    finally:
        assistant.stream_chat, assistant.system_one = saved
        window.close()


def test_conversations_save_with_the_project_as_plain_text():
    import tempfile
    from pathlib import Path

    from traffic_simulator.network_generator import generate_network
    from traffic_simulator.network_io import load_network, save_network, saved_conversation

    history = [{"role": "user", "content": "What is 2,2?"},
               {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "describe",
                                                                     "arguments": {"id": "2,2"}}], "raw": [{}]},
               {"role": "tool", "id": "c1", "name": "describe", "content": "{}"},
               {"role": "assistant", "content": "A signalised junction."}]
    plain = assistant.portable(history)
    assert plain == [{"role": "user", "content": "What is 2,2?"},
                     {"role": "assistant", "content": '(Used tools: describe({"id": "2,2"}))\nA signalised junction.'}]
    path = Path(tempfile.mkdtemp()) / "project.json"
    network = generate_network(2, 2)
    save_network(network, path, {"messages": plain, "transcript": [["You", "What is 2,2?"]]})
    assert saved_conversation(path)["messages"] == plain and load_network(path).nodes.keys() == network.nodes.keys()
    save_network(network, path)
    assert saved_conversation(path) is None


def test_available_tools_lists_every_tool_before_start():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from traffic_simulator import network_tools
    from traffic_simulator.ai_mode import TOOLS_NOTE
    from traffic_simulator.main import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    panel = window.ai_mode
    assert not panel.prompt.isEnabled() and panel.tools_button.isEnabled()  # nothing started: it still works
    assert panel.start_button.accessibleName() == "Start" and not panel.start_button.isEnabled()  # nothing chosen
    panel.tools_button.click()
    app.processEvents()
    who, listed = panel._shown[-1]
    reads, changes = listed.split("Changes, each waiting for your Apply:")
    assert who == TOOLS_NOTE and listed.startswith(f"{len(network_tools.TOOLS)} tools.")
    for tool in network_tools.TOOLS:
        assert f"**{tool['name']}**(" in (changes if tool["edits"] else reads) and tool["description"] in listed
    assert "**set_junction_control**(" in changes and "**network_summary**()" in reads  # with its parameters
    assert "set_junction_control" in panel.transcript.toPlainText()
    assert panel.conversation_state() is None  # the list isn't part of the saved conversation
    window.close()


def test_a_tool_call_queued_before_stop_never_runs():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from traffic_simulator.ai_mode import _ToolRequest
    from traffic_simulator.main import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    panel = window.ai_mode
    request = _ToolRequest({"id": "old", "name": "network_summary", "arguments": {}})
    worker = threading.Thread(target=panel._relay.tool.emit, args=(panel._session, panel._turn, request))
    worker.start()
    worker.join()  # from the worker's thread: queued for the UI thread, not yet delivered
    panel.stop()
    app.processEvents()
    assert request.done.is_set() and request.result == {"error": "Stopped by the user."}  # answered, not run
    window.close()


def test_system_2_shares_edits_and_keeps_the_conversation():
    """The real chat panel against the fake Ollama: warm-up, editor updates,
    watch mode, save/restore and clear."""
    import time

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from traffic_simulator.main import MainWindow

    restore = _serve_providers()
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    panel = window.ai_mode

    def settle(condition, limit=20.0):
        start = time.time()
        while not condition() and time.time() - start < limit:
            app.processEvents()
            time.sleep(0.01)
        assert condition(), "timed out"

    def idle():
        return panel.prompt.isEnabled() and not panel._running

    def last_question():
        return next(m["content"] for m in reversed(panel.history) if m["role"] == "user")

    try:
        saved_key = os.environ.get("GROK_API_KEY")
        os.environ["GROK_API_KEY"] = "k"  # Grok is offered, against the fake xAI
        try:
            panel.rescan()
        finally:
            if saved_key is None:
                os.environ.pop("GROK_API_KEY")
            else:
                os.environ["GROK_API_KEY"] = saved_key
        system_1 = [panel.system_1.itemText(i) for i in range(panel.system_1.count())]
        assert system_1[0] == "Off" and ("JEV (JEV_API_KEY)" in system_1) == ("JEV_API_KEY" in panel.keys())
        assert "Local (Ollama)" in [panel.provider.itemText(i) for i in range(panel.provider.count())]
        assert "Ollama (1 model)" in panel.found_status.text()
        def requests():  # model requests (POSTs) the fake server has had
            return sum(body is not None for _, _, body in _Providers.seen)

        def pick(provider):
            panel.provider.setCurrentIndex(panel.provider.findData(provider))
            panel.provider.activated.emit(panel.provider.currentIndex())

        before = requests()
        pick("grok")  # lists Grok's models for the dropdown, and nothing more
        settle(lambda: panel.api_model.currentData() == "grok-4.3")
        assert panel.start_button.isEnabled() and panel.start_button.accessibleName() == "Start"
        assert requests() == before and not panel.prompt.isEnabled() and not panel._running
        assert "Grok is selected: press Start" in window.ai_status.text()
        panel.local_models.setCurrentText("llama3:latest")
        pick("local")
        app.processEvents()
        assert requests() == before and not panel._running  # picked, not started
        assert "Llama is selected: press Start" in window.ai_status.text()
        panel.start_button.click()
        assert not panel.prompt.isEnabled()  # gate closed while warming up
        assert "Llama is warming up" in window.ai_status.text()  # shown in the status bar, any tab
        settle(idle)
        assert "Llama is ready" in window.ai_status.text() and "(llama3:latest)" in window.ai_status.text()
        assert panel.start_button.accessibleName() == "Restart"
        assert panel._tools and panel.history == []  # the warm-up check isn't kept

        window.network.nodes["2,2"].control = "roundabout"  # the user edits on the map...
        window.model_updated()
        window.select("node", "2,2")
        panel.prompt.setText("What did I change?")
        panel.send()
        assert "llama3:latest is thinking" in panel.progress_label.text() and not panel.progress.isHidden()
        settle(idle)
        assert panel.progress.isHidden() and panel.progress_label.isHidden()  # the working indicator goes
        question = last_question()
        assert question.startswith("[Editor update]") and question.endswith("What did I change?")
        assert "- 2,2: signalised → roundabout" in question and "Selected on the map: junction 2,2" in question
        assert [m["role"] for m in panel.history] == ["user", "assistant", "tool", "assistant"]  # used describe
        assert any(said.startswith("Shared with the assistant: your 1 change") for who, said in panel._shown if not who)

        panel.prompt.setText("And now?")
        panel.send()
        settle(idle)
        assert "changed the network" not in last_question()  # nothing new since its last reply

        window.watch_action.setChecked(True)  # the menu bar's eye is a logger: the model never looks by itself
        assert "Llama is ready" in window.ai_status.text() and not hasattr(panel, "watch")
        asked = len(panel.history)
        window.network.nodes["3,2"].control = "uncontrolled"
        window.model_updated()
        waited = time.time()
        while time.time() - waited < 2.5:  # longer than the old watch mode's 2 s pause
            app.processEvents()
            time.sleep(0.02)
        assert len(panel.history) == asked and idle()  # nothing sent: the edit waits for the user's next message
        window.watch_action.setChecked(False)

        steps = len(window.history.undo_stack)  # demand tools work the Demand Generator's controls
        done = panel._execute("set_od_demand", {"pairs": ["1W->1E"], "demand": "hourly", "value": 250})
        assert done["value_each"] == 250 and window.od_matrix.cells[("1W", "1E")].flow == 250
        assert window.od_panel.flow.value() == 250 and len(window.history.undo_stack) == steps + 1
        assert "No such origin" in panel._execute("set_od_demand", {"pairs": ["9X->1E"], "demand": "hourly",
                                                                  "value": 1})["error"]
        from traffic_simulator.ai_mode import _ToolRequest  # a bad proposal is refused before asking
        request = _ToolRequest({"id": "x", "name": "set_od_demand",
                                "arguments": {"pairs": ["1W->9Z"], "demand": "hourly", "value": 5}})
        panel._tool_requested(panel._session, panel._turn, request)
        assert request.done.is_set() and panel._pending is None and "No such origin" in request.result["error"]
        request = _ToolRequest({"id": "y", "name": "set_junction_control",
                                "arguments": {"junctions": ["1E"], "control": "roundabout"}})
        panel._tool_requested(panel._session, panel._turn, request)  # network edits are tried on a copy
        assert request.done.is_set() and "not a junction" in request.result["error"]
        assert window.network.approach_nodes["1E"].control == "signalised"  # the real network untouched
        request = _ToolRequest({"id": "z", "name": "set_end_flows", "arguments": {"flows": [{"end": "1W"}]}})
        panel._tool_requested(panel._session, panel._turn, request)  # a call it can't read: refused, saying what it takes
        assert request.done.is_set() and panel._pending is None
        assert "set_end_flows takes end (text, required)" in request.result["error"]
        request = _ToolRequest({"id": "w", "name": "set_od_demand",
                                "arguments": {"pair": "1W->1E", "Demand": "HOURLY", "value": "40"}})
        panel._tool_requested(panel._session, panel._turn, request)  # one it can: pairs, hourly and 40, then asked
        assert panel._pending is request and request.call["arguments"] == {"pairs": "1W->1E", "demand": "hourly",
                                                                           "value": 40.0}
        panel._decide(False)
        state = panel.conversation_state()
        assert all(m["role"] in ("user", "assistant") for m in state["messages"])  # plain text only

        import tempfile
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        assert panel.export_button.isEnabled() and panel.export_folder == window.results_folder
        folder, choose = Path(tempfile.mkdtemp()), QFileDialog.getSaveFileName
        try:  # Export History: the chat, what it observed, its thinking, the journal
            QFileDialog.getSaveFileName = lambda *args: (str(folder / "chat.md"), "Markdown (*.md)")
            panel.export_history()
            text = (folder / "chat.md").read_text(encoding="utf-8")
            assert "## Chat" in text and "What did I change?" in text  # the chat as shown
            assert "[Editor update]" in text and "2,2: signalised → roundabout" in text  # what it was told
            assert "Tool call **describe**" in text and "Result of **describe** (observed)" in text
            assert "> mulling" in text and "## Planning journal" in text  # its thinking, the journal
            QFileDialog.getSaveFileName = lambda *args: (str(folder / "chat"), "JSON (*.json)")
            panel.export_history()
            saved = json.loads((folder / "chat.json").read_text(encoding="utf-8"))
            assert saved["record"] and any(m.get("thinking") == "mulling" for m in saved["record"])
            assert all("raw" not in m for m in saved["record"]) and saved["chat"][0]["speaker"]
        finally:
            QFileDialog.getSaveFileName = choose
        assert panel._shown[-1][1].startswith("History exported to")
        panel.restore_conversation(None)
        assert panel.history == [] and panel._shown == []
        panel.restore_conversation(state)
        assert panel.history == state["messages"] and panel._shown[-1][1].startswith("Conversation restored")

        answer = QMessageBox.question
        QMessageBox.question = lambda *args: QMessageBox.StandardButton.Yes
        try:
            panel.clear_conversation()
        finally:
            QMessageBox.question = answer
        assert panel.history == [] and panel._shown == [] and panel.conversation_state() is None and panel.record == []
    finally:
        panel.provider.setCurrentIndex(0)
        panel._choose()
        assert window.ai_status.text() == ""  # nothing connected: no indicator
        window.close()
        restore()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
