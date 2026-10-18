# ACMS — Agentic Context-Aware Mobility System

## Layout

- `src/traffic_simulator/` — application and domain implementation.
- `config/defaults.json` — editable default network settings.
- `ARCHITECTURE.md` — the index: capabilities, layout, conventions and a map of every module. The in-app
  assistant reads it with every request, so it stays short.
- `docs/architecture/` — each module in full, plus flows, sources, tests and what isn't built yet.
- `AGENTS.md`, `CONTEXT.md` and `stages/` — for coding agents (Interpretable Context Methodology): what always
  holds, which stages a task takes, and for each numbered stage what to read, run and write to its `output/`.
- `extensions/` — drop-in features (Extensions tab). `tools/` — the prompt campaign and the quick test runner.
- `references/` — reference material kept on this PC (books, papers, SUMO notes; only the UI rulebook is in git).
  `reports/` — audits, campaign logs and agent records, also kept on this PC only.

## Setup

```powershell
C:\Python312\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
```

## Run

```powershell
.venv\Scripts\python.exe run.py          # network editor
.venv\Scripts\python.exe run_monitor.py  # test monitor: live progress, Run Again
.venv\Scripts\python.exe tools\run_test.py tests\test_simulation.py webster   # just the matching tests, timed
.venv\Scripts\python.exe tools\run_test.py --all -j 4    # every test file, 4 at a time
.venv\Scripts\python.exe tools\preview_ui.py --output reports/ui-preview  # desktop screenshots and layout record
```

The editor also starts with `.venv\Scripts\acms.exe` or
`python -m traffic_simulator.main`.

## Building and running a network

- **3D View:** the map's tilt icon toggles an orthographic isometric view (45 degree yaw, 35.3 degree elevation,
  zero roll). Wheel zooms; **F** fits the projected network; **R** resets zoom while keeping the chosen view.
  Roads and vehicles keep their world dimensions and simulation speeds. Motion into the screen naturally looks
  slower through foreshortening. Toggle back at any time without restarting the run. The view choice is not saved
  in the project file.
- **Vehicle styling:** cars have tapered bodies, raised cabins, sloped glass and separate wheels in three
  variants. Bicycles and motorbikes have tyres, open frames, handlebars and riders; buses and vans have distinct
  cabs and windows. Both views use the same low-poly geometry. Physical length and width stay unchanged.
  In **Mode Behavior**, click a type and choose its **Colour**. **Random** gives each vehicle stable varied paint
  throughout the run; the choice is saved with the project and supports Undo. Demand charts keep the type colour.
  Choose paint before Run: like other vehicle-type edits, changing it stops an active run.
- **Show Buildings:** the city icon beside 3D View hides/shows placed institutions, their labels and selected
  catchments. It changes visibility only: institutions and their demand remain. Placement previews still show
  while you add a building. Buildings start visible; this display choice lasts for the session.
- **Building heights:** 3D View extrudes the existing metre-scaled footprints into structures with facades and
  roofs. In **Demand Generator → Institutions**, set **Height** when placing or selecting a building.
  Height includes the roof, is saved and undoable, and can be edited during a run. Old files use labelled
  assumed defaults: Residential 9 m, School 7.2 m, Hospital/Office 18 m, Shop 4.5 m. Set a measured height when
  available. Height changes leave 2D footprints, catchments and traffic demand unchanged.
- **Roads:** select a road to set its lanes, speed and direction, and a **turn pocket** at each junction it arrives
  at (kerb or offside, 1–2 lanes, 15–150 m): turners move into it, and a full pocket holds up the lane behind.
  **Slip roads** (Ctrl+click two roads that meet, then **Add Slip Road**) curve smoothly out of and back into their roads, sized so they
  are quicker than the junction they bypass.
- **Background:** once you've drawn the roads over a map or plan, **Hide Background** (the eye beside Lock in
  Create → Background) hides it to show the roads alone; press it again to bring it back. It changes nothing saved.
