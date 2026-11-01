"""The pages of the two web apps, in one registry: a new page is one router module and one entry.

- The **Console** (``pl-web``) is for operating and proving: run the agent, draft rules, run
  the learner, ask the record, inspect the ledger. Its pages follow the demo's story.
- The **Approvals** app (``pl-approvals``) is for signing: one inbox of everything a person
  must decide (held payments, policy drafts, learner suggestions) and what they decided. It
  serves no drafting, admin or attack route at all (D94).

``demo`` pages appear only with ``DEMO_MODE=true``.

Each page names the roles that may open it (D96); the menu shows only those, and every route
is refused by path for anyone else (``allowed``). A path no page or rule covers is refused.
Gate still checks every action against its own roles; this only decides which screens a
person sees, so nobody is offered a button Gate would refuse.
"""

from collections.abc import Collection
from dataclasses import dataclass

from web.logins import ADMIN, EXAMINER, OFFICER, SUPERVISOR

CONSOLE, APPROVALS = "console", "approvals"
EVERYONE = frozenset({SUPERVISOR, OFFICER, EXAMINER, ADMIN})
SUPERVISORS, OFFICERS = frozenset({SUPERVISOR}), frozenset({OFFICER})
APPROVERS = SUPERVISORS | OFFICERS


@dataclass(frozen=True)
class Page:
    path: str
    title: str
    group: str
    hint: str  # one line under the title in the navigation, for screen readers and tooltips
    roles: frozenset[str]
    demo: bool = False


PAGES = {
    CONSOLE: (
        Page("/", "Overview", "Start", "Health of Gate and the ledger, and what happened today",
             EVERYONE),
        Page("/agent", "Run the agent", "Payments", "Give the payments agent a task, watch it",
             SUPERVISORS),
        Page("/policy", "Rules in force", "Rules", "The bank's rules: draft them from the PDF",
             OFFICERS),
        Page("/learn", "Suggested changes", "Rules", "Rule changes the learner proposes",
             OFFICERS),
        Page("/improve", "Improve the learner", "Rules", "Revisions of the learner's prompt (D95)",
             OFFICERS),
        Page("/audit", "Ask the record", "Proof", "Ask about the history; every claim is checked",
             frozenset({OFFICER, EXAMINER})),
        Page("/ledger", "Ledger", "Proof", "Every record, verified the way an auditor would",
             EVERYONE),
        Page("/demo", "Attacks", "Demo", "Tamper with a record or rewrite history",
             frozenset({ADMIN}), demo=True),
    ),
    APPROVALS: (
        Page("/", "Waiting for you", "Decide", "Everything held until a person signs", APPROVERS),
        Page("/decided", "Decided", "Decide", "Verdicts and approvals you gave, newest first",
             APPROVERS),
    ),
}  # fmt: skip
# Routes that are not menu pages: the Approvals app's review pages and the payment queue.
RULES: dict[str, tuple[tuple[str, frozenset[str]], ...]] = {
    CONSOLE: (),
    APPROVALS: (
        ("/queue", SUPERVISORS), ("/escalations", SUPERVISORS),
        ("/policy", OFFICERS), ("/learn", OFFICERS), ("/improve", OFFICERS),
    ),
}  # fmt: skip
GROUPS = {CONSOLE: ("Start", "Payments", "Rules", "Proof", "Demo"), APPROVALS: ("Decide",)}
TITLES = {CONSOLE: "Console", APPROVALS: "Approvals"}


def navigation(
    app: str = CONSOLE, *, demo_mode: bool, roles: Collection[str]
) -> list[tuple[str, list[Page]]]:
    shown = [p for p in PAGES[app] if (demo_mode or not p.demo) and p.roles & set(roles)]
    return [
        (g, [p for p in shown if p.group == g])
        for g in GROUPS[app]
        if any(p.group == g for p in shown)
    ]


def current(path: str, app: str = CONSOLE) -> Page | None:
    """The page a path belongs to: the longest registered prefix."""
    matches = [p for p in PAGES[app] if _covers(p.path, path)]
    return max(matches, key=lambda p: len(p.path), default=None)


def _covers(prefix: str, path: str) -> bool:
    return path == prefix or (prefix != "/" and path.startswith(prefix + "/"))


def allowed(app: str, path: str, roles: Collection[str], *, demo_mode: bool) -> bool:
    """May someone with these roles open this path? The longest matching page or rule decides;
    no match means no (fail closed)."""
    pages = [(p.path, p.roles) for p in PAGES[app] if demo_mode or not p.demo]
    matches = [(prefix, who) for prefix, who in (*pages, *RULES[app]) if _covers(prefix, path)]
    if not matches:
        return False
    _, who = max(matches, key=lambda m: len(m[0]))
    return bool(who & set(roles))


def home(app: str, roles: Collection[str], *, demo_mode: bool) -> str | None:
    """The first page this person may open in this app, or None if there is none."""
    shown = [p for _, group in navigation(app, demo_mode=demo_mode, roles=roles) for p in group]
    return shown[0].path if shown else None
