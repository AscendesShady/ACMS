# design_checks.py and accessibility.py: the network as drawn

Both are core modules (no Qt). They read the network (and, for accessibility, optionally a run's measured link
times); the assistant reaches them through `design_audit` and `accessibility_report` (`MainWindow._runs_command`).
Vulnerability helpers live in network_tools.py: `cut_elements` and `critical_links_plan`'s area closure.

## design_checks.py (tool `design_audit`)

Each finding names the object, what the rule asks and what is there; every rule is cited.

- **Sight on curves** (Green Book 2018 §3.2.2, §3.3.12): for each curved road, its tightest radius R, stopping sight
  distance SSD = 0.278·V·t + 0.039·V²/a (Eq 3-2, metric, t = 2.5 s, a = 3.4 m/s²; 50 km/h gives 63.4 m, the book's
  table rounds to 65) and the clear offset it needs from the inside lane's centre, HSO = R·(1 − cos(28.65·SSD/R
  degrees)) (Eq 3-37). A placed building inside that band blocks it; with no buildings, each curve's needed offset is
  listed to keep clear.
- **Intersection sight** (§9.5.3, Case B1): at junctions without signals, the major road (fastest, then most
  lanes); along it b = 0.278·V·t_g (Eq 9-1; the book's example, 100 km/h and 7.5 s, gives 208.5 m) with t_g from
  Table 9-6 (car 7.5 s, single-unit truck 9.5, combination truck 11.5; 0.5 s or 0.7 s more a lane crossed beyond
  one) for the largest vehicle type; the triangle from the junction, the minor road's decision point (4.4 m back
  from the major road's edge) and the point b along the major road, each side, each minor road. A building whose
  square footprint enters a triangle blocks it.
- **Turning paths** (§2.8, Table 2-5b): every junction turn the simulation drives (its lane-centre curve, not ring
  turns or straight on), its tightest radius (circles through each three points) against the centreline turning
  radius of the largest design vehicle among the vehicle types (`design_vehicle`: car 6.40 m, city bus 11.52 m,
  single-unit truck 11.58 m, semitrailer 12.36 m: its 13.66 m design radius less half its width, the table's
  footnote; only these pairs could be read reliably from the copy). Reported per junction: how many turns are too
  tight and the tightest.
- **Skew** (§9.5.2): adjacent legs of a junction with 3 or more meeting at under 75°.
- **Junction spacing** (Towards Safer Roads in Developing Countries, TRL 1991, Ch 7 checklist B item 12): a road
  between two junctions on an arterial (2 or more lanes each way, or 60 km/h or more) shorter than 250 m.
- **Lane balance** (§10.9.5.9): at a junction without signals, a merge of two links into one needs at least their
  lanes less one beyond it; a diverge into two, at least their lanes less one before it.
- **Weaving** (HCM 2000 Ch 24, weaving.md): each merge followed within 750 m by a diverge, its type and what it can
  take (weaving flow, volume ratio); an on-ramp and off-ramp that close with no auxiliary lane (the merge and
  diverge interact; one between them makes a Type A weave); a layout Exhibit 24-5 calls infeasible. `weaving_los`
  grades them with the demand.
- Not checked, and said: acceleration and deceleration lane lengths (Tables 10-4, 10-6 are images in the copy),
  grades (the network has none), access within 50 m of a junction (checklist items 11 and 37; here a building's
  door is a junction).

## design_checks.py: the network's shape (tools `analyse_network`, `network_indices`)

- `analyse_network(network)`: whether it validates; its separate parts; junctions traffic from the boundary ends
  can't reach or can't leave (one-way roads kept); dead-end and unconnected junctions; roads under 15 m; curves
  posted faster than their tightest bend allows (`interchanges.curve_speed_kmh`); roundabouts whose radius crowds a
  road at most 1.5 radii long. View Summary's **Design check** shows it.
- `network_indices(network)`: graph indices (Rodrigue, *The Geography of Transport Systems*): vertices, edges,
  components, cyclomatic μ, α, β, γ, the diameter in links, degrees, the detour index between boundary ends and
  Shimbel accessibility. A run's workbook carries them.
- Helpers the assistant's tools share: `roads_at` (a junction's roads), `tightest` (a road's sharpest curvature),
  `route_lengths` (shortest metres from each source, one-way roads kept). View Summary and the workbook read these
  without loading network_tools; the tools of the same names call them.

## accessibility.py (tool `accessibility_report`)

Levine, From Mobility to Accessibility (Eq 3.1, Appendix A); Rodrigue, The Geography of Transport Systems. Times
are shortest paths over the roads (`link_minutes`, one-way roads kept): free flow at the speed limits, or a run's
measured link traversal times (3 or more traversals) when the tool is given a run id.

- Opportunities sit at each building's door junction (`institutions.door`): jobs (every building's employees),
  school places (schools' students), hospitals and shops (counted).
- From each junction: the opportunities within each time budget (cumulative, default 15 and 30 min), Hansen's
  potential accessibility to jobs A_i = Σ W_j·exp(−β·t_ij) (β 0.1 a minute by default), and the minutes to the
  nearest hospital and school.
- For the residents (Residential buildings, at their doors): the people-weighted means, the least-served
  buildings, and the Gini of jobs access weighted by residents (`weighted_gini`).
- `close_road`: the same with that road closed and each residents' measure's loss in percent (Taylor, Vulnerability
  Analysis for Transportation: accessibility loss); minutes to the nearest school and hospital before and after.

## Vulnerability (network_tools.py, tool `critical_links`)

- `critical_links_plan` (unchanged): each used road closed in turn at free flow, the extra vehicle-hours and demand
  cut off; now also `cut_elements`.
- `cut_elements`: Tarjan's bridges and articulation points on the undirected road graph (iterative, no recursion
  limit): the bridge roads and cut junctions, each with everything it splits from the main part. Boundary roads
  (always bridges) and junctions that only cut off their own boundary ends are left out.
- `area` {"x", "y", "radius_m"}: every road touching that circle closed at once (`_area_closure`; Taylor Ch 6,
  area-wide disruption): the extra vehicle-hours and the demand cut off, static.

## Structured errors (network_tools.explain_error)

Every refused tool call reaches the model as {"error", "error_type", "suggested_fix"} (ChatSUMO's structured errors,
so a model can recover): unknown_id (with `did_you_mean`, the network's ids nearest by spelling), unroutable, busy,
no_demand, bad_argument, no_change, refused (`ERROR_KINDS`). `ai_mode._execute` returns it in place of the bare
message.