- **Demand:** an OD pair's hourly flow can take a **PHF** (peak-hour factor, 0.25–1): the hour's busiest
  quarter runs at the flow ÷ PHF. The assistant can also set four 15-minute counts.
- **Run, Pause, Play, Stop:** every run writes its workbook to Documents\ACMS results when it ends, however it
  ends: Stop, a new Run, a network edit, closing the window or a failure. A run that didn't finish normally says
  **Incomplete** on its Run sheet. A crash is recovered at the next start.
- **What a run reports:** delay against the road limit and against each vehicle's own free speed, person delay
  by the people on board, the wait of vehicles not yet able to enter, exact trip and system travel times,
  completion rate, travel time, planning time and buffer indexes, collisions (same lane and crossing) and
  emergency braking. Junction LOS shows each roundabout entry's simulated and HCM 6 capacity.
- **Only through the assistant (not the Run button yet):** a warm-up left out of the figures
  (`measure_from_min`), two-wheelers filtering past queues (`lateral`), and the step method or a shorter step for
  checking results.

## Institutions: trips from buildings

In **Demand Generator → Demand → Institutions**, place homes, a school, a hospital, an office or a shop on the
map (or ask AI Mode: "put a school with 600 students near 2,2 with an 800 m catchment"). A building's **circle
of influence is its catchment**: the junctions and boundary ends inside it are where its people come from, each
an equal share, and its **door** is its nearest junction. Everyone makes one trip each way a day at the shift's
times. Its **Mode split** button works out how they travel: a logit of time and cost on the trips from the
catchment to the door, between Walk and your vehicle types, with editable costs and coefficients; give an
observed share ("30 % come by car") and the model's constants are calibrated to it. Walkers make no vehicle trip,
bus riders ride the buses, and the rest drive (people ÷ occupancy). A Run spawns these trips beside the OD
matrix's: in daily demand over the building's day, in hourly demand at the institutions' busiest hour. AI Mode
calls the same statistics with `place_institution` and `institution_mode_split`.

## AI Mode

### 1. Save your keys as environment variables

System 2 has no key boxes: it reads your API keys from the PC's environment variables.
Save your Grok key as **`GROK_API_KEY`** and your Gemini key as **`GEMINI_API_KEY`**
(you only need the ones you use), once per PC:

```powershell
setx GROK_API_KEY "your-grok-key"
setx GEMINI_API_KEY "your-gemini-key"
```

Or: Windows search → *Edit environment variables for your account* → **New…**, with the
name exactly as above and your key as the value. On macOS/Linux, `export` them in your shell
profile.

