# network_generator.py: grids

- `generate_network(h, v, spacing=500, driving_side, speed_limit_kmh, lanes_per_direction,
  lane_changing, signal_method="webster")` builds the network in three steps:
  - Places junctions at `column × spacing, row × spacing`, each timed by `signal_method` (one of
    `SIGNAL_METHODS`, kept as the network's `default_signal_method`), and connects neighbours with two-way roads.
  - Adds boundary ends: E/W on every row, N/S on every column, each `max(0.48 × spacing, 180 m)` long.
- `add_boundary_approaches(network)` also rebuilds missing ends for old files.
