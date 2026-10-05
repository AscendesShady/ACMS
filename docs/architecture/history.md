# history.py

`History(state, limit=100)`:
- `commit(state, merge_key)` pushes the previous state unless it is the same key within
  1.5 s (`MERGE_SECONDS`).
- `undo()` and `redo()` return the state to restore.
