"""Procedural generation of orthogonal road networks."""

from __future__ import annotations

from .models import SIGNAL_METHODS, Network, Node, Road


def add_boundary_approaches(network: Network) -> None:
    """Add model-backed exterior ends to an existing grid network.

    Ends are named for trip assignment by side and graph coordinate: east and
    west ends count up from the bottom row (1E, 2E, ...), north and south ends
    count up from the left column (1N, 1S, ...). The end node and its road
    share that name.
    """
    approach_length = max(float(network.default_spacing) * 0.48, 180.0)
    rows, columns = network.horizontal_corridors, network.vertical_corridors
    grid = {(node.row, node.column): node for node in network.nodes.values()}

    def add_approach(name: str, inner: Node, dx: float, dy: float,
                     row: int, column: int, outer_first: bool) -> None:
        network.approach_nodes[name] = Node(
            id=name, x=inner.x + dx, y=inner.y + dy, row=row, column=column
        )
        network.approach_roads[name] = Road(
            id=name,
            from_node=name if outer_first else inner.id,
            to_node=inner.id if outer_first else name,
            length_m=approach_length,
            lanes_per_direction=network.default_lanes_per_direction,
            lane_changing=network.default_lane_changing,
            speed_limit_kmh=network.default_speed_limit_kmh,
        )

    for row in range(rows):
        number = rows - row
        if (row, 0) in grid:  # a deleted edge junction has no end
            add_approach(f"{number}W", grid[row, 0], -approach_length, 0, row, -1, True)
        if (row, columns - 1) in grid:
            add_approach(f"{number}E", grid[row, columns - 1], approach_length, 0,
                         row, columns, False)
    for column in range(columns):
        if (0, column) in grid:
            add_approach(f"{column + 1}N", grid[0, column], 0, -approach_length, -1, column, True)
        if (rows - 1, column) in grid:
            add_approach(f"{column + 1}S", grid[rows - 1, column], 0, approach_length,
                         rows, column, False)


def generate_network(
    horizontal_corridors: int,
    vertical_corridors: int,
    default_spacing: float = 500.0,
    driving_side: str = "left",
    speed_limit_kmh: float = 50.0,
    lanes_per_direction: int = 2,
    lane_changing: bool = True,
    signal_method: str = "webster",
) -> Network:
    if not 1 <= horizontal_corridors <= 20:
        raise ValueError("horizontal (rows, roads running east-west) must be 1 to 20.")
    if not 1 <= vertical_corridors <= 20:
        raise ValueError("vertical (columns, roads running north-south) must be 1 to 20.")
    if default_spacing <= 0:
        raise ValueError("Default spacing must be positive.")
    if driving_side not in {"left", "right"}:
        raise ValueError("Driving side must be 'left' or 'right'.")
    if speed_limit_kmh <= 0:
        raise ValueError("Speed limit must be positive.")
    if lanes_per_direction < 1:
        raise ValueError("Lanes per direction must be at least one.")
    if signal_method not in SIGNAL_METHODS:
        raise ValueError(f"Signal timing must be one of {', '.join(SIGNAL_METHODS)}.")

    network = Network(
        horizontal_corridors=horizontal_corridors,
        vertical_corridors=vertical_corridors,
        default_spacing=float(default_spacing),
        driving_side=driving_side,
        default_speed_limit_kmh=float(speed_limit_kmh),
        default_lanes_per_direction=lanes_per_direction,
        default_lane_changing=lane_changing,
        default_signal_method=signal_method,
    )

    def node_id(row: int, column: int) -> str:
        # Graph coordinates "x,y": bottom-left intersection is 1,1.
        return f"{column + 1},{horizontal_corridors - row}"

    for row in range(horizontal_corridors):
        for column in range(vertical_corridors):
            identifier = node_id(row, column)
            network.nodes[identifier] = Node(
                id=identifier,
                x=column * float(default_spacing),
                y=row * float(default_spacing),
                row=row,
                column=column,
                signal_method=signal_method,
            )

    for row in range(horizontal_corridors):
        for column in range(vertical_corridors - 1):
            network.add_road(node_id(row, column), node_id(row, column + 1))

    for column in range(vertical_corridors):
        for row in range(horizontal_corridors - 1):
            network.add_road(node_id(row, column), node_id(row + 1, column))

    add_boundary_approaches(network)

    network.validate()
    return network
