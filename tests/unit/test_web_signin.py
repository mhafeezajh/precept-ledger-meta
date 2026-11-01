"""Sign-in and role-based screens for the two web apps (D96): passwords stored only as scrypt
hashes, a lockout after repeated failures, and pages each role may open, refused by path for
everyone else. Gate still decides every action; this only decides which screens a person sees."""

import re
from pathlib import Path

import pytest

from common.errors import ConfigError
from scripts.dev_keys import ensure_keys
from tests.unit.test_web import LOGINS_TEXT, PASSWORDS, PERSONAS, sign_in, stack
from web.backend import WebError, load_personas
from web.logins import (
    ADMIN,
    EXAMINER,
    OFFICER,
    SUPERVISOR,
    SignIn,
    check_password,
    hash_password,
    load_logins,
    parse_logins,
    people,
)
from web.nav import APPROVALS, CONSOLE, allowed, home, navigation

FAST = 2**10  # TEST ONLY: a cheap scrypt cost, so the suite stays quick


# --- passwords and the logins file -------------------------------------------------------


def test_a_password_is_stored_only_as_a_salted_scrypt_hash() -> None:
    stored = hash_password("test-only-correct horse", n=FAST)
    assert stored.startswith(f"scrypt${FAST}$8$1$")
    assert "correct" not in stored
    assert stored != hash_password("test-only-correct horse", n=FAST)  # a fresh salt each time
    assert check_password("test-only-correct horse", stored)
    assert not check_password("test-only-correct hors", stored)
    assert not check_password("", stored)
    for broken in ("", "scrypt$x$8$1$AA$AA", "bcrypt$1$2$3$4$5", stored + "$extra", "scrypt"):
        assert not check_password("test-only-correct horse", broken)  # malformed: refused


def test_the_logins_file_names_people_and_their_roles() -> None:
    logins = parse_logins(LOGINS_TEXT)
    assert set(logins) == {"j.ortiz", "a.chen", "r.patel", "examiner", "demo-admin"}
    assert logins["examiner"].roles == frozenset({EXAMINER})
    assert "scrypt" not in repr(logins["j.ortiz"])  # the hash never appears in a repr
    found = people(logins, PERSONAS)
    assert found["j.ortiz"] is PERSONAS["j.ortiz"]  # a Gate person acts with their Gate token
    assert found["demo-admin"].kind == "web"  # a web-only person has no Gate token
    assert "payments-ops" not in found  # the agent never signs in


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("j.ortiz payments-supervisor\n", "expected"),
        ("j.ortiz root scrypt$1024$8$1$AA$AA\n", "unknown role"),
        ("j.ortiz payments-supervisor scrypt$1$2\n", "password hash"),
        ("j.ortiz examiner scrypt$1024$8$1$AA$AA\n", "Gate"),  # Gate says supervisor
        ("k.wu payments-supervisor scrypt$1024$8$1$AA$AA\n", "Gate"),  # not a Gate approver
        ("payments-ops examiner scrypt$1024$8$1$AA$AA\n", "Gate"),  # the agent's name
        ("x examiner scrypt$1024$8$1$AA$AA\nx examiner scrypt$1024$8$1$AA$AA\n", "twice"),
    ],
)
def test_a_wrong_logins_file_stops_the_app(text: str, problem: str) -> None:
    with pytest.raises(ConfigError, match=problem):
        people(parse_logins(text), PERSONAS)


def test_no_logins_file_means_nobody_can_sign_in(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="make dev-keys"):
        load_logins(tmp_path / "web-logins.txt")


