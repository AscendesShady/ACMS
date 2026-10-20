# lane_graph.py: the lane graph (no Qt)

A network snapshot as the simulation drives it: `Graph(network)` builds its `Link`s (one road one way), their
`Lane`s, the `Turn`s through each junction, bus stops' `Bay`s and each signalised junction's `Signal`, and answers
routes (`route`, `route_shares` by `path_size_logit`), move times and control delays, demand assignment, degrees of
saturation and queue storage. The give-way capacities measured on the simulated drivers (`PRIORITY_CAPACITY_VPH`,
`ROUNDABOUT_ENTRY_VPH`, read by `measured_capacity`) and the turn, priority and bay constants live here too.

What each part does, and why, is in simulation.md (**Lane graph** and the paragraphs that name `Graph`): one run
builds one Graph and only reads it after. Anything needing only routes or turns builds `Graph(network)` without a
`Simulation`, so no random streams or signal plans: the window's route checks and DEVL dispatches, design_checks'
turns, weaving's route shares and the assistant's tools. simulation.py imports every name back, so
`from traffic_simulator.simulation import Graph` (and the rest) still works.
