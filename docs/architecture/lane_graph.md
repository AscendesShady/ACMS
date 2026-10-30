# lane_graph.py: the lane graph (no Qt)

A network snapshot as the simulation drives it: `Graph(network)` builds its `Link`s (one road one way), their
`Lane`s, the `Turn`s through each junction, bus stops' `Bay`s and each signalised junction's `Signal`, and answers
routes (`route`, `route_shares` by `path_size_logit`; `bike_route`, a cyclist's: the least perceived time, a
link with a cycle lane at `CYCLE_LANE_TIME_FACTOR` 0.7 of its `free_move_time`, an assumption after Broach, Dill &
Gliebe 2012's finding that bike lanes about exactly offset adjacent traffic; with no cycle lanes it is `route`'s), move times and control delays, demand assignment, degrees of
saturation and queue storage. The give-way capacities measured on the simulated drivers (`PRIORITY_CAPACITY_VPH`,
`ROUNDABOUT_ENTRY_VPH`, read by `measured_capacity`) and the turn, priority and bay constants live here too.
`two_phases(arms)` splits a two-phase signal's arms by the junction's own roads (simulation.md, `Signal`).
A `Link`'s cycle lane (`cycle`) is kept out of `lanes`; `all_lanes()` gives both, as `all_turns()` gives its
`turns` and `bike_turns`: what a detector or a count of its vehicles covers.

What each part does, and why, is in simulation.md (**Lane graph** and the paragraphs that name `Graph`): one run
builds one Graph and only reads it after. Anything needing only routes or turns builds `Graph(network)` without a
`Simulation`, so no random streams or signal plans: the window's route checks and DEVL dispatches, design_checks'
turns, weaving's route shares and the assistant's tools. simulation.py imports every name back, so
`from traffic_simulator.simulation import Graph` (and the rest) still works.
