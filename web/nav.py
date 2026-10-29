"""The console's pages, in one registry: a new page is one router module and one entry here.

Groups follow what people come to do: operate the agent, govern its rules, prove what
happened. ``demo`` pages appear only with ``DEMO_MODE=true``.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    path: str
    title: str
    group: str
    hint: str  # one line under the title in the navigation, for screen readers and tooltips
    demo: bool = False


PAGES = (
    Page("/", "Overview", "Operate", "Health of Gate and the ledger, and what happened today"),
    Page("/approvals", "Approvals", "Operate", "Requests held by Gate, waiting for a person"),
    Page("/agent", "Agent", "Operate", "Give the payments agent a task and watch every step"),
    Page("/policy", "Policy", "Govern", "Draft rules from the policy PDF, review and approve"),
    Page("/learn", "Learner", "Govern", "Rule changes proposed from the ledger's history"),
    Page("/ledger", "Ledger", "Prove", "Every record, verified the way an auditor would"),
    Page("/audit", "Audit", "Prove", "Ask about the history; every claim is checked"),
    Page("/demo", "Attacks", "Demo", "Tamper with a record or rewrite history", demo=True),
)
GROUPS = ("Operate", "Govern", "Prove", "Demo")


def navigation(*, demo_mode: bool) -> list[tuple[str, list[Page]]]:
    shown = [p for p in PAGES if demo_mode or not p.demo]
    return [
        (g, [p for p in shown if p.group == g]) for g in GROUPS if any(p.group == g for p in shown)
    ]


def current(path: str) -> Page | None:
    """The page a path belongs to: the longest registered prefix."""
    matches = [
        p for p in PAGES if path == p.path or (p.path != "/" and path.startswith(p.path + "/"))
    ]
    return max(matches, key=lambda p: len(p.path), default=None)
