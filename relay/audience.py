"""The human-facing projection of herdr-projects pane metadata.

The relay keeps the raw pane stream for every client. Notifications use this projection so a
coordinator's internal workers do not each become a separate request to the person.
"""
import re


THREAD_NAME = re.compile(r"^hp-.+-t-\d+")
THREAD_CWD = re.compile(r"/threads/t-\d+/?$")
WORKTREE_CWD = re.compile(r"/worktrees/hp-.+-t-\d+(?:[-/]|$)")


def primary_session(rec: dict) -> bool:
    """A coordinator or hand-started session; legacy panes fail open."""
    role = (rec.get("hp_role") or "").strip()
    if role:
        return role in {"coordinator", "session"}
    name = (rec.get("label") or "").strip()
    if name.startswith("hpc-"):
        return True
    if THREAD_NAME.match(name):
        return False
    cwd = rec.get("cwd") or ""
    if THREAD_CWD.search(cwd) or WORKTREE_CWD.search(cwd):
        return False
    return not re.fullmatch(r"t-\d+", (rec.get("project") or "").strip())


def human_thread(rec: dict) -> bool:
    """A project has explicitly escalated this live thread to its human owner.

    `human` includes reports and failures; `stalled` is an internal question that outlived the
    coordinator's response window. Internal prompts stay with the coordinator.
    """
    return ((rec.get("hp_role") or "").strip() == "thread"
            and (rec.get("hp_audience") or "").strip() in {"human", "stalled"})


def pane_human(rec: dict) -> bool:
    return primary_session(rec) or human_thread(rec)


def effective_project(rec: dict) -> str:
    """Registered slug first; cwd basename is only a legacy fallback."""
    return ((rec.get("hp_project") or "").strip()
            or (rec.get("project") or "").strip()
            or (rec.get("workspace_label") or "").strip()
            or "unknown")


def notify_blocked(rec: dict) -> bool:
    """Both initial prompts and internal -> stalled upgrades pass this one gate."""
    return pane_human(rec)