def test_dev_keys_make_a_login_and_password_for_each_person(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    ensure_keys(keys)
    logins_file, passwords_file = keys / "web-logins.txt", keys / "web-passwords.txt"
    assert passwords_file.stat().st_mode & 0o777 == 0o600
    passwords = dict(line.split() for line in passwords_file.read_text().splitlines()
                     if line and not line.startswith("#"))  # fmt: skip
    logins = load_logins(logins_file)
    found = people(logins, load_personas(keys))
    assert set(found) == {"j.ortiz", "m.lee", "a.chen", "r.patel", "examiner", "demo-admin"}
    assert all(check_password(passwords[n], logins[n].password_hash) for n in found)
    assert all(p not in logins_file.read_text() for p in passwords.values())
    before = logins_file.read_text(), passwords_file.read_text()
    ensure_keys(keys)  # a second run adds what is missing and changes nothing else
    assert (logins_file.read_text(), passwords_file.read_text()) == before


# --- the lockout -------------------------------------------------------------------------


def test_five_wrong_passwords_lock_the_name_for_a_while() -> None:
    now = [1000.0]
    signin = SignIn(parse_logins(LOGINS_TEXT), clock=lambda: now[0])
    assert signin.check("a.chen", PASSWORDS["a.chen"]) is not None
    assert signin.check("a.chen", "wrong") is None
    assert signin.check("nobody", "wrong") is None  # an unknown name: the same answer
    for _ in range(4):
        assert signin.check("a.chen", "wrong") is None
    with pytest.raises(WebError) as locked:
        signin.check("a.chen", PASSWORDS["a.chen"])  # even the right password, while locked
    assert (locked.value.code, locked.value.status) == ("web.locked", 429)
    assert signin.check("r.patel", PASSWORDS["r.patel"]) is not None  # others are unaffected
    now[0] += 15 * 60 + 1
    assert signin.check("a.chen", PASSWORDS["a.chen"]) is not None


# --- which screens each role sees ---------------------------------------------------------


def test_each_role_opens_only_its_own_pages() -> None:
    def pages(app: str, role: str) -> set[str]:
        return {p.path for _, group in navigation(app, demo_mode=True, roles={role})
                for p in group}  # fmt: skip

    assert pages(CONSOLE, SUPERVISOR) == {"/", "/agent", "/ledger"}
    assert pages(CONSOLE, OFFICER) == {"/", "/policy", "/learn", "/improve", "/audit", "/ledger"}
    assert pages(CONSOLE, EXAMINER) == {"/", "/audit", "/ledger"}
    assert pages(CONSOLE, ADMIN) == {"/", "/ledger", "/demo"}
    assert pages(APPROVALS, SUPERVISOR) == pages(APPROVALS, OFFICER) == {"/", "/decided"}
    assert pages(APPROVALS, EXAMINER) == pages(APPROVALS, ADMIN) == set()
    assert home(APPROVALS, {EXAMINER}, demo_mode=True) is None
    assert home(CONSOLE, {OFFICER}, demo_mode=True) == "/"
    # deeper paths follow their page; the Approvals app's review pages need their own role
    assert allowed(CONSOLE, "/policy/drafts/01X", {OFFICER}, demo_mode=True)
    assert not allowed(CONSOLE, "/policy/drafts/01X", {SUPERVISOR}, demo_mode=True)
    assert allowed(APPROVALS, "/escalations/e1/verdict", {SUPERVISOR}, demo_mode=True)
    assert not allowed(APPROVALS, "/escalations/e1/verdict", {OFFICER}, demo_mode=True)
    assert allowed(APPROVALS, "/learn/proposals/01X", {OFFICER}, demo_mode=True)
    assert not allowed(APPROVALS, "/learn/proposals/01X", {SUPERVISOR}, demo_mode=True)
    # fail closed: an unlisted path, a demo page outside demo mode, no roles
    assert not allowed(CONSOLE, "/secret", {OFFICER, SUPERVISOR, ADMIN}, demo_mode=True)
    assert not allowed(CONSOLE, "/demo", {ADMIN}, demo_mode=False)
    assert not allowed(CONSOLE, "/", set(), demo_mode=True)
    assert not allowed(CONSOLE, "/ledgerx", {EXAMINER}, demo_mode=True)


# --- in the apps ---------------------------------------------------------------------------


async def test_every_page_asks_for_sign_in_first(tmp_path: Path) -> None:
    async with stack(tmp_path, signed_in=False) as s:
        page = await s.web.get("/ledger")
        assert page.status_code == 303
        assert page.headers["location"] == "/sign-in?next=/ledger"
        fragment = await s.web.get("/ledger", headers={"hx-request": "true"})
        assert fragment.headers["hx-redirect"] == "/sign-in"  # htmx reloads the whole page
        form = await s.web.get("/sign-in")
        assert form.status_code == 200
        assert 'type="password"' in form.text
        assert "Switch to" not in form.text
        assert "test-only" not in form.text
        refused = await s.web.post("/sign-in", data={"name": "j.ortiz", "password": "x"})
        assert refused.status_code == 403  # the sign-in form needs its CSRF token too
        assert (await s.web.get("/static/css/app.css")).status_code == 200


async def test_a_person_signs_in_and_out(tmp_path: Path) -> None:
    async with stack(tmp_path, signed_in=False) as s:
        before = s.web.cookies.get("pl_session")
        wrong = await sign_in(s.web, "a.chen", "not the password", next_="/learn")
        assert wrong.status_code == 401
        assert "Wrong name or password" in wrong.text
        assert "not the password" not in wrong.text
        done = await sign_in(s.web, "a.chen", next_="/learn")
        assert done.status_code == 303
        assert done.headers["location"] == "/learn"
        assert s.web.cookies.get("pl_session") not in (None, before)  # a new session
        page = await s.web.get("/learn")
        assert "Signed in as <strong>a.chen</strong>" in page.text
        assert 'href="/agent"' not in page.text  # a supervisor's page is not in the menu
        token = re.search(r'"X-CSRF-Token": "([0-9a-f]{64})"', page.text)
        assert token is not None
        out = await s.web.post("/sign-out", data={"csrf": token.group(1)})
        assert out.status_code == 303
        assert out.headers["location"] == "/sign-in"
        assert (await s.web.get("/learn")).status_code == 303  # signed out


async def test_sign_in_goes_only_to_a_page_of_this_app_the_person_may_open(
    tmp_path: Path,
) -> None:
    async with stack(tmp_path, signed_in=False) as s:
        for target in ("https://evil.example/", "//evil.example/", "/\\evil", "/demo"):
            response = await sign_in(s.web, "j.ortiz", next_=target)
            assert response.headers["location"] == "/", target  # not elsewhere, not refused
        examiner = await sign_in(s.inbox, "examiner")
        assert examiner.status_code == 403
        assert "examiner has nothing to sign in the Approvals app" in examiner.text


async def test_a_role_cannot_open_another_roles_page(tmp_path: Path) -> None:
    async with stack(tmp_path, signed_in=False) as s:
        await sign_in(s.web, "examiner")
        page = await s.web.get("/agent")
        assert page.status_code == 403
        assert "examiner cannot open this page" in page.text
        token = re.search(r'"X-CSRF-Token": "([0-9a-f]{64})"', page.text)
        assert token is not None
        run = await s.web.post("/agent/runs", headers={"x-csrf-token": token.group(1)},
                               data={"task": "Pay Acme Paper $1.00"})  # fmt: skip
        assert run.status_code == 403
        assert (await s.web.get("/ledger")).status_code == 200
        await sign_in(s.inbox, "a.chen")
        inbox = await s.inbox.get("/")
        assert "Payments held" not in inbox.text  # a supervisor's section
        assert "Rule changes" in inbox.text


async def test_a_web_only_person_never_reaches_gate(tmp_path: Path) -> None:
    async with stack(tmp_path) as s:
        examiner = s.console.sessions.personas["examiner"]
        with pytest.raises(WebError) as refused:
            await s.console.backend.escalations(examiner)
        assert refused.value.code == "web.no_gate_token"


async def test_the_lockout_holds_in_the_app(tmp_path: Path) -> None:
    async with stack(tmp_path, signed_in=False) as s:
        for _ in range(5):
            assert (await sign_in(s.web, "r.patel", "wrong")).status_code == 401
        locked = await sign_in(s.web, "r.patel")
        assert locked.status_code == 429
        assert "Too many attempts" in locked.text


def test_the_test_passwords_are_marked_test_only() -> None:
    assert all(p.startswith("test-only") for p in PASSWORDS.values())
