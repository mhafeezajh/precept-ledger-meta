"""Sign in and sign out (D96): the only routes without the guard.

A wrong name and a wrong password get the same answer, and an unknown name costs the same
password work (``web.logins.SignIn``). Signing in or out always issues a new session cookie.
After sign-in the person goes to the page they asked for, if it is in this app and their role
may open it, and otherwise to their first page; never to another site.
"""

import logging

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from common.logging import log_event
from web.app import console, render, set_session
from web.backend import WebError
from web.nav import TITLES, allowed, home

router = APIRouter()
logger = logging.getLogger("pl.web")


def _local(path: str) -> bool:
    """A path on this site: not ``//host``, not ``/\\host``, not a full URL."""
    return path.startswith("/") and not path.startswith("//") and "\\" not in path


@router.get("/sign-in", response_class=HTMLResponse)
async def sign_in_page(request: Request, next_: str = Query("", alias="next")) -> HTMLResponse:
    return render(request, "pages/sign_in.html", next=next_, error=None, person="")


@router.post("/sign-in")
async def sign_in(
    request: Request,
    name: str = Form("", max_length=64),
    password: str = Form("", max_length=256),
    next_: str = Form("", alias="next", max_length=512),
) -> Response:
    c = console(request)

    def again(message: str, status: int) -> HTMLResponse:
        return render(request, "pages/sign_in.html", status=status, next=next_, error=message,
                      person=name)  # fmt: skip

    if c.signin is None:  # fail closed: no logins loaded, nobody signs in
        return again("Sign-in is not set up on this server.", 503)
    try:
        login = c.signin.check(name, password)
    except WebError as exc:  # locked out
        log_event(logger, logging.WARNING, "sign-in locked", person=name)
        return again(exc.message, exc.status)
    persona = c.sessions.personas.get(name) if login else None
    if persona is None:
        log_event(logger, logging.INFO, "sign-in refused", app=c.app)  # the name may be junk
        return again("Wrong name or password.", 401)
    start = home(c.app, persona.roles, demo_mode=c.demo_mode)
    if start is None:
        return again(f"{name} has nothing to sign in the {TITLES[c.app]} app.", 403)
    wanted = _local(next_) and allowed(c.app, next_, persona.roles, demo_mode=c.demo_mode)
    log_event(logger, logging.INFO, "signed in", person=name, app=c.app)
    return set_session(request, RedirectResponse(next_ if wanted else start, 303), name)


@router.post("/sign-out")
async def sign_out(request: Request) -> Response:
    return set_session(request, RedirectResponse("/sign-in", status_code=303), None)
