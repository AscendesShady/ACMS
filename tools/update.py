r"""Update ACMS from GitHub: launch.bat runs .venv\Scripts\python.exe tools\update.py before opening ACMS.

A git clone pulls (fast-forward only, so local commits or edits are never touched); a downloaded folder compares
.acms-version with the newest commit on GitHub and copies the newer files over it, deleting the files the last
version shipped that the new one no longer has (never a file ACMS didn't ship: your own extension stays).
No internet: nothing happens. ACMS_NO_UPDATE set (to anything): nothing happens either.
Exit code 3 means ACMS was updated (and reinstalled), so launch.bat starts again from its new copy. A reinstall that
fails leaves .acms-reinstall, so the next launch tries it again (an update's new package would be missing for ever).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "AscendesShady/ACMS"
ROOT = Path(__file__).resolve().parent.parent
VERSION = ROOT / ".acms-version"
REINSTALL = ROOT / ".acms-reinstall"  # updated, not yet reinstalled: the next launch reinstalls
UPDATED = 3
# Files ACMS shipped once and no longer does: deleted by the first update of a folder with no .acms-version yet (a
# downloaded folder lists nothing before then; an old extension would load for ever, extensions load by folder scan)
RETIRED = ("extensions/dispatch_emergency.py",)


def git_update() -> bool:
    if not shutil.which("git"):  # a clone made with GitHub Desktop, which keeps its git to itself
        print("Update skipped: this folder is a git clone but git isn't on PATH (https://git-scm.com/download/win).")
        return False
    head = lambda: subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
    before = head()
    pull = subprocess.run(["git", "pull", "--ff-only", "-q"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    if pull.returncode:
        print("Update skipped:", (pull.stderr.strip().splitlines() or ["git pull failed"])[-1])
    return head() != before


def zip_update() -> bool:
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/main",
                                 headers={"Accept": "application/vnd.github.sha", "User-Agent": "ACMS-updater"})
    latest = urllib.request.urlopen(req, timeout=5).read().decode().strip()
    if VERSION.exists() and VERSION.read_text().split("\n", 1)[0].strip() == latest:
        return False
    print("Updating ACMS...")
    install_zip(ROOT, latest, urllib.request.urlopen(f"https://github.com/{REPO}/archive/{latest}.zip", timeout=60).read())
    return True


def install_zip(root: Path, sha: str, data: bytes) -> None:
    """A GitHub zip of commit `sha` over the folder `root`. .acms-version keeps the commit and, a line each, the
    files it shipped: a file the last version shipped that this one doesn't is deleted (extensions load by folder
    scan, so a removed one would load for ever), and a file ACMS never shipped is never touched."""
    version = root / VERSION.name
    before = set(version.read_text().splitlines()[1:]) if version.exists() else set(RETIRED)
    # Each file written beside its place, then os.replace: a full disk never leaves a half-written file, and no path
    # is longer than the file's own (a temporary folder of ACMS-<sha> would add ~60 characters: Windows' 260 limit)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [(info, info.filename.split("/", 1)[1]) for info in archive.infolist() if not info.is_dir()]
        shipped = sorted(name for _, name in members)  # GitHub zips hold one folder, ACMS-<sha>: dropped

        def put(info, name: str) -> None:
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".acms-part")
            try:
                part.write_bytes(archive.read(info))
                os.replace(part, dest)  # a file open in Word fails here: no version written, the next launch retries
            finally:
                part.unlink(missing_ok=True)

        # launch.bat last: cmd reads it as it runs, so it changes only once everything else has (a failure before
        # it leaves the running launch.bat whole, and launch.bat restarts only after a whole update)
        for info, name in members:
            if name != "launch.bat":
                put(info, name)
        left = []  # files to delete that are open somewhere: listed again, so the next update deletes them
        for name in sorted(before - set(shipped)):
            gone = (root / name).resolve()
            if gone.is_relative_to(root.resolve()):  # the list is ours, but a path is never followed out of the folder
                try:
                    gone.unlink(missing_ok=True)
                except OSError:
                    left.append(name)
        for info, name in members:
            if name == "launch.bat":
                put(info, name)
    tmp_version = version.with_suffix(".new")
    tmp_version.write_text("\n".join([sha, *shipped, *left]))
    os.replace(tmp_version, version)


def main() -> int:
    if os.environ.get("ACMS_NO_UPDATE"):  # a developer's clone, or a version to stay on
        return 0
    try:
        updated = git_update() if (ROOT / ".git").exists() else zip_update()
    except (OSError, subprocess.TimeoutExpired, zipfile.BadZipFile) as e:  # offline, GitHub down, rate-limited
        print("Update check skipped:", e)
        updated = False
    if not updated and not REINSTALL.exists():
        return 0
    REINSTALL.touch()
    print("Updated: installing...")
    pip = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-e", str(ROOT)])
    if pip.returncode:  # the next launch tries again; files just replaced still restart launch.bat (its new copy)
        print("Install failed: ACMS opens as it is, and the next launch tries again.")
        return UPDATED if updated else 0  # a retry that fails opens ACMS: never a restart loop
    REINSTALL.unlink(missing_ok=True)
    return UPDATED


if __name__ == "__main__":
    sys.exit(main())
