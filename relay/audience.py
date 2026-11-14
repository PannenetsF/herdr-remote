"""The human-facing projection of herdr-projects pane metadata.

The relay keeps the raw pane stream for every client. The human surface (Projects view, default
timeline/cues and Web Push) shows only primary sessions -- coordinators and hand-started sessions,
plus legacy panes that fail open. A worker thread is NEVER a human-facing row on its own, no matter
its ``hp_audience``. Human/stalled thread escalations are aggregated onto the owning coordinator in
the same (host, project), so one project's worth of workers produces at most one notification.
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


def is_thread(rec: dict) -> bool:
    return (rec.get("hp_role") or "").strip() == "thread"


def escalated_thread(rec: dict) -> bool:
    """A worker its project has handed to the human: `human` reports or `stalled` questions."""
    return is_thread(rec) and (rec.get("hp_audience") or "").strip() in {"human", "stalled"}


# Projects rows are primary sessions only. Threads are aggregated, never rendered directly.
def pane_human(rec: dict) -> bool:
    return primary_session(rec)


def effective_project(rec: dict) -> str:
    """Registered slug first; cwd basename is only a legacy fallback."""
    return ((rec.get("hp_project") or "").strip()
            or (rec.get("project") or "").strip()
            or (rec.get("workspace_label") or "").strip()
            or "unknown")


def project_key(rec: dict) -> str:
    """Per (host, project): equal slugs on two machines stay separate, like the web projection."""
    return f"{rec.get('host') or 'local'}|{effective_project(rec)}"


def project_followups(records: list) -> dict:
    """Escalated worker threads grouped by their owning (host, project). Internal threads excluded."""
    grouped: dict = {}
    for rec in records:
        if not escalated_thread(rec):
            continue
        grouped.setdefault(project_key(rec), []).append(rec)
    return grouped


def primary_coordinator(records: list, key: str):
    """The earliest live coordinator for a project; None when the project has none.

    A project without a live coordinator gets no aggregate notification and no standalone worker
    surface -- its threads remain reachable only through the raw All-panes stream.
    """
    owner = None
    for rec in records:
        if (rec.get("hp_role") or "").strip() != "coordinator" or project_key(rec) != key:
            continue
        if owner is None or rec.get("order", 1 << 30) < owner.get("order", 1 << 30):
            owner = rec
    return owner


def notify_blocked(rec: dict) -> bool:
    """Only a primary session's own block pushes; workers are handled by the project aggregate."""
    return primary_session(rec)
