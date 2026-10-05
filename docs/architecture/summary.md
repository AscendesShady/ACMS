# summary.py and mode_behavior.py: View Summary and Mode Behavior tabs

- `SummaryPanel` has four tabs, each one Markdown page (`_Page`, a `QTextBrowser`). The open page
  rebuilds when shown, after each undoable change, and when AI Mode's status changes.
  `MainWindow._summary_inputs` gathers what they report on.
- **Network** (`network_markdown`):
  - A bold headline of counts.
  - Layout: grid, junctions by control, roads (one-way, curved, overpass), length and lane-km, speed
    limits, interchanges, extent.
  - Boundary ends: supply, demand, balance.
  - **Design check** (`analyse_network`): each object it names is a `select:` link. Clicking it
    selects the object on the map (`MainWindow._show_on_map`).
- **Demand** (`demand_markdown`):
  - Hourly and daily totals.
  - A 24-hour bar chart of all daily demand (`hourly_chart`, drawn to an image), with the busiest
    and quietest hours.
  - Trip-weighted mode splits.
  - **OD matrix vs boundary ends:** each end's hourly OD row and column totals against the supply
    and demand on its arrows; mismatches say **Check**.
  - The busiest pairs and the arrival patterns.
- **Results** (`results_markdown`): the run going on (rebuilt every 2 s while the page is open) or the last
  one (`MainWindow._results`): the headline (simulated time, throughput, people an hour), traffic (spawned,
  trips, on the road, throughput, people carried, mean speed, distance, time on the road, delay and delay
  per km, mean trip and its delay), queues (now, most at once, the longest), the longest queues and busiest
  roads (`select:` links to the map), by vehicle type, the assumptions (occupancy, sampling), and the
  workbook's path. Labelled lines and lists, not tables: the sidebar is narrow.
- **AI Usage** (`ai_markdown`):
  - System 2's status.
  - This session's requests from `assistant.USAGE`: count, tokens in (cached) and out as the
    providers report them, time to the first words and to the whole reply, and the latest requests.
  - The models Ollama holds in memory (`assistant.ollama_loaded`, i.e. `/api/ps`), with memory,
    VRAM share and context length.
  - Cloud models have no memory to show; no cost estimates are made.
- The mode splits on the Demand page name the current vehicle types.
