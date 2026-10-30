"""The pages of the two web apps, in one registry: a new page is one router module and one entry.

- The **Console** (``pl-web``) is for operating and proving: run the agent, draft rules, run
  the learner, ask the record, inspect the ledger. Its pages follow the demo's story.
- The **Approvals** app (``pl-approvals``) is for signing: one inbox of everything a person
  must decide (held payments, policy drafts, learner suggestions) and what they decided. It
  serves no drafting, admin or attack route at all (D94).

``demo`` pages appear only with ``DEMO_MODE=true``.
"""

from dataclasses import dataclass

CONSOLE, APPROVALS = "console", "approvals"


@dataclass(frozen=True)
class Page:
    path: str
    title: str
    group: str
    hint: str  # one line under the title in the navigation, for screen readers and tooltips
    demo: bool = False


PAGES = {
    CONSOLE: (
        Page("/", "Overview", "Start", "Health of Gate and the ledger, and what happened today"),
        Page("/agent", "Run the agent", "Payments", "Give the payments agent a task, watch it"),
        Page("/policy", "Rules in force", "Rules", "The bank's rules: draft them from the PDF"),
        Page("/learn", "Suggested changes", "Rules", "Rule changes the learner proposes"),
        Page("/audit", "Ask the record", "Proof", "Ask about the history; every claim is checked"),
        Page("/ledger", "Ledger", "Proof", "Every record, verified the way an auditor would"),
        Page("/demo", "Attacks", "Demo", "Tamper with a record or rewrite history", demo=True),
    ),
    APPROVALS: (
        Page("/", "Waiting for you", "Decide", "Everything held until a person signs"),
        Page("/decided", "Decided", "Decide", "Verdicts and approvals you gave, newest first"),
    ),
}
GROUPS = {CONSOLE: ("Start", "Payments", "Rules", "Proof", "Demo"), APPROVALS: ("Decide",)}
TITLES = {CONSOLE: "Console", APPROVALS: "Approvals"}


def navigation(app: str = CONSOLE, *, demo_mode: bool) -> list[tuple[str, list[Page]]]:
    shown = [p for p in PAGES[app] if demo_mode or not p.demo]
    return [
        (g, [p for p in shown if p.group == g])
        for g in GROUPS[app]
        if any(p.group == g for p in shown)
    ]


def current(path: str, app: str = CONSOLE) -> Page | None:
    """The page a path belongs to: the longest registered prefix."""
    matches = [
        p for p in PAGES[app] if path == p.path or (p.path != "/" and path.startswith(p.path + "/"))
    ]
    return max(matches, key=lambda p: len(p.path), default=None)