System 1 uses Jev (TypeSafe's hosted decision model) with **`JEV_API_KEY`**, or Laya on this PC: save
the path of `laya.exe` (with its `.gguf` beside it) as **`LAYA_EXE`**, e.g.
`setx LAYA_EXE "F:\huggingface_models\laya.exe"`. Its GPU build needs the CUDA 12 runtime, which
Ollama already ships; the app lends it Ollama's copy.

For local models, install [Ollama](https://ollama.com) and pull a model that can use tools,
e.g. `ollama pull llama3.1` (set `OLLAMA_HOST` if Ollama isn't on `127.0.0.1:11434`). A local model's context
window is 32k tokens unless you set **`ACMS_NUM_CTX`** (e.g. `setx ACMS_NUM_CTX 16384` for a 12B model on an
8 GB GPU): each request is trimmed to fit it, so nothing is cut off silently.

### 2. Run the program and pick from the dropdowns

When the program starts, it scans the PC's environment variables and Ollama automatically;
**AI Mode → System 2** then offers only what it found:

- **Chat with**: Off, Grok (xAI), Gemini (Google) and/or Local (Ollama).
- **API model** (Grok, Gemini): the chat models your key can use, listed by the provider.
- **Local model** (Local): the models pulled into Ollama.
- **Start**: nothing is read or sent until you press it. It connects the model you picked;
  once connected, it reads **Restart**.
- **System 1 → Use**: Off, JEV when `JEV_API_KEY` is found, Laya (local) when `laya.exe` is. Picking
  Laya starts it (a few seconds; it ends with the app, or when you pick another).

Saved a key or started Ollama while the program was open? Press **Rescan PC** (the magnifier beside Start on System 2). Keys are
never shown, typed in or saved with your networks, so the app can be copied to another PC;
save the same environment variables there.

### 3. Say what you want: the Scenario Builder

Describe a scenario in your own words and ACMS builds and tests it:

> Build Dhanmondi in Dhaka with heavy rush-hour traffic, buses every 8 minutes, and compare adding a
> lane on the busiest road with a roundabout at the busiest junction. How safe is it?

The model passes what you said to the Scenario Builder (place or grid, traffic level, junctions, signals,
buses, mode and route choice, what-ifs, reports). The app then plans every step with the same tools you have: the network
from OpenStreetMap or a grid, demand sized to the traffic level and checked in a run, mode and route
choice, a baseline run with its statistics, and each what-if as an experiment over several seeds, compared
seed by seed. It shows you the plan, with anything it assumed, as one card. After **Apply** it runs the
whole plan as one undo step and reports the measured numbers in plain words: what's clearly better or worse,
by how much, and what to be careful of. Workbooks go to Documents\ACMS results.

**How the model works out what you mean.** Every model, local or not, chooses and chains the tools itself:
there is no fixed form, so you can ask in your own words, follow up, change your mind or ask something new.
Before acting it writes one line starting **Plan:** saying what it understood and which tools it will call, so
you can see its reading before you approve anything. Each request is offered only the tools its words point to
(a dozen to forty of the 78, and those used in the last two exchanges); it can ask for more with find_tools. Fewer tools
make a small local model choose better and read less: the tools alone are about 16k tokens. The prompt gives it
recipes for what people ask most ("a city of 2 x 1.5 km", "demand from buildings", "busy traffic" as a
level and not a place, what-ifs on a copy). A call that failed isn't made again unchanged, and a thinking model
that ends with nothing to say is asked once more without thinking. The Scenario Builder (build_scenario) is one
of its tools, for when you describe a whole study.

### 4. Chat alongside the assistant

After **Start**, the model warms up by reading the [ARCHITECTURE.md](ARCHITECTURE.md) index
(capabilities and a map of every module; it reads a module's file in `docs/architecture/`, or this README, when it
needs the detail; a model that can't use tools reads this README up front); the question box unlocks once it's
ready, and the status bar shows who's connected, e.g. **Llama is ready**.

- Ask for demand by how busy it should be ("80 % saturated"): it sizes it from the junctions'
  capacity (signals, roundabouts, priority junctions, measured on the simulated drivers), or scales the
  demand you've entered; it then runs it for 45 simulated minutes and cuts it back if traffic piles up.
- While it works, the line above the question box says what it's doing (thinking, writing, waiting
  for your Apply) and for how long.
- Answers stream in; the assistant can look at your network, check its design and propose
  changes, each shown as a card you **Apply** or **Skip** (Ctrl+Z undoes applied ones).
- Each message tells it your edits since its last reply and your map selection.
- Ask about your selection ("explain this junction") or the design ("review my network"): it looks
  them up with its describe and analyse_network tools.
- Ask it to test the network ("does a third lane on 2,2-3,2 cut delay?"): it notes its plan, runs an
  experiment headless over several seeds (after you Apply), and reports the comparison with its
  Excel workbook in Documents\ACMS results, which keeps its planning journal too. Stop ends a test.
- Ask why a junction is slow ("diagnose 2,2"), whether a run can be trusted, how many seeds a comparison
  needs, which design holds up in rain, an incident or a busier peak (conditions, factorial designs), what a
  change is worth (benefit-cost), or for a design audit (Green Book sight lines, turning paths) and what
  residents can reach: each is a tool with its sources (docs/architecture/analysis.md, network_checks.md).
- The row of icons under **Chat with** holds Start, Available Tools, **Rescan PC**,
  **Export History…** (the arrow: saves the chat, everything the assistant observed and its thinking,
  as Markdown or JSON) and **Clear Conversation…** (the bin). Hover a
  button for its name.
- The conversation is saved in the project file; **Clear Conversation…** forgets it.
- A long chat never overflows the model: when a request would not fit its context window, older tool results
  are shortened and the oldest exchanges left out (the saved conversation keeps them all), and a reply cut off
  by a full window says so. It looks up the documents a few lines at a time (`read_source` with `find`).
- A model that goes quiet mid-reply is given up on after 90 s; before its first words it may take up to 3 minutes
  (10 for a local model loading from disk). Stop ends it at once. A local model stays loaded for 30 minutes after
  each question, so a pause doesn't cost a reload.
- **Watch my edits** (the eye in the menu bar, off by default) records everything you do while it's on: every
  click, menu choice and shortcut, drop-down choice, text or number you enter (its final value), tab, map click
  and selection, what each click fills in or shows (a Random pick, a message), and every change to the network,
  demand and vehicle types, each with its time, in
  `activity.csv` in a dated folder, `ACMS activity <date time>`, in Documents\ACMS results (and, with the Research Log on, in each step's
  folder). It only records: the assistant never looks at your edits by itself, it answers when you ask.

Local models need tool support to see or change the network (llama3.1, llama3.2, gemma4…;
phi3 can't). Keep this README, ARCHITECTURE.md and `docs/` next to `src/` when copying the app.
System 1, when chosen, answers the signal agent's TRUE/FALSE questions (tens of ms on Laya) in place of
System 2.

### 5. Record your sessions for research: Research Log

Tick **Research Log** in the Extensions tab and every AI Mode step is recorded in a folder in Documents\ACMS results
(`ACMS research log <date time> <agent>`, e.g. `… 2026-10-04 10-06-29 Ollama llama3.1-8b`): **1** your prompt, **2** what the model understood (its own Plan: line, and the tools it was
offered), **3** what it did (each tool, the plan you approved or skipped, the results, the workbooks it
wrote with their checksums), and **4** a snapshot: the list of changes to the network, demand and vehicle types, and a
picture of what changed: the map if the network did, the OD matrix if the demand did, none if nothing changed. Each step also has its timings (the model's, your deciding, the computer's) and
tokens. `steps.csv` has a row a step for analysis, and `report.html` shows them all. When the session ends, a
document named after its folder (`.odt`, which Word opens) holds all of it to read through like a report. `session.json` records the app's
version and a checksum of its code, so the results can be tied to the code that made them. Keys are never written.

A session can hold many chats. Grade them as you go (the panel's **As intended**, **Partly** and **Not as intended**
grade the latest chat, with a note), or press **End session** when you're done: the next question starts a new
session, and **History** opens on the one you ended. In History, pick any session (from today or another day), click
each chat to see its prompt, what was done, the reply, the changes and the map, and grade it with a note; then rank
the session as a whole the same way. Grades go into each chat's `step.json`, `steps.csv` and `report.html`, the
session's into its `session.json`; `sessions.csv` in the results folder has a row a session (its chats, how many of
each grade, its own grade), for success-rate tables.

### 6. Transit Signal Priority by System 1 or System 2

Tick **Transit Signal Priority (TSP)** in the Extensions tab, then pick who decides each bus's priority
under **Decided by**:

- **System 1**: AI Mode's System 1 (Jev or Laya) reads facts from the telemetry, each with one typed
  question: is the bus's exit lane clear, are more people on the bus than queued on the cross streets? It
  allows priority when both are yes (an encoder reads words well, but not a weighing of numbers in one
  question).
- **System 2**: the model started in AI Mode's System 2, prompted zero-shot as the traffic signal engineer
  on duty, answers by calling the boolean tool `decide_tsp(activate)` (or in words, TRUE or FALSE, if it
  can't call tools).

Both read the same telemetry: each second a bus's request is on, the junction publishes a JSON message
(the request with the bus's distance, speed and expected arrival, the signal phase and timing, each
approach's queue). The agent answers the soonest bus first with the freshest message, and a run never
waits for it (the plan until the answer comes). Each junction keeps its coordinated plan unless priority
actually acts for a bus; once the bus has passed it goes back to the plan the shorter way round (greens cut or
lengthened a little until they end with the plan's). A bus whose exit lane is full gets no priority, and only a
real decision counts: anything else from the model is the plan, counted as rejected. Each run's workbook is
named by its start,
seed and the model that decided (`ACMS results 2026-10-02 21-15-03 seed 42 laya.xlsx`). While it runs, each
junction with a bus request on is ringed on the map: amber and dashed while the agent is asked, green when
priority is granted, grey when refused, red when it fell back to the plan; the TSP panel shows live figures
(requests, questions waiting, latency, errors). Besides passenger
throughput, it reports vehicle wait time, passenger delay and queues, plus a **TSP** sheet and every decision
(**Agent decisions**): latency, tokens, TRUE share, tool calls and fallbacks, with the telemetry feed as a
`.jsonl` file beside it. **Export** includes them too.

Every run's workbook is made for analysis: besides the KPIs it has **Road conditions** (each direction of each road:
flow, speed against the limit, level of service A to F, travel time index, density, delay), **Roads by minute**, a
**City** sheet describing the network (extent, road length by type, speed and lanes, how junctions are controlled,
densities, connectivity indices, curves, roundabouts), every road's geometry and curves, and the statistics of what
the run observed (detectors, link travel times, queue discharge at signals, conflicts and blackspots, bus headways,
reliability). A **GeoJSON** of the network and its results is saved beside it for QGIS or any GIS (in latitude and
longitude when the map is placed). Before any run, **Export** saves the network alone.

**Batch Run** (Extensions tab) runs every seed × agent × time headless. While it goes, the menu bar's clock shows
its run's time (hover it for each run by name; Never preempt is the baseline), the status bar how long is left, and the
map draws that run's vehicles unless you switch off **Show runs on map** (the pin beside Start). Its seeds
override the menu bar's (which reads **Overridden** while Batch Run is ticked). Tick **Test TSP** (TSP needn't be
ticked in the tab), then pick any number of **Models** (a dropdown of ticks: Jev, Laya, your Ollama models,
Grok and Gemini when their keys are saved); each is tested against two baselines,
**Never preempt** (general traffic: the coordinated plan) and **Always preempt** (every request granted), which
always run too, seed by seed, so each model sits between the two ends. **Decide within**
(1–280 simulated s) is each answer's deadline (missed: the plan), and **Pace** (1×, 2×, 5×, 10×) keeps the
runs where a model decides in step with the clock (the baselines, which wait for no one, run as fast as the
computer allows): they never pause, so a slow model pays for its latency (× the pace). **Runs at once** (1–8)
sets how many go side by side (never two on one model server, so none waits behind another's questions; each
run records the pace it kept). Its workbook's **Headline** gives each agent's
passenger throughput increase and its passenger delay, queue and wait time reductions against Never preempt (Always preempt is listed beside them),
with confidence intervals.

Simulated time is the model's own clock (0.5 s a step; the assistant's tests can use 0.25 or 0.1 s): a run's results don't depend on how fast it went, except
for an AI agent's thinking time. Without an agent, a batch runs as fast as the computer allows. The assistant's own
tests (run_simulation, experiments, the Scenario Builder, optimise_signals) also run as fast as the computer allows:
with TSP or DEVL ticked they pause for each answer, and the decision takes effect after the model's real latency in
simulated time, as if the run went at 1×, with no new questions while paused. So they show an agent's effect at 1×
only; to test it at another pace, with traffic moving on while it thinks, use a paced Batch Run (their results say
so in `agent_timing`). **Replay a run** (the icon beside Start Batch) runs one of those workbooks again from
its telemetry feed, every decision at the simulated time it took effect and no model asked: with the same
network and demand open it comes out the same, to the vehicle, and says so.
