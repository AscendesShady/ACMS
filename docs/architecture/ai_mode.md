# ai_mode.py, assistant.py and network_tools.py: AI Mode

- **Key discovery** (`ai_mode.scan_keys`, run by `AIModePanel.rescan` at start and by **Rescan PC**)
  looks for `JEV_API_KEY`, `LAYA_EXE` (laya.exe's path), `GROK_API_KEY` and `GEMINI_API_KEY`:
  - First in the app's environment.
  - Then, on Windows, in the user and system environment settings (registry). A key set
    after the editor or terminal started is still found.
  - Keys stay in memory only; nothing is saved with networks, so the app can be copied to
    another device.
  - System 2 has **no key boxes**: its keys come only from the environment and are never shown.
    Its **Chat with** dropdown offers only what the scan found:
    - Off;
    - Grok, if `GROK_API_KEY` is set;
    - Gemini, if `GEMINI_API_KEY` is set;
    - Local, if Ollama is running with models.
  - A line above it says what was found and what wasn't.
  - If a rescan no longer finds the chosen one, the chat goes back to Off.
  - System 1 works the same way, with no key box. It says what was found, and its **Use** dropdown
    offers Off, JEV when `JEV_API_KEY` is set, and Laya (local) when `laya.exe` is found (`LAYA_EXE`,
    else `laya` on PATH) or a Laya already answers on `LAYA_HOST`. See **System 1** below.
- **Local models:** `assistant.ollama_models()` lists the models in the local Ollama server
  (`OLLAMA_HOST`, default `127.0.0.1:11434`).
- **System 2 warm-up and prompt gate:**
  - Pick Grok, Gemini or Local under **Chat with**, then the model in **API model** or
    **Local model**. Picking only selects (`_choose`, `_choose_model`). For Grok and Gemini,
    `assistant.list_models` fetches the chat models for the dropdown. That is a list request
    only; the dropdown preselects the provider's last started model or the default.
  - Changing the pick lets go of a connected model (`_disconnect`); the conversation stays.
  - **Available Tools** (a wrench icon beside Start, `show_tools`) lists every tool in the transcript
    from `network_tools.TOOLS`, with its parameters and description: the reads that run at once, then
    the changes that wait for Apply (and, for a model that can't call tools, that it answers from the
    documents). It works before Start and sends nothing; the list isn't saved with the conversation.
  - Nothing is read or prepared until **Start** (`start`). Start then:
    - shows the working indicator (a label and busy bar just above the question box, `_busy`) with
      "Warming up…". While the model works it says what it is doing, "is thinking…" (before its first
      words and after each tool result), "is writing…" (as text streams in) or "Waiting for you to apply
      or skip the change…", with the seconds since it started, ticking each second; it goes when the reply is done;
    - sends the model `system_prompt()`: instructions, how to use the tools, and the full text of the
      `ARCHITECTURE.md` index (module detail from `docs/architecture/` and `README.md` on demand, `read_source`,
      `README_NOTE`; README was a quarter of every request's input). A model without tools gets `README.md` in full too;
    - lets the model confirm it has read them.
  - Only then does the prompt box unlock. Once connected, the button reads **Restart**, which
    also retries after an error.
- **Streaming:** `assistant.stream_chat` shows replies as they arrive.
  - xAI: server-sent events from `POST /v1/chat/completions`.
  - Gemini: `POST /v1beta/models/{model}:streamGenerateContent?alt=sse`. Its function declarations take only an
    OpenAPI subset of JSON Schema, so each tool's parameters go to it with only `GEMINI_SCHEMA_KEYS`, at every
    level (`_gemini_schema`: e.g. no `additionalProperties`, which it refuses with a 400); Ollama and xAI get the
    schemas as written.
  - Ollama: one JSON object per line from `POST /api/chat`, with `num_ctx` sized to fit the documents.
  - Reasoning a model streams apart from its answer is kept on the reply as `thinking` (Ollama's
    `message.thinking`, xAI's `reasoning_content`, Gemini's thought parts when it sends them); it isn't
    shown, sent back or saved with the project, only exported.
- **Stop:** ends an answer at once; that question is left out of the conversation. A model that has gone
  quiet (still loading, or stalled) is cut off too: `assistant._events` shuts the request's socket within
  0.2 s (`_cut_when`), so the app doesn't wait out the timeout and the server drops the request. Without a Stop, a
  reply waits up to `API_TIMEOUT_S` (180 s) for its first words, a local model too when Ollama has it loaded
  (`/api/ps`), else `LOCAL_TIMEOUT_S` (600 s: loading from disk); every local request asks Ollama to keep the
  model loaded `KEEP_ALIVE` (30 min; its own default, 5 min, reloaded it after a pause); once it streams, `STALL_TIMEOUT_S` (90 s) of silence ends it as stalled. The
  worker's streamed text and tool calls carry its session and turn (`_Relay`): one still queued when Stop,
  Restart, Clear or another project changes them is dropped, a tool call answered "Stopped by the user."
  without running, so the worker isn't left waiting.
- **Export History…** (`export_history`): saves, as Markdown (`history_markdown`, to read) or JSON
  (`history_json`), the chat as shown; the record (`AIModePanel.record`: every message of every answer
  this session with its time and model, the user's with the editor update it carried, the model's with
  its thinking and tool calls, and each tool's result, i.e. what the assistant observed; Gemini's raw
  parts left out); and the planning journal. It offers the window's results folder. The record isn't
  saved with the project or made portable on Restart; Clear Conversation empties it.
- **Tools** (`network_tools.py`, no Qt). `assistant.converse` runs the loop: the model asks for a tool,
  the app runs it, the result goes back, and the model continues (up to 12 rounds per answer). A call the model
  wrote as text instead of making it (llama3.1 often does: `{"name": "describe", "parameters": {...}}`) is made
  all the same when it names a tool offered and its arguments are an object (`assistant.calls_in_text`).
  - **Arguments first** (`network_tools.normalise`, in `AIModePanel._tool_requested` for every tool and in
    `run_tool`): what small models send is read in the shapes the schema gives (a close misspelling or a
    plural takes the parameter's name, `ends` → `end`; numbers, whole numbers and true/false sent as text
    become them; a choice matches whatever its case; a one-item list where one value is wanted is that value;
    nulls are left out, and so is text meaning nothing (`_NOTHING`: "None", "null", "n/a"…, which llama3.2
    sends for every optional argument, even ones the tool lacks), a real 0 kept; lists stay as sent). An unknown or missing argument, or a value that can't be read,
    is refused before anything runs, the error giving the tool's signature (`signature`), e.g.
    "set_end_flows takes end (text, required), supply_vph (number), demand_vph (number)".
  - **Edits that change nothing** (`run_tool`: the network equal before and after) are refused ("Nothing
    would change … it is like that already"), so they are never proposed; a network edit's result lists
    what it changed (`changed`, from `describe_changes`). The system prompt tells the model to read every
    result, treat an error as nothing done, and quote the result's own numbers.
  - **Refusals as data** (`network_tools.explain_error`, from `AIModePanel._execute`): every refused call comes
    back as {"error", "error_type", "suggested_fix"}, and for an unknown id `did_you_mean`, the network's nearest
    ids by spelling (network_checks.md), so a model can correct the call without guessing.
  - **Read tools** run straight away:
    - `network_summary`, `list_roads`, `list_junctions`.
    - `describe`: any junction, end or road, including its roundabout design and safe curve speed.
    - `analyse_network`, a design check covering:
      - validity and disconnected parts;
      - junctions traffic can't reach or leave, with one-way roads considered;
      - dead ends and roads under 15 m;
      - curves posted faster than `curve_speed_kmh` allows;
      - roundabouts too big for their roads.
    - `read_source`: the app's own files, limited to the project, a page of whole lines up to `READ_CHARS`
      (6,000 characters) and `READ_LINES` (150) (by lines alone, sources.md's long rows made one page 63.6k); `find` returns the
      lines holding every word (one file, or every readable file with no path; `FIND_HITS` 25) with their numbers,
      so a question reads ~30 lines instead of a page of a 40–66k-character doc.
    - `get_selection` and `show_on_map`: select an object and centre the map on it.
    - `list_institutions`: each institution's kind, place, size, circle, figures, mode shares (and whether a
      logit set them), its door, its catchment and its people a day each way.
    - `read_traffic` (`MainWindow._traffic`): the live run's latest frame as plain data: time,
      counts, each signal's state per arm and plan (method, cycle, greens, offset; Webster's flow ratios, TRANSYT's and Synchro's performance index, SCATS's and SCOOT's live saturation), and each
      vehicle's id, type, position, heading, speed, road, lane or turn, metres along, origin and
      destination, and `heading_to` (the next roundabout, signal or slip road on its route and the metres
      to it); filtered by road, `limit` vehicles (default 100). It reads while the run carries
      on.
    - `note_plan` (`AIModePanel._note_plan`): the assistant's goal, hypothesis, plan, expectation or
      conclusion, into the **planning journal** (`AIModePanel.journal`). Every tool call goes in too
      (`AIModePanel.log`): read, refused before asking (with why), applied or skipped by the user. The
      journal is a sheet of each experiment workbook and of each `run_simulation` workbook, so how the
      agent plans (what it expected, tested, concluded, and what the user let it do) can be studied.
  - **Demand tools** work the Demand Generator's own controls (`MainWindow._od_command` →
    `od_matrix.ODCellPanel.command`), never the data directly, so the user sees values arrive there:
    - `read_od_matrix`: the ends, the demand shown (a Run uses it), and compactly for small models (about
      3,000 characters for 182 pairs): `hourly_vph` and `daily_trips` maps of every pair with data, what most
      pairs share (`usual`: pattern, split, daily curve, hourly pairs' `peak_hour_factor` and `quarters`) once, and only the pairs that differ (`different`).
    - Every demand edit's result says which demand a Run now uses (`runs_use`), with a `note` when the tool
      switched the kind shown (shape_daily_curve shows daily; calibrate_demand hourly). `set_od_demand` and
      `shape_daily_curve` are tried during the check and put back (`ODCellPanel._enter`), so one that would
      change nothing is refused, and so is a pattern or split alone for pairs with no such demand.
      `calibrate_demand` that would clear the matrix is refused; it and `scale_demand` report `pairs_cleared`.
    - `set_od_demand`: selects the pairs in the grid (`pairs` like "1W->1E", or every pair from
      `origins` / to `destinations`; none named means every pair, as for every demand tool) and switches Hourly/Daily
      (`demand`; left out, the kind shown). It then types the value, picks the
      arrival pattern, and types each vehicle type's share in turn, so the slider moves and the last
      type takes the rest. Hourly demand also takes `peak_hour_factor` (0.25–1, as typed in the PHF box) or
      `quarters` (four 15-minute counts or shares; daily_profile.md), not both; refused for daily demand.
      - `mode_split` is `[{"mode": name, "percent": n}, …]` (a {name: percent} object also works).
      - Names come from `read_od_matrix`'s `vehicle_types`; types left out get 0, and the total
        must be 100. An unknown type is refused, with the real names in the error.
    - `shape_daily_curve`: loads the curve editor with `points` [[hour, level]…] (sorted, scaled to
      0–1, run from 00:00 to 24:00), a `polynomial` [a0…an] of degree 2–6, or a `preset` (weekday,
      flat).
    - `generate_demand`: fills the OD matrix by a gravity model (`network_tools.demand_plan`, handed to the
      Demand Generator as `ODCellPanel.planner`): each origin end's trips (in proportion to its supply; with no
      end flows set anywhere, equally; `total` sets the size) and each destination's (its demand) are shared out by exp(−`distance_decay_per_km` × km) of
      the shortest route along the roads (Dijkstra, one-way roads kept; 0.5 per km by default, or fitted by
      bisection so the mean trip is `mean_trip_km` long, Hyman's calibration; every result gives its
      `mean_trip_km`), then
      balanced to both totals by Furness's iterations (100 at most, to 0.1 %); daily totals are the ends'
      veh/h × 24 unless `total` says. Pairs with no route get none. The numbers are typed into the grid
      through the same controls as `set_od_demand` (pairs with the same number selected together), every
      pair in scope replaced (all pairs unless pairs, origins or destinations narrow it; a pair the model
      gives none is cleared), with `arrival_pattern` and `mode_split` for all. It reports the method, the
      totals asked and made, the busiest pairs, any pair with no route and how many rounded to 0 veh/h. If no
      pair would get any demand (flows only at one end, or a total under 1 a pair) it is refused, saying
      why, and the matrix is left as it was.
      - `degree_of_saturation` (instead of `total`, 0.1–1.2) sizes the demand: the gravity model's
        shares are scaled (bisection, `_fit_saturation`) until the most saturated junction meets it, or the
        longest queue fills `STORAGE_LIMIT` (half) of its link (`Graph.storage_ratio`; short links spill back
        before X reaches the target, and the result's `sized_by` says which bound held),
        by `Graph.saturation` on every pair routed as a Run routes it (`Graph.assign`):
        - signalised: the critical X = Y·C/(C − L) at Webster's cycle (`signal_timing.critical_saturation`)
          with the Run's own lane-based flow ratios (`Graph.arm_ratio`), matching its Webster plan;
        - roundabout: each entry's flow over its capacity measured on the simulated drivers
          (`simulation.ROUNDABOUT_ENTRY_VPH`) against the circulating flow in front of it (the movements
          whose ring arc passes that entry); the ring is one path, so an entry's lanes don't add capacity;
        - priority (uncontrolled): each movement's flow over its measured capacity per lane
          (`simulation.PRIORITY_CAPACITY_VPH`) against the movements it gives way to (the Graph's own
          conflicts), lane by lane as at a signal (`Graph._lane_load`).
        The capacities were measured by holding one approach saturated against priority traffic (sources.md);
        the HCM's formulas don't fit these drivers.
        Daily demand is taken at the network's busiest hour by each pair's curve (`design_hour` in the
        result), as a Run times its signals. Pairs the tool doesn't cover keep their demand and still
        load the junctions. Every result reports the five most saturated junctions with their control.
        A scope whose routes cross no junction where anyone waits is refused.
      - **Checked in a run:** once typed in, the Demand Generator's demand is run headless
        (`MainWindow._check_demand`: 45 simulated minutes at its busiest hour, the Seed box's seed, the
        ticked extensions, the window kept alive and Stop cancelling, as for run_simulation). It holds
        unless, over the last 15 minutes, the vehicles in the network (and those waiting to enter) grow by
        more than 5 % of those setting off, and by 25 or more (`experiments.holds`). If it doesn't, the share of the tool's
        pairs that holds is found by bisection (`experiments.largest_holding`, 4 more runs) and typed in
        instead, in the same undo step; `checked_in_a_run` reports the runs, `total_kept` what stayed. If
        even the smallest share jams, the tool is refused and the matrix put back. Not checked while a run
        is going (it says so).
    - `scale_demand`: scales the demand already entered (the kind shown), keeping its pattern between
      pairs, by `factor` (0.01–100) or to `degree_of_saturation` as above (`network_tools.scale_plan`,
      the Demand Generator's `ODCellPanel.scaler`); `pairs`, `origins` or `destinations` narrow it. It
      reports the factor, totals before and after and the junctions' saturation; a degree of saturation is
      checked in a run as above.
    - `choose_modes`: each pair's split by multinomial logit (`network_tools.mode_plan`, the Demand
      Generator's `ODCellPanel.chooser`): for each of the `alternatives` (vehicle types; road pairs never take the
      metro, which has its own OD matrix: metro.md), U = constant +
      in_vehicle_per_min × in-vehicle minutes + out_of_vehicle_per_min × (out_of_vehicle_min + half its
      headway) + cost × (cost + cost_per_km × route km), `coefficients` by default HCM 2000's (−0.025, −0.050
      a minute, −0.005 a cent). In-vehicle minutes are `from_run`'s mean measured trip time of that pair and
      type where the run has one, else free flow along the shortest route at min(type's speed, limit). The
      logit shares are people; each untimetabled type's vehicles are its people ÷ occupancy (timetabled
      types keep their timetable), typed in as whole percents, pairs with the same split together, one undo
      step; the pairs' veh/h stay. `nests` make it a nested logit (`stats.nested_logit`, scale 0–1). It
      reports the model, value of time (cents an hour), each mode's mean direct elasticities to cost and
      in-vehicle time (β·x·(1 − P)), each pair's logsum, the coefficients and, for up to 12 pairs, the km, where the
      times came from, the people and vehicle shares. Pairs without demand of the kind shown are skipped;
      none left is refused, and so are fewer than 2 alternatives, unknown or repeated types, a positive
      coefficient, all timetabled types and an unknown run.
    - The demand edit tools need Apply like any edit, and each is one undo step. Invalid input (an
      unknown end, shares not adding up to 100, no totals to balance to) comes back as an error the model
      can correct.
    - Applied during a run, a demand change reaches it from its next minute, and the run carries on
      (see main.py, Run); the model is told so.
  - **Checked before asking** (`AIModePanel._check`): network edits are first tried on a copy of the
    network, and demand changes run only their checks. A change that would fail goes straight back
    to the model as an error, so the user is only ever asked to approve changes that can work.
  - **Edit tools** wait for the user. A "Proposed change" card appears with **Apply** / **Skip**,
    and nothing changes until Apply. The tools:
    - `set_junction_control` (slip roads past a new roundabout move out, `fit_slip_roads`), `update_roads` (lanes, or `add_lanes` +1 or -1 each way as people say it, speed, direction, lane changing, type, length; a turn pocket on the approach to `pocket_junction`: `pocket_lanes` 1–2, 0 removes it, `pocket_length_m`, `pocket_side` kerb or offside, a new one 1 kerb lane of 50 m; making a road one-way drops the pocket at the end its traffic now leaves; "pocket" offers the build group).
    - `add_junction`, `add_road`, `add_boundary_end`. These ask for `crossings=roundabout|overpass`
      when the new road would cross others, via the same `build_edge` that Add Edge uses.
    - `move_junctions`, `delete_objects` (institutions too), `build_interchange`, `add_slip_road` (2 roads meeting
      at a junction, `single_lane` or two; `build_slip_road`), `set_end_flows` (one end a call;
      only generate_demand reads end flows: a Run spawns from the OD matrix), `generate_grid` (blocks 20–2,000 m,
      `GRID_SPACING_M`: asked for a 50 x 50 km city of 3 x 3 junctions, models built 12.5 and 50 km blocks).
    - `set_signal_timing`: signalised junctions' method (`SIGNAL_METHODS`), phases, fixed greens, cycle
      limits, target saturation and stop weight, checked by `validate()`; a junction not signalised is refused.
    - `add_bus_stop` and `update_bus_stop` (distance along, direction served, dwell) through `Network.add_bus_stop`
      and `move_bus_stop`; `update_institution` (people, employees, shift, car share, radius; its new links
      returned; a car share replaces a mode split).
    - `place_institution`: one building of a `kind`, at `x`, `y` or the nearest clear ground to a junction or end
      (`near`: rings 10 m apart, 16 directions, diagonals first), with its kind's figures unless given and a
      catchment `radius_m` (default 10 m past its 4th nearest junction or end); returns its door and catchment.
    - `institution_mode_split` (the window, `MainWindow._institution_split`; checked first, its preview the shares):
      each of `institutions` (all by default) split by `network_tools.institution_split`: a multinomial logit
      between Walk and the vehicle types on the trips from each catchment place to the door, and, with metro lines,
      Metro or Walk from each station of its door station's line inside its circle (those stations are places too,
      as the junctions are: metro.md) (route km; vehicle
      types as choose_modes, Walk all out-of-vehicle at `walk_kmh`, default 5), averaged over the catchment;
      `alternatives` default to Walk and every type with stated assumed attributes (`default_alternative`);
      `observed_percent` calibrates the constants (Train 2009 §2.8). Sets `mode_shares` (and `car_share` to Car's)
      as one undo step; returns the shares, the constants, the people and vehicles a day, each origin's km and shares.
    - `place_map`: the map's place, by name (Nominatim, throttled to a request a second and cached, fetched in
      `prepare`) or latitude and longitude, under the network's centre, and its turn.
    - `deploy_institutions`: `count` (1–50) buildings of the `kinds` asked (all by default) at random spots of
      clear ground within the network's extent (`institutions.footprint_problem`, as when placing by hand),
      each with its kind's usual figures and a catchment reaching its 4 nearest junctions or ends (+10 m) unless
      `radius_m`; all or none ("only 3 of 5 found clear ground"). The same `seed` gives the same layout.
      Buildings only: a run carries on.
    - `network_from_background`: replaces the whole network (undoable, like `generate_grid`) with the
      background's roads (background_network.py): over the map, OpenStreetMap's road data for `width_m` ×
      `height_m` (200–4,000 m, 1,200 by default) round the network's centre, `road_classes` main or all; over
      an image, the pixels of `road_colour` (required: the model is told to ask the user) within
      `colour_tolerance`, traced at `resolution_m`. Settings, driving side and background stay; institutions
      still on clear ground stay (those a new road runs through are listed as removed); a plain colour
      background is refused. It reports the source, junctions (signalised, roundabouts), roads, ends and
      the lanes read from an image.
  - **Prepared off the UI thread** (`network_tools.prepare`, called by `AIModePanel._run` on the model's
    thread before a call is checked): a `deploy_institutions` call without a seed gets one, so the check and
    the change approved place the same buildings; `network_from_background` fetches the OSM data or traces
    the image into a cache (`_PREPARED`, 8 entries; a failure kept a minute), so the check and Apply use it
    at once and the window never waits on the network or the tracing.
  - **Safety:**
    - The model reaches the app only through these tools: its replies are data the app acts on, and
      a name that isn't a tool is refused. No tool writes files (`read_source` only reads, and only
      listed project files), so it can't change the app's code. The full list, and which tools
      change things, is pinned by a test (`test_the_assistant_can_do_only_what_its_tools_say`), so
      any new capability is a deliberate change. The wholesale tools build only through the same checked
      paths (the Demand Generator's controls, `institutions.place`, `Network` methods and `validate()`).
    - Edits go through the same model methods as the editor, then `validate()`.
    - `run_tool` rolls back a failed edit completely.
    - Each applied change is one undo step (Ctrl+Z).
    - Tools run on the UI thread; the model's requests run on a background thread and wait for
      them.
  - **Local models without tools:** Ollama reports each model's capabilities (`/api/show`). A
    model that can't use tools (e.g. phi3, orca-mini) chats from the documents only, and the
    chat says so.
  - **Vehicle types** (`network_tools.TYPE_TOOLS`, run by the window, `MainWindow._types_command`):
    `list_vehicle_types` (every type's parameters, the default split, the kinds it can make) and
    `create_vehicle_types` (an edit, Apply first): new types, each made as a SUMO vehicle class
    (`vehicle.VEHICLE_CLASSES`: passenger, taxi, bus, coach, delivery, truck, trailer, motorcycle,
    moped, bicycle, tram, emergency, evehicle, with SUMO's size, speeds, acceleration, braking, gap and
    speed deviation), optionally on a timetable (`headway_min`) and with its own occupancy; through
    `VehicleModes.create`, which checks them all first (new names, a known kind, 8 types at most, a whole
    headway, an occupancy 0–500) and adds none on a refusal. The types appear in Mode Behavior and the
    Demand Generator at once, with 0 % of each pair's trips; all of one call are one undo step.
    `update_vehicle_type` (an edit) changes one type by name through `VehicleModes.update`: new name,
    headway, and size, speeds, acceleration, braking, gap, τ, σ, speed deviation and occupancy within
    ranges (`VehicleModes.FIGURES`), and `filters` (lateral runs), all checked first; one undo step.
  - **Network testing** (`network_tools.RUN_TOOLS`, run by the window, `MainWindow._runs_command`), the
    assistant as the network's planner and tester, not only its signal timer:
    - `run_simulation` (an edit: it writes a workbook, so Apply first; refused when the matrix has no demand of
      the kind shown, rather than reporting a run of zeros): one headless run of the open
      network and demand, `duration_min` 1–240 simulated minutes, any seed (the Seed box by default),
      through `experiments.run_headless` on a worker thread (`MainWindow._in_background`: a local event
      loop keeps the window alive, the status bar says how far, **Stop** ends it). Refused while the
      user's run or another test goes (`_in_background` itself refuses a second, naming the first). Stop while an
      applied edit is still running (an experiment) leaves the change applied and recorded so, and the panel ready:
      `_decide` notices the turn moved on and neither says it's thinking nor answers a newer card. It saves a workbook like a live run's (plus the planning journal) and returns the
      headline figures as `R1`, `R2`…
    - `run_experiment` (an edit): `scenarios` (1–6; the first is the baseline, normally the network as
      built), each with `changes` (edit tools from `network_tools.SCENARIO_TOOLS` applied through
      `run_tool` to a copy: `experiments.scenario_network`), `demand_scale` (0.05–10, to stress it) and
      `extensions` (their signal overrides, `ExtensionsPanel.overrides_for`); `seeds` ("1-5", a list; at
      most 20) and 60 runs in all. Every scenario is checked before anything runs; the open network is never
      changed (a finding is applied afterwards by the ordinary edit tools, with the user's approval). Each
      run has its workbook in "ACMS experiment … runs"; the experiment's workbook
      (`experiments.experiment_workbook`) has a Read me, the Comparison (`experiments.compare`: each figure
      per scenario over its seeds, mean, SD, 95 % interval, the difference from the baseline with Welch's
      interval, clear or not, better or not), the Scenarios, every Run, the Planning journal and the
      baseline's inputs. Fewer than 3 seeds comes with a caution. It returns as `E1`, `E2`…
      The difference is a paired t-test over the shared seeds (common random numbers) with its `p`, at 95 %
      widened by Bonferroni for several scenarios (`confidence`); each row has `replications_needed` for
      ±`precision_percent` (5 by default), and any over 20 come back under `unstable`. The figures include
      the travel time, planning time and buffer indexes and the unfinished share of trips. run_simulation and
      run_experiment take `measure_from_min`, a warm-up left out of the figures (results.md).
    - With an extension that may ask an AI agent ticked (TSP, DEVL), these headless tests pause for each answer and
      apply it after the model's real latency, as at 1× (experiments.md, Simulated time and AI agents); their results
      carry `agent_timing` saying so, and point to a paced Batch Run for other paces.
    - `read_results` (the tests so far, or one by id with its busiest roads and longest queues) and
      `compare_runs` (two or more single runs, each a sample of one: it says so).
    - `analyse_run` (a read; the latest run unless `id`): `experiments.analyse_run`: trip-time reliability,
      warm-up and steady state (MSER-5, batch means), the Gini of road flows, arrivals per origin and minute
      against Poisson, 95th-percentile queues against each road's length, bus headway adherence per line and
      stop. `check_calibration` (a read): the run's measured road flows against `counts` (GEH, `stats.fit`)
      and/or `profiles` (FHWA 2019 variation envelopes and Criteria I–IV over 3 or more observed days).
    - More reads on a run (the latest unless `id`; refused with none): `read_detectors` (detector statistics
      per link and stop-line saturation flow, `experiments.detectors`), `analyse_safety` (conflicts, the safety
      performance function and Empirical Bayes ranking, `ttc_s` 0.5–3), `forecast` (Holt's smoothing of a
      series, `what` and `road`, with hold-out accuracy against the naive forecast). And on the network or
      data: `critical_links` (each used road closed in turn, the demand shown re-routed at free flow,
      `network_tools.critical_links_plan`), `regress` (OLS of `y` on `x` over `data` or an experiment's runs,
      `log` for natural logs), `network_indices` and `cluster_conditions` (network tools: graph indices; days
      into weighted travel conditions by k-means).
    - Edits that run tests: `assign_routes` (route choice by path-size logit on the network, its link and move
      times (`turn_times_s`, junction delay counted once: simulation.md) by successive averages over `iterations` headless runs, or `off`; one undo step) and `optimise_signals` (a
      genetic algorithm over fixed-time settings for `junctions`, up to 120 runs without workbooks; returns
      the `set_signal_timing` to apply and changes nothing). Both pass the signal agent's waiting form, as
      the other headless runs do. `assign_routes` takes `until_gap` (stop once the relative gap, the share of flow off its logit share, is under it;
      `MainWindow._equilibrium`, shared with run_experiment's scenarios).
    - `run_simulation` and run_experiment's scenarios also take `incidents`, `weather`, `random_incidents`,
      `meters`, `assign_routes` and `lateral` (lateral position and filtering, said on the workbook's Run sheet;
      experiments.md, Conditions); run_experiment also `conditions` (every design
      under each, with regret and the robust choice), `factors` (a factorial design; two factors get a two-way
      ANOVA) and `objective` (the figure they're judged on, delay by default). A run's answer carries `health`
      ("fine" or its problems, analysis.run_health).
    - Reads on runs and experiments (analysis.md): `junction_los` (control delay and LOS per approach and
      junction, by 15-minute period), `metro_report` (metro.md: each line's riders, busiest segment against HCM
      2000 Eq 27-28's capacity, stop boardings, at the design hour, from the metro OD matrix and the buildings' metro
      riders; words like metro, rail, station, transit offer it), `weaving_los` (weaving.md: each weaving segment's HCM 2000 Ch 24 speeds,
      density, LOS and capacity from the demand, a run's own speed and density beside them; words like weave,
      ramp, freeway, merge offer it), `diagnose_signal` (one signal's flow ratios, v/c, arrival type, HCM delay
      against simulated, causes worst first with the tool to fix each), `check_run_health`, `emissions_report`,
      `resilience` (a disrupted run against the same seed's undisrupted one), `power_analysis` (`_power_analysis`:
      seeds for 80 and 90 % power, the smallest detectable difference, a power curve, from an experiment or sd and
      difference), `rank_scenarios` (all pairs: ANOVA, Friedman, paired differences with Bonferroni and Wilcoxon,
      compact letters), `read_decisions` (an agent's decisions in a run with their outcome, telemetry and
      person-delay estimate), `benefit_cost` (`_benefit_cost`: a scenario against the baseline in money, NPV and
      BCR at 3.5, 4 and 7 %, the BCR's range over the seeds; everyone's delay, on board finished or not
      (`person_delay_on_board_h`) and waiting to enter (`entry_wait_veh_h` × the run's occupancy), over the hours
      measured after the warm-up; a `caution` when the scenario completes over 5 % more or fewer trips); `analyse_safety` takes `speed_change_kmh` (the power
      model's change in crashes).
    - Checks on the network (network_checks.md): `design_audit` (Green Book sight, turning paths, skew, lane
      balance; Safer Roads junction spacing; HCM weaving segments), `accessibility_report` (what residents reach, a road's closure's
      loss), and `critical_links`'s single points of failure and `area` closure.
    - Edits that run tests: `preflight_check` (`_preflight`: a run at a tenth of the demand for slow links and
      one at half for gridlock), `test_agent_compliance` (`_compliance`: an agent's extension on a fixed battery,
      as is, 30 % more demand, heavy rain with a lane closed, each run's invariants), `decision_sensitivity`
      (TSP's decisions re-asked with one fact changed, and unchanged for consistency), `parameter_sensitivity`
      (`_parameter_sensitivity`: each calibration parameter ±10 % on common seeds, experiments.md), and `equilibrate_demand`
      (an OD tool: elastic demand, od_matrix.md).
    - `set_extension`: tick or untick an extension by name (or its short name, "TSP") and fill its
      panel's fields by their labels (`ExtensionsPanel.find`, `set_settings`; a missing field or choice refused).
    - The extensions' own actions (`MainWindow._runs_command`; each checked before its approval card, a refusal
      changing nothing): `read_extensions` (`ExtensionsPanel.describe`: each extension ticked or not, what it does,
      each setting by label with its value and what it can be, a dropdown that lists as it opens listed now; and the
      emergency schedules); `schedule_emergency` (`_schedule_emergency`: the Dispatch Emergency Vehicle tab's rows
      replaced by `set_fleet`, each an end or junction with a route between them (`_route_check`), every_min or
      after_min 0-120, not both; dispatch_now also sends them into the live run); `start_batch` (Batch Run's
      `start`: its seeds, duration and Test TSP/DEVL set in the panel, then Start Batch as a click would, the TSP
      and DEVL tabs giving the models; checked first: the seeds, duration, demand, no run going, the tabs' models,
      with Test DEVL the schedules); `replay_run` (a workbook's path, `MainWindow.replay_run`). The Research Log has
      none: grading the agent is the user's.
  - The assistant's note (`assistant.TOOLS_NOTE`) sets the workflow: `note_plan`, an experiment with the
    network as built first and one change a scenario, 3 seeds or more, `read_results`, `note_plan` with the
    conclusion, a difference claimed only where the comparison says clear, and the open network changed
    only when the user agrees.
- **The Scenario Builder** (`scenario.py`, `build_scenario`, `MainWindow._build_scenario`): "tell it and it
  becomes", after the papers in `references/transportation_engineering_papers/` (ChatSUMO, ChatSUMO-Agent, AgentSUMO),
  which keep the model away from planning.
  - The form (`scenario.SCHEMA`) is flat, with choices wherever possible: intent (build, test, question),
    place and area_km or grid (with block_m, lanes, speed_kmh, driving_side), junctions, signal_timing,
    traffic (light 0.4, medium 0.65, heavy 0.85, near capacity 0.95, over capacity 1.1 as the busiest
    junction's degree of saturation; `LEVELS`) or vehicles_per_hour, whole_day, trip_km, bus_every_min,
    mode_choice with car_cost_per_km and bus_fare, route_choice, what_if (change: add lane, remove lane,
    speed limit, roundabout, signals, signal timing, close road, more traffic, less traffic, bus priority,
    optimise signals; where, value, method), report (reliability, safety, detectors, vulnerability, forecast,
    network), minutes, seeds.
  - `prepare(spec, state)` fills what was left out with stated defaults (a 3 × 3 grid if there's no network,
    medium traffic, 30 min, 3 seeds, the busiest road or every junction as a what-if's target) and lists
    problems that stop the build (a place and a grid, a grid over 20, a speed limit with no value, a signal
    timing with no method, a timetable with no bus type…) before anything runs.
  - `plan(spec)` orders the steps as a study takes them: place_map and network_from_background, or
    generate_grid; set_junction_control; set_signal_timing; update_vehicle_type (the bus's headway);
    generate_demand (by degree of saturation, checked in a run, or a total; mean_trip_km) and
    shape_daily_curve; choose_modes; assign_routes; run_simulation and analyse_run; the reports
    (analyse_safety, read_detectors, critical_links, forecast, network_indices); optimise_signals for its
    what-if; and run_experiment with every what-if against "As built" over the seeds (paired). Targets written
    `@…` (every junction, the signalised ones, the busiest road or junction by delay in the baseline run, a
    road's lanes, the optimised plan) are resolved as each step runs (`MainWindow._resolve`).
  - Checked first: the approval card shows the plan step by step with its assumptions (`describe`, the
    check's `preview`); one Apply runs it all. Each step goes through its tool's own path (the Demand
    Generator, Mode Behavior, the run tools, a checked network edit), and the whole build commits once
    (`MainWindow._building`): one undo step. A failed step is recovered where a known fix exists (demand that
    can't be sized by saturation: a flow per boundary end by the level; a what-if that can't be built: left
    out, the rest tested; a step that would change nothing, such as the grid already on screen, counted done),
    else reported; nothing runs after a failed network, demand or baseline step.
  - `report(spec, done)` says in plain words what was built, the demand (busiest junction's saturation,
    whether it held), the baseline run's figures, spillback, bus headway adherence, safety, critical roads,
    and each what-if's clear differences in delay, trip time, throughput, travel time index and conflicts
    (paired, with a caution where those figures need over 20 runs), with what couldn't be done and the
    workbooks.
- **How a request becomes tool calls** (no form mode: every model chooses and chains the tools itself, so it can
  follow a conversation; guided mode, which had local models fill in the Scenario Builder's form, was removed on
  2026-10-05 after the Research Log showed it missing the point of every build request it got: institutions, a
  2000 x 1500 m size, "busy city" read as a place, none of which the form could hold).
  - **Tool selection** (`network_tools.tools_for`, from `AIModePanel._ask`): each request is offered `CORE_TOOLS`
    (network_summary, describe, list_roads, list_junctions, show_on_map, get_selection, note_plan, find_tools),
    the tools of each `TOOL_GROUPS` group its words point to (build, place, demand, institutions, run, signals,
    experiment, analysis, transit, extensions, docs: regex stems; "place" also when a capitalised word that isn't
    the app's own names somewhere, `_places_named`), any tool it names, and those called in the last two exchanges (the whole conversation's made the offer grow
    without end: 26 to 63 tools over a 190-prompt campaign).
    Typically 13 to 40 of the 78 (a greeting gets 8). The long specs are offered only when their own words are
    used: `build_scenario` (3.8k characters) when a request touches three or more of `STUDY_GROUPS` (build, place,
    demand, run, signals, experiment), both builds and tests ("build a 3x2 grid, run it"), or says "study"; the demand models (`demand_models`: shape_daily_curve,
    calibrate_demand, choose_modes, equilibrate_demand) for words like mode, split, fare, counts, daily; and "junction"
    alone no longer offers the signal tools. "populat" offers the institutions' and demand tools; how-to words ("how do
    I", "key", "button", "setup") offer `read_source` for README. On 38 earlier stress prompts this cut the tool specs
    sent from 19.4k to 16.3k characters a request (15 adversarial ones: 22.5k to 17.6k), with every prompt still offered
    the tools it needed (38/38, from 37: "demnd" and "saturtion" are matched now). `find_tools` ({"need"}, `expand_tools`) adds the tools
    matching what the model says it needs to the list it is sent, mid-answer; a tool it calls that it wasn't
    offered runs and is added too. Small models choose better among fewer tools and read fewer tokens
    (Paramanayakam et al. 2025; sources.md).
  - **The prompt** (`assistant.TOOLS_NOTE`): before its first call the model writes one `Plan:` line (what the user
    wants in its words and the tools in order: Plan-and-Solve prompting; `assistant.plan_line` reads it for the
    Research Log), then calls them in the same reply; recipes for what users ask most (a city of W x H m, demand
    from buildings, a traffic level word, a real place, simulate, what-if on a copy, a slow junction, a question
    about an idea, "more people" (there is no population figure: update_institution or scale_demand, saying which),
    the metro as the user's own network (they draw lines and fill its OD matrix; the assistant reads metro_report
    and buildings' metro shares), things the app doesn't have (a stadium, a harbour: say so, put nothing else in their
    place, call no invented tool;
    a number past a tool's limit: say the limit first)); the honesty rules (an error means nothing happened; quote the results' own numbers).
  - **The loop** (`assistant.converse`): a call that failed is not run again unchanged (the model is told so,
    `error_type` "repeated"); a local model that sends nothing (all of it in its thinking: `NoAnswer`) or a tool
    call Ollama can't parse (`MALFORMED`: qwen3.5 4B's "XML syntax error") is asked again with thinking off
    (`stream_chat(think=False)`), then once more with a one-line `NUDGE` (`_next_reply`); if it still can't, or
    after `MAX_TOOL_ROUNDS` (12) rounds without an answer, the app ends the answer with a note of why and the tools
    that ran (`_ended`), not an error, so the step keeps its record. A reply sent as a call to an invented
    "answer" tool (`ANSWER_TOOLS`) is the reply; a tool call written as text is made, and so is one shaped as a call
    (a name and a parameters object) naming a tool that doesn't exist, so it is refused instead of being shown as the
    answer (llama3.2 3B wrote `{"name": "teleport_vehicles", …}` as its reply). An invented tool's refusal
    (`normalise`) gives the tools named alike only as options, and says that if none does it the app can't, so the
    model tells the user (`close_all_roads` is spelt like `list_roads` but isn't it). When every call of an answer was
    refused, the app ends the reply with `REFUSED_NOTE` ("every tool call in this answer was refused, so nothing was
    changed"), on screen too (llama3.2 3B claimed "the city was created" after each call was refused). A reply whose
    figures (at least 3, and half of them; or one of 1,000 or more, not a year: qwen3.5 said 1,269 veh/h could enter
    where its tool said 3,119 was kept) come from no tool result of the answer nor the request ends with
    `UNGROUNDED_NOTE` (`ungrounded`; llama3.2 made up a refused run's results). A tool refused and never run in an
    answer gets one reminder (`RETRY`, quoting the refusal) to call it again corrected or say why not. A tool refused
    `MAX_REFUSALS` (3) times in an answer isn't run again in it (`error_type` "gave_up": tell the user what couldn't be
    done, or ask; no RETRY for it): llama3.2 3B called set_end_flows 7 times with new wrong arguments, to the round
    limit at 110k input tokens. A reply that claims a change (`CLAIM`: "I've set…", "has been added", "Done") when no
    tool ran in the answer gets one reminder (`CLAIMED`: call the tool, or say plainly it wasn't done), then, still
    claiming, `NOTHING_RAN_NOTE`. The app's ending note (`_ended`) lists what ran in this answer only. Usage records
    carry the asking step's tag (`USAGE_TAG`, a thread-local), so a step counts its own requests. An Ollama model's
    own prompt count, when above the estimate, sizes its next num_ctx (`_CHARS_PER_TOKEN_SEEN`). A request that
    ends with no answer is recorded too (`no_answer`; qwen3.5 thought for 40 s, then said nothing, uncounted before).
    End names
    copied from an example that are neither the network's nor in the request (`repair_ends`) are dropped, every end
    used and said so in `read_as` ("None" there is no end). Another tool's argument is dropped and said so in `read_as` (`drop_strays`: llama3.2
    sent set_od_demand's peak_hour_factor to generate_demand on every try); `normalise` called directly refuses it,
    naming the tool it belongs to. A mean_trip_km of 0 is no length (the default decay);
    an empty mode_split is none, and fractions adding up to 1 are percent (`ODMatrixPanel._shares_from`); generate_demand
    with nothing to size it by names degree_of_saturation first.
    A traffic level put in a choice of another kind (`LEVEL_WORDS`: demand "peak") is read as the degree_of_saturation
    it means (`_level_read`: busy/peak 0.85, medium 0.65, light 0.4) when the tool takes one and none in range was
    given (llama3.2 sent 0 after the refusal), else the refusal says which to give; a total, or with no level word a
    degree_of_saturation, of 0 is llama3.2's filler, so none (`_ZERO_IS_NONE` in `normalise`: width_m, height_m,
    resolution_m, total, mean_trip_km, degree_of_saturation, when optional);
    place_map's note says network_from_background takes no arguments. the app's own
    ending note (`_ended`) is now shown as well, not only kept in the record. **Plan follow-through**
    (`unfinished`, `planned_tools`): the Plan: line ends "-> tool, tool" (TOOLS_NOTE's example); when the model
    gives its final answer while tools its own plan names (of those it was offered) were never called, the app
    reminds it once (`FOLLOW_THROUGH`: call it, or say in one line why it isn't needed), so the second part of a
    two-part request isn't dropped. Once an edit (`converse(edits=…)`: `EDIT_TOOLS`) has run, tools a later Plan: line
    adds don't count, the first line's still do (llama3.2 3B built the network, re-planned with update_roads and
    add_boundary_end, and reminded, called them with made-up arguments). When the answer has done nothing yet (a `Plan:` line and the turn ended: Gemini
    3.5 Flash Lite did so in 12 of 15 prompts, and sent the same line again when only reminded), the reminder's request
    requires a call to the planned tools (`stream_chat(require=…)`: Gemini's function calling mode ANY with
    `allowedFunctionNames`, xAI's `tool_choice`; Ollama has no such option and is only reminded, up to 3 times).
    Lines of only names and arrows count as plan (`_only_a_plan`: llama3.2 3B's second plan line). A signal method the request doesn't name while it names another
    (`network_tools.contradicts`, from `AIModePanel._ask`'s `call_tool`: qwen3.5 4B, asked for SCOOT, thought
    SCOOT and sent scats) isn't run: the model is told what the user asked for (`error_type`
    "not_what_was_asked"). So is `generate_grid` for a request naming a real place and no grid (`_places_named`; llama3.2 3B, asked for
    Dhanmondi's roads, placed the map then built a 5 x 6 grid): it is told to use network_from_background. A road named from its other end ("2,2-2,3" for 2,3-2,2) is that road (`run_tool`
    runs again with the road's own id and says so in `read_as`; `explain_error` suggests it first), and ids sent
    as objects are refused saying how to call (`_ids`). Ids are read however a small model lists them: "2,2; 3,3",
    Python's "['3,3']" (`_repaired_json` falls back to `ast.literal_eval`, JSON's kinds only), and "all" where a
    tool takes ids (`ALL_IDS`: update_roads every road, set_junction_control every junction, set_signal_timing
    every signalised one; never delete or move). An id that exists nowhere is a KeyError in every edit tool
    (`_junction` and the rest), so the other-end retry and `did_you_mean` work for all; a boundary end given as a
    junction, or a junction as an end, says which it is. An edit that would change nothing comes back as
    `already_so` (unchanged, "tell the user so"), not a refusal (qwen3.5, asked to retime a Webster signal with
    Webster, read the refusal as failing). Tool accuracy, 2026-10-13 (`tools/eval_commands.py`, 22 everyday
    tasks, `reports/tool_accuracy_2026-10-13/`), also gave: set_junction_control's before → after (`changed`);
    set_signal_timing naming every unsignalised junction at once, phases 2 or 4, one green a phase, greens alone
    meaning fixed and refused with another method; update_roads speed 5-130 and pocket settings left with no
    pocket junction ignored; update_institution's car_share a percent (0.4 refused as 40 %'s fraction) and its
    before → after; generate_grid's result in the description's columns x rows, and width_m/height_m for a size in
    metres (horizontal 1500 refused as a count, not metres); y grows south in move_junctions, add_junction and
    add_boundary_end; list_junctions and list_roads with a filter saying how many in all; run_simulation's
    incident fields given at its top level read as one incident (`fold_incident`); set_od_demand's value per
    pair and the matrix total; read_od_matrix's totals; read_traffic refusing unknown roads, with the matching
    vehicles' mean speed and stops; the run headline's completion rate, unfinished percent and warm-up;
    daily demand tested at its busiest hour (`demand_simulated`), not from midnight; an experiment's answer
    leading with its objective and four lead figures with units (`LEAD_FIGURES`; the rest counted in `more`),
    `interval_level` for the per-row confidence and "not told apart" for a difference that isn't clear;
    analysis tools refusing "the latest" when the latest test is an experiment; scenarios under conditions
    compared with their own condition's baseline (`_baseline_of`); start_batch seeds a list. A second round:
    degree_of_saturation given as a level word ("busy") read as its figure, and a level word said again beside a
    degree given left out (`_level_read`); update_roads given a road's two junctions ["2,2", "3,2"] reads that road
    (`read_as`); "list_junctions" or "list_roads" in place of ids means all; delete_objects of the two junctions of
    a road the request names (and not the junctions themselves) refused by `contradicts` (llama3.2 deleted
    junctions 4,4 and 4,3, and every road at them, for "delete road 4,4-4,3"); the analysis tools offered for a
    request about counts, validation or observed data (`TOOL_GROUPS`).
    - Measured on 8 two-part requests (qwen3.5 4B, 2026-10-05, each second part changing something, checked by
      the arguments that ran): with the follow-through 6/8, without it 6/8 but "generate busy traffic and run it"
      planned and never done (with it, reminded once, both done); with the road-id and method checks 8/8 (the
      method check caught scats for SCOOT, and qwen resent scoot); then 7/8, "add a lane" sent as the count the
      road already had, so update_roads took add_lanes, after which that prompt and two others passed 3/3.
      qwen3.5 4B still varies on "switch every signal to SCOOT" (once it reached for optimise_signals instead). A misspelt choice with one
    close match ("scot") is that choice (`_coerced`). A list
    or object sent as JSON text (small models do, for run_experiment's scenarios) is read by `normalise`
    (`_repaired_json`: trailing commas dropped, brackets left open closed); text that still isn't JSON is refused
    saying so, with the parser's message and position, not a vague "give 1 to 6 scenarios".
  - **Tokens and time** (`assistant.context_size`): Ollama's `num_ctx` is the next of 16k, 32k, 64k... above the
    conversation, not its exact size, because a request with another num_ctx reloads the model and drops its
    cache of the prompt: with a steady size each round of a conversation reuses the system prompt and documents
    already read (before: about 12k input tokens read again every round). The size is estimated at 4 characters a
    token (measured 4.6 on qwen3.5 4B with the tools: 14,017 tokens for about 65k characters); at 3, a usual first
    request asked for 32k, which a 12B model's cache can't hold on an 8 GB GPU (gemma4 12B timed out on 8 of 15
    prompts). With README out of the prompt, 39 of 45 stress prompts' first requests now fit 16k.
  - **Context budget** (`assistant.fit`, in `stream_chat` before every request): a model past its window fails
    quietly (Ollama cuts the reply off or drops the start of the prompt; 82 % of a 190-prompt campaign's replies
    were cut), so each request is trimmed to its window less `REPLY_TOKENS` (4096), counted at `CHARS_PER_TOKEN`
    3 (JSON and code run near 3) with the tools' schemas and tool calls included. Always, budget or not, tool
    results more than one exchange back are a stub (`STUB`; the previous exchange's stay whole for a follow-up):
    a local model's time grows with what it reads, and the same stub every time keeps Ollama's cached prefix.
    Past the budget: the previous exchange's results become a stub too, then the oldest exchanges go whole (each tool call keeps its result; the system prompt says how
    many went), then the current exchange's longest results are cut (`CUT` says so). The system prompt and the
    latest message always stay; the saved conversation is untouched. The window: `LOCAL_CONTEXT_TOKENS` (the
    `ACMS_NUM_CTX` environment variable, default 32768; 16384 for a 12B model on an 8 GB GPU), else
    `API_CONTEXT_TOKENS` (131072); `num_ctx` never asks past it. Ollama's `done_reason` and `num_ctx` go in each
    usage record, and a reply stopped by a full window ends saying so. ARCHITECTURE.md, sent with every request,
    is kept to an index (about 10.6k characters; the system prompt about 16.6k). It is the same every request, so
    Ollama reads it once and reuses it: trimming it saves window, not time, and it is not varied per request.
  - **Compact results** (`assistant.compact`): what the model reads of a tool result is JSON without spaces, a
    list of three or more records as `{"columns", "rows"}` (each key once); the result kept by the app and the
    Research Log is unchanged. list_roads on an 8 x 8 grid: 24.2k → 3.3k characters (with `LIST_LIMIT` 60 rows).
  - **Measured** (2026-10-05, qwen3.5 4B on an RTX 3070, the stress test: 30 prompts written by another model,
    the real window offscreen with the Research Log ticked, each from a fresh 4 x 4 grid; Documents\ACMS
    results\AI stress test). Before the steady context size and the later fixes, then after, on the 15 prompts
    both ran: model time 468 s → 283 s, 9.4 s → 5.2 s a request, output tokens 18.6k → 14.1k; passes 11 → 11
    of 15 (the after run's two crashes, an empty reply and Ollama's "XML syntax error", were then fixed by the
    retry ladder). Over all 30 after: 21 passed; the rest were 5-minute timeouts of heavy experiments the
    model set up correctly, a reply sent as an "answer" tool, a two-part request half done, and two 12-round
    loops (each since handled). Ollama's prompt_eval_count reports the whole prompt even when it is cached, so
    the saving shows in time, not tokens.
  - **Measured, adversarial** (2026-10-05: 15 prompts asking for what the tools can't do, e.g. invented tools, ids
    that don't exist, numbers past the limits, a metro, "increase the population", a past the model never had; the
    real window offscreen, Research Log ticked, each from a fresh 4 x 4 grid, every card applied, 300 s each; method
    and files in Documents\ACMS results\AI stress test\adversarial-2026-10-05). Before the changes above, then
    after, one run each: qwen3.5 4B 1,811 s → 492 s, timeouts 2 → 1; llama3.2 3B tool calls written as its answer
    3 → 0, and 9 answers whose every call was refused now say so (it had claimed "the city was created");
    gemma4 12B timeouts 10 → 6 (its cache now mostly fits 16k); Gemini 3.5 Flash Lite plan-only replies 13 → 0
    (14 of 15 did what they planned or said why not); Grok 4.6 1,239 s → 362 s, timeouts 1 → 0. Still seen: a
    small model's arithmetic on what it built, and run_experiment set-ups that outlast 300 s.
- **Steps, for the Research Log** (`AIModePanel._step`, `_record_call`, `_end_step`): each answer is a step from
  the question (or watch-mode look) to its end: the prompt, the editor update, the provider and model, the tools
  it was offered (`tools_offered`) and its Plan: line (`plan`), every tool call as it is decided (its arguments, the approval card's plan, the
  user's decision and when, its result, compute seconds), the model's messages and reply, any error, and its
  requests' tokens and seconds. `attach` takes `state()` (the window's state before the step, only while a logging
  extension is ticked) and `step(event)` (MainWindow._research_step: the changes and the map, then the extensions'
  `log`; extensions.md).
- **Working alongside the user:**
  - **Editor updates.** Every message starts with an `[Editor update]` block the app writes.
    It carries the user's own changes since the assistant's last reply, from
    `network_tools.describe_changes`, which compares the network then and now (bus stops added,
    moved or re-pointed, and deleted, included). It also carries what's selected on the map.
  - The transcript notes what was shared. The assistant's own edits are not reported back to it.
  - **Conversation as state.**
    - The conversation belongs to the project, not to one model. Switching models keeps it:
      the new model warms up on the documents plus the conversation.
    - The conversation is saved in the project file as plain text (`assistant.portable`: tool
      calls become a note, stale tool results are dropped) and restored on load.
    - **Clear Conversation…** forgets it after asking. Keys are never saved.
  - **Controls:** one row of icon buttons across the page under the model pickers: Start/Restart
    (primary; its glyph and name swap via `set_glyph`), Available Tools, Rescan PC, Export History… and
    Clear Conversation… (danger). Beside the question box, Ask (a send
    icon, primary) and Stop share one place, one at a time (`_stopping`); the box is as tall as they are. Asking about the selection or the design goes through the model's own
    `describe`, `get_selection` and `analyse_network` tools; models without tools get the editor update.
  - The assistant answers only when asked: it never looks at the user's edits by itself (its watch mode was
    removed on 2026-10-06). The menu bar's Watch my edits is the window's activity log (clock.md), not the
    assistant's; the user's edits reach the model in the next message's editor update.
- **Status bar indicator:** always in view on the right of the status bar, whatever the tab.
  - It shows the connected model's short name (`model_label`: "llama3.1:8b" → "Llama") and full ID.
  - It shows what the model is doing: selected (press Start), warming up, ready, answering, waiting for your approval, or couldn't start.
  - The dot's colour shows the state. Clicking it opens System 2.
  - It is hidden while Chat with is Off. It is driven by `AIModePanel.indicator` →
    `MainWindow._show_ai_status`.
- **Main window hooks:** `AIModePanel.attach(network, selection, show, edited)`, wired in `main.py`:
  - `_selection_info` describes the map selection.
  - `_show_on_map` points an object out.
  - `_ai_edited` redraws and commits the undo step. A new grid refreshes the Create tab and fits the view.
- **System 1** (`assistant.py`, no Qt): fast typed answers, no text generated.
  - **One API, two back ends.** `assistant.system_one(provider, state, questions, key)` posts
    `{"model": "jev-latest", "state", "questions"}` to `/v1/systemone`: Jev at TypeSafe (`JEV_URL`, Bearer
    `JEV_API_KEY`) or Laya on this PC (`laya_url`, `LAYA_HOST`, default `127.0.0.1:8080`). Questions are
    typed: `noul` (P(yes)), `choice` (`criteria` {label: description}, up to 16 on Laya) and `score`
    (`criteria` a list of levels). Each request goes in `USAGE`, so View Summary → AI Usage counts it.
  - **Laya** is a ModernBERT-large decision model (`laya_english_f16.gguf`, 846 MB) run by ggmlc's
    `laya.exe serve`; it isn't a chat model, so Ollama can't load it. Picking Laya under **Use**
    (`AIModePanel._choose_system_1`) calls `assistant.start_laya` off the UI thread:
    - a Laya already answering `/health` on `LAYA_HOST` is used as it is;
    - otherwise `laya.exe serve --models-dir <its folder> --port <LAYA_HOST's>` starts with no window,
      Ollama's CUDA 12 runtime (`lib/ollama/cuda_v12`, or `CUDA_PATH\bin`) put first on its PATH, since
      its GPU build links to `cudart64_12.dll` and `cublas64_12.dll`, and its output in `acms-laya.log`
      in the temp folder;
    - it listens on every interface, so it gets a fresh random key (`LAYA_API_KEY`) that only this app
      sends;
    - the page then says "Laya is ready on cuda:0 (english)", or why it couldn't start (a missing CUDA
      runtime, or the last words it printed);
    - picking anything else, or closing the app (`atexit`), ends the one the app started.
  - **What asks it:** the signal agent (`MainWindow._ask_agent`, simulation.md), for Transit Signal
    Priority when its **Decided by** is System 1 (extensions.md: the junction observed as structured data,
    one noul), about 50 ms warm on an RTX 3070 against seconds for a chat model; System 2 when it says
    System 2 (the started model, prompted zero-shot as the engineer on duty, answering by the boolean tool
    `decide_tsp(activate, reason)`; both read the junction's JSON telemetry). Plain text from another
    extension goes to System 1 when one is chosen (`system_one_for_agents`, safe from the agent's thread) as
    the state of one noul (`assistant.system_one_true`, `YES_NO`), else System 2. In prose Laya got 5 of 8
    traffic questions right (distances and seconds are its weak spot); a structured state with a specific
    question did far better, which is how TSP asks it.
  - **For batch runs** (the Batch Run extension): `agent_options()` lists who can decide TSP as the last scan
    found them: the two baselines (Never preempt, then Always preempt: no system, a `rule` False or True), System 1's (Jev, Laya) and System 2's (every Ollama model; Grok and Gemini
    whenever their keys are on this PC, each with the model last started in System 2, else the provider's usual
    one, `assistant.PREFERRED`), each with its system, provider, model and a name for the run's file; `key_for(provider)` gives
    its key and `start_laya()` starts Laya off the UI thread. `assistant.last_usage()` is a thread's last
    request's tokens, for each System 2 answer's record (`_record` keeps the thread's own record, never one read
    back from `USAGE`, which a batch's runs append to side by side).
  - View Summary's AI lines show System 1's choice and state.

## Local campaign harness

See [prompt_campaign.md](prompt_campaign.md) for the optional same-process Qt runner of numbered DOCX prompts. The user-authorized campaign applies validated cards automatically for both prompt groups.

Apply callback exceptions now return an explicit tool error and settle the pending request, rather than leaving the model blocked after Qt reports a slot traceback. The error warns of possible partial changes; no automatic rollback or success is claimed.
