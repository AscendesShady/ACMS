"""View Summary pages. Run with `python tests/test_summary.py` (or the test monitor)."""

from traffic_simulator.interchanges import build_trumpet
from traffic_simulator.network_generator import generate_network
from traffic_simulator.od_matrix import ODCell
from traffic_simulator.summary import ai_markdown, demand_markdown, network_markdown


def test_network_page_reports_layout_flows_and_linked_problems():
    network = generate_network(3, 4)
    network.nodes["2,1"].control = "roundabout"
    network.approach_nodes["1E"].supply_vph = 600
    build_trumpet(network, ("1,2", "3,2"), "2,3")
    text = network_markdown(network)
    assert "1 roundabout" in text and "TR1 (trumpet)" in text and "curved" in text
    assert "**Supply (entering):** 600 veh/h" in text and "600 veh/h off balance" in text
    # The trumpet left 2,2 with only its road to 2,1: a dead end, linked to the map.
    assert "### Design check: 1 problem" in text and "**Dead-end junctions:** [2,2](select:2%2C2)" in text
    network.add_node(5000, 5000)  # a junction with no roads
    text = network_markdown(network)
    assert "### Design check: 5 problems" in text and "**Unconnected junctions:** [N1](select:N1)" in text
    assert "**The network is in 2 separate parts**" in text  # a lone junction is its own part too
    assert "### Design check: no problems" in network_markdown(generate_network(2, 2))
    network.add_node(6000, 5000)
    assert "[N2](select:N2)" in network_markdown(network)  # both listed, each a link


def test_demand_page_checks_od_against_the_ends():
    network = generate_network(3, 4)
    network.approach_nodes["1W"].supply_vph = 300
    network.approach_nodes["1E"].supply_vph = 600
    cells = {("1W", "1E"): ODCell(flow=300, split=(20, 80, 0)), ("1E", "1W"): ODCell(flow=100),
             ("2W", "2E"): ODCell(daily=2400)}
    text, by_hour = demand_markdown(network, cells, ["1W", "1E", "2W"], ["1W", "1E", "2E"])
    assert "Hourly: 2 of 7 pairs · 400 veh/h" in text and "Bus 18% · Car 81% · Bike 1%" in text
    assert "Daily: 1 of 7 pairs · 2400 veh/day" in text and "![Daily demand in each hour](chart:hourly)" in text
    assert abs(sum(by_hour) - 2400) < 1e-6 and "Busiest hour **16:00–17:00" in text
    assert "| 1W | 300 / 300 | 100 / 0 | **Check** |" in text  # OD in 100, no demand set
    assert "| 1E | 100 / 600 | 300 / 0 | **Check** |" in text and "2 of 2 ends don't match" in text
    assert "1W → 1E 300 veh/h · 1E → 1W 100 veh/h" in text
    empty, none = demand_markdown(network, {}, ["1W"], ["1E"])
    assert none is None and "No daily demand yet" in empty


def test_ai_page_reports_requests_and_local_memory():
    usage = [{"provider": "local", "model": "llama3.1:8b", "at": 0, "first_s": 0.5, "total_s": 2.0,
              "input_tokens": 9000, "output_tokens": 80, "cached_tokens": None},
             {"provider": "grok", "model": "grok-4.3", "at": 0, "first_s": 1.5, "total_s": 3.0,
              "input_tokens": 1000, "output_tokens": 20, "cached_tokens": 800}]
    loaded = [{"name": "llama3.1:8b", "size": 6_000_000_000, "size_vram": 4_500_000_000, "context_length": 16384}]
    text = ai_markdown(["System 2: Llama is watching (llama3.1:8b)", "Conversation: 4 messages"], usage, loaded)
    assert text.startswith("**System 2: Llama is watching") and "- Conversation: 4 messages" in text
    assert "2 (Ollama 1, Grok 1)" in text and "10,000 in (800 cached) · 100 out" in text
    assert "1.0 s on average (fastest 0.5 s)" in text and "| grok-4.3 | 1.5 s | 3.0 s | 1,000 → 20 |" in text
    assert "| llama3.1:8b | 6.0 GB | 4.5 GB (75%) | 16,384 tokens |" in text
    assert "Ollama isn't running" in ai_markdown([], [], None) and "No model requests yet" in ai_markdown([], [], [])


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
