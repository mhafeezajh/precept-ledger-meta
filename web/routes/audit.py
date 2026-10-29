"""Audit: an examiner's question, answered with claims that are each proven or rejected (beat 3).

The console runs ``assist.auditor.Auditor`` itself, as ``make demo-audit`` does: the export is
verified first, a model only chooses filters and writes claims, ``claims.checker`` decides
what holds, and the audit is recorded as ``assist.qa`` when the assist key is available.
"""

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from assist.auditor import MAX_QUESTION, AuditResult
from web.app import console, done, render
from web.backend import WebError

router = APIRouter(prefix="/audit")
EXAMPLE = (
    "Show every payment over $10,000 approved outside business hours this week,"
    " and who approved it."
)
Ask = Callable[[str, bool], Awaitable[AuditResult]]


def _ask(request: Request) -> Ask:
    found = console(request).extras.get("audit")
    if found is None:
        raise WebError("no model is configured for audits (MODEL_PROFILE)", code="web.no_model",
                       status=503)  # fmt: skip
    ask: Ask = found
    return ask


@router.get("", response_class=HTMLResponse)
async def audit_page(request: Request) -> HTMLResponse:
    state = await console(request).backend.ledger_state()
    earlier = [r for r in state.records if r.get("type") == "assist.qa"][:10]
    return render(request, "pages/audit.html", example=EXAMPLE, earlier=earlier,
                  available="audit" in console(request).extras, max_chars=MAX_QUESTION)  # fmt: skip


@router.post("/ask")
async def ask(
    request: Request, question: str = Form(...), inject_fault: str = Form("")
) -> Response:
    question = " ".join(question.split())
    if not question:
        raise WebError("write a question first", code="web.bad_request", status=400)
    fault = bool(inject_fault) and console(request).demo_mode
    result = await _ask(request)(question[:MAX_QUESTION], fault)
    return done(request, "partials/audit_result.html", "/audit", result=result)
