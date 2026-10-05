"""Undo/redo checks. Run with `python tests/test_history.py` (or the test monitor)."""

from traffic_simulator.history import History


def test_undo_and_redo_walk_the_steps():
    history = History("a")
    for state in "bc":
        assert history.commit(state)
    assert not history.commit("c")  # no change, no step
    assert (history.undo(), history.undo(), history.undo()) == ("b", "a", None)
    assert (history.redo(), history.redo(), history.redo()) == ("b", "c", None)


def test_a_new_change_clears_redo():
    history = History("a")
    history.commit("b")
    history.undo()
    history.commit("x")
    assert history.redo() is None and history.undo() == "a"


def test_typing_into_one_field_is_one_step():
    history = History(0)
    for step, value in enumerate((1, 12, 123)):
        history.commit(value, merge_key="flow", now=step * 0.5)
    history.commit(124, merge_key="flow", now=10.0)  # a later session: its own step
    assert history.undo() == 123 and history.undo() == 0 and history.undo() is None


def test_history_is_bounded():
    history = History(0, limit=3)
    for value in range(1, 10):
        history.commit(value)
    assert [history.undo() for _ in range(4)] == [8, 7, 6, None]


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
