r"""Update ACMS from GitHub: launch.bat runs .venv\Scripts\python.exe tools\update.py before opening ACMS.

A git clone pulls (fast-forward only, so local commits or edits are never touched); a downloaded folder compares
.acms-version with the newest commit on GitHub and copies the newer files over it, deleting the files the last
version shipped that the new one no longer has (never a file ACMS didn't ship: your own extension stays).
No internet: nothing happens. ACMS_NO_UPDATE set (to anything): nothing happens either.
Exit code 3 means ACMS was updated (and reinstalled), so launch.bat starts again from its new copy.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = "AscendesShady/ACMS"
ROOT = Path(__file__).resolve().parent.parent
VERSION = ROOT / ".acms-version"
UPDATED = 3


def git_update() -> bool:
    if not shutil.which("git"):
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
    before = set(version.read_text().splitlines()[1:]) if version.exists() else set()
    # Extract on the same drive, then os.replace file by file: a full disk never leaves a half-written file
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
        (top,) = Path(tmp).iterdir()  # GitHub zips hold one folder, ACMS-<sha>
        shipped = sorted(src.relative_to(top).as_posix() for src in top.rglob("*") if src.is_file())
        for name in shipped:
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(top / name, dest)
    for name in before - set(shipped):
        gone = (root / name).resolve()
        if gone.is_relative_to(root.resolve()):  # the list is ours, but a path is never followed out of the folder
            gone.unlink(missing_ok=True)
    tmp_version = version.with_suffix(".new")
    tmp_version.write_text("\n".join([sha, *shipped]))
    os.replace(tmp_version, version)


def main() -> int:
    if os.environ.get("ACMS_NO_UPDATE"):  # a developer's clone, or a version to stay on
        return 0
    try:
        updated = git_update() if (ROOT / ".git").exists() else zip_update()
    except (OSError, subprocess.TimeoutExpired, zipfile.BadZipFile) as e:  # offline, GitHub down, rate-limited
        print("Update check skipped:", e)
        return 0
    if not updated:
        return 0
    print("Updated: installing...")
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-e", str(ROOT)])
    return UPDATED


if __name__ == "__main__":
    sys.exit(main())
