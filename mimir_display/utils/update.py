"""
Client self-update helper.

Finds pull_and_update.sh (or update_display.sh) relative to the installation
and launches it in a detached subprocess so the calling process survives the
subsequent `systemctl restart mimir-display` that the script issues.

Resolution order for the script path:
  1. MIMIR_REPO_DIR env var (set by install.sh to the git checkout root)
  2. Walk up from __file__ until we find scripts/pull_and_update.sh
     (works for editable/dev installs where __file__ is inside the repo)
  3. Well-known installed path: /opt/mimir-display/scripts/
"""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

_SCRIPT_NAMES = ("pull_and_update.sh", "update_display.sh")
_FALLBACK_DIRS = ("/opt/mimir-display/scripts",)


def find_update_script() -> str | None:
    """Return the path to the best available update script, or None."""

    # 1. Explicit env var pointing at the repo / install dir
    repo_dir = os.environ.get("MIMIR_REPO_DIR", "").strip()
    if repo_dir:
        for name in _SCRIPT_NAMES:
            p = os.path.join(repo_dir, "scripts", name)
            if os.path.isfile(p):
                return p

    # 2. Walk up from __file__ (editable / dev installs)
    here = os.path.abspath(__file__)
    current = os.path.dirname(here)
    for _ in range(8):
        for name in _SCRIPT_NAMES:
            p = os.path.join(current, "scripts", name)
            if os.path.isfile(p):
                return p
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # 3. Well-known installed paths
    for d in _FALLBACK_DIRS:
        for name in _SCRIPT_NAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p

    return None


def trigger_update(
    git_branch: str = "main",
    dry_run: bool = False,
    log: logging.Logger | None = None,
) -> int | None:
    """
    Launch the update script in a detached subprocess.

    The subprocess is started with a new session (start_new_session=True) so
    it survives the `systemctl restart mimir-display` the script will issue.

    Returns the subprocess PID on success, or None if the script cannot be found.
    """
    _log = log or logger
    script = find_update_script()
    if not script:
        _log.warning(
            "update: script not found — set MIMIR_REPO_DIR to the git checkout root"
        )
        return None

    env = os.environ.copy()
    env["GIT_BRANCH"] = git_branch
    if dry_run:
        env["DRY_RUN"] = "1"

    _log.info("Launching update script: %s (branch=%s dry_run=%s)", script, git_branch, dry_run)

    proc = subprocess.Popen(
        ["bash", script],
        env=env,
        start_new_session=True,   # survive parent restart
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    _log.info("Update process started (pid=%d)", proc.pid)
    return proc.pid
