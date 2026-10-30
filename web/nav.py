"""The console's pages, in one registry: a new page is one router module and one entry here.

Pages follow the demo's story: the agent works and a person approves, the rules come from
the policy and from the learner, then the record is checked. ``demo`` pages appear only with
``DEMO_MODE=true``.
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
    Page("/", "Overview", "Start", "Health of Gate and the ledger, and what happened today"),
    Page("/agent", "Run the agent", "Payments", "Give the payments agent a task, watch each step"),
    Page("/approvals", "Approvals", "Payments", "Requests Gate is holding for a supervisor"),
    Page("/policy", "Rules in force", "Rules", "The bank's rules: draft from the PDF, approve"),
    Page("/learn", "Suggested changes", "Rules", "Rule changes the learner proposes from history"),
    Page("/audit", "Ask the record", "Proof", "Ask about the history; every claim is checked"),
    Page("/ledger", "Ledger", "Proof", "Every record, verified the way an auditor would"),
    Page("/demo", "Attacks", "Demo", "Tamper with a record or rewrite history", demo=True),
)
GROUPS = ("Start", "Payments", "Rules", "Proof", "Demo")


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
