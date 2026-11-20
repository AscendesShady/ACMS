"""tools/update.py: a downloaded folder updated from a zip, and the switch that turns updating off.
Run with `python tests/test_update.py` (or the test monitor)."""

import importlib.util
import io
import os
import tempfile
import zipfile
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("update", Path(__file__).resolve().parents[1] / "tools" / "update.py")
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


def archive(sha: str, files: dict) -> bytes:
    """A zip as GitHub makes one: every file under one folder, ACMS-<sha>."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as made:
        for name, text in files.items():
            made.writestr(f"ACMS-{sha}/{name}", text)
    return data.getvalue()


def test_a_zip_update_replaces_files_and_deletes_only_what_acms_shipped_before():
    root = Path(tempfile.mkdtemp())
    (root / "extensions").mkdir()
    (root / "extensions" / "mine.py").write_text("my own")  # the user's drop-in extension: never ACMS's to delete
    (root / "extensions" / "old.py").write_text("v0")  # a downloaded folder before its first update: no list yet
    update.install_zip(root, "a1", archive("a1", {"extensions/old.py": "v1", "src/kept.py": "v1", "README.md": "v1"}))
    assert (root / ".acms-version").read_text().splitlines() == ["a1", "README.md", "extensions/old.py", "src/kept.py"]
    assert (root / "extensions" / "old.py").read_text() == "v1" and (root / "extensions" / "mine.py").exists()
    update.install_zip(root, "b2", archive("b2", {"src/kept.py": "v2", "README.md": "v2"}))  # old.py removed upstream
    assert not (root / "extensions" / "old.py").exists()  # it would have loaded for ever (extensions load by scan)
    assert (root / "extensions" / "mine.py").read_text() == "my own"
    assert (root / "src" / "kept.py").read_text() == "v2"
    assert (root / ".acms-version").read_text().splitlines() == ["b2", "README.md", "src/kept.py"]
    assert sorted(p.name for p in root.iterdir()) == [".acms-version", "README.md", "extensions", "src"]  # no litter


def test_a_first_update_deletes_retired_files_and_writes_no_longer_paths_than_its_own():
    root = Path(tempfile.mkdtemp())
    (root / "extensions").mkdir()
    (root / "extensions" / "dispatch_emergency.py").write_text("old")  # shipped once; a folder with no list yet
    deep = "src/" + "d" * 40 + "/x.py"
    update.install_zip(root, "c3", archive("c3", {deep: "v", "README.md": "v"}))
    assert not (root / "extensions" / "dispatch_emergency.py").exists()  # it would load for ever
    assert (root / deep).read_text() == "v"
    assert not list(root.rglob("*.acms-part")) and sorted(p.name for p in root.iterdir()) == [".acms-version", "README.md", "extensions", "src"]


def test_a_failed_reinstall_is_tried_again_next_launch_without_a_restart_loop():
    root = Path(tempfile.mkdtemp())
    kept = update.git_update, update.zip_update, update.subprocess.run, update.REINSTALL, update.ROOT
    codes = iter([1, 1, 0])  # pip fails after the update, fails again on the retry, then works
    update.REINSTALL, update.ROOT = root / ".acms-reinstall", root
    update.subprocess.run = lambda *args, **kwargs: type("Done", (), {"returncode": next(codes)})()
    try:
        update.zip_update = lambda: True  # files replaced: launch.bat restarts from its new copy all the same
        assert update.main() == update.UPDATED and update.REINSTALL.exists()
        update.zip_update = lambda: False  # the restarted launch: nothing new, the reinstall tried and failed again
        assert update.main() == 0 and update.REINSTALL.exists()  # ACMS opens: no loop
        assert update.main() == update.UPDATED and not update.REINSTALL.exists()  # a later launch: installed
        assert update.main() == 0  # and then nothing to do
    finally:
        update.git_update, update.zip_update, update.subprocess.run, update.REINSTALL, update.ROOT = kept


def test_acms_no_update_makes_the_updater_do_nothing():
    def never():
        raise AssertionError("it looked for an update")

    kept, update.git_update, update.zip_update = (update.git_update, update.zip_update), never, never
    os.environ["ACMS_NO_UPDATE"] = "1"
    try:
        assert update.main() == 0
    finally:
        del os.environ["ACMS_NO_UPDATE"]
        update.git_update, update.zip_update = kept


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
