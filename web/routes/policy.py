"""Policy: draft rules from the policy PDF, read each beside its clause, fix, approve (beat 1).

Drafting, checks and approval all happen in ``pl-gate`` (``/policy/v1``) under the chosen
persona's token; Gate refuses anyone without the compliance-officer role.
"""

from pathlib import Path

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from common.errors import PolicyError
from policy.pdf import MAX_PDF_BYTES
from policy.rules import MAX_YAML_BYTES, PolicyRule, parse_rules
from web import views
from web.app import console, done, is_htmx, render
from web.backend import WebError

router = APIRouter(prefix="/policy")
DEMO_PDF = Path(__file__).resolve().parents[2] / "deploy" / "demo" / "payments_policy.pdf"
OFFICER_ROLE = "compliance-officer"


def _rules(yaml_text: str) -> tuple[list[PolicyRule], str | None]:
    try:
        return list(parse_rules(yaml_text)), None
    except PolicyError as exc:
        return [], str(exc.args[0])


def _go(request: Request, path: str) -> Response:
    """After an action: htmx follows with HX-Location, a plain form with a 303."""
    if is_htmx(request):
        return Response(status_code=204, headers={"hx-location": path})
    return RedirectResponse(path, status_code=303)


@router.get("", response_class=HTMLResponse)
async def policy_page(request: Request) -> HTMLResponse:
    persona = request.state.session.persona
    state = await console(request).backend.ledger_state()
    approval, rules = views.in_force(state.records)
    history = [r for r in state.records if r.get("type") in ("policy.draft", "policy.approve")]
    return render(request, "pages/policy.html", approval=approval, rules=rules,
                  history=history[:12], officer=OFFICER_ROLE in persona.roles,
                  demo_pdf=DEMO_PDF.name, conditions=views.conditions)  # fmt: skip


@router.post("/drafts")
async def submit(
    request: Request,
    pdf: UploadFile | None = None,
    use_demo: str = Form(""),
    inject_fault: str = Form(""),
) -> Response:
    if use_demo:
        data, name = DEMO_PDF.read_bytes(), DEMO_PDF.name
    elif pdf is not None and pdf.filename:
        data, name = await pdf.read(MAX_PDF_BYTES + 1), pdf.filename
    else:
        raise WebError("choose a PDF, or draft from the demo policy", code="web.bad_request",
                       status=400)  # fmt: skip
    fault = bool(inject_fault) and console(request).demo_mode
    persona = request.state.session.persona
    draft = await console(request).backend.submit_policy(persona, data, name, inject_fault=fault)
    return _go(request, f"/policy/drafts/{draft['record']['record_id']}")


@router.get("/drafts/{draft_id}", response_class=HTMLResponse)
async def draft_page(request: Request, draft_id: str) -> HTMLResponse:
    persona = request.state.session.persona
    found = await console(request).backend.policy_draft(persona, draft_id)
    body = found["record"]["body"]
    rules, problem = _rules(body["rules_yaml"])
    flagged = {f["rule_id"] for f in body["findings"]}
    return render(request, "pages/policy_draft.html", draft=found["record"], body=body,
                  rules=rules, problem=problem, flagged=flagged,
                  conditions=views.conditions)  # fmt: skip


@router.post("/drafts/{draft_id}/revisions")
async def revise(request: Request, draft_id: str, rules_yaml: str = Form(...)) -> Response:
    if len(rules_yaml.encode("utf-8")) > MAX_YAML_BYTES:
        raise WebError("the rules are too long", code="web.bad_request", status=413)
    persona = request.state.session.persona
    revised = await console(request).backend.revise_policy(persona, draft_id, rules_yaml)
    return _go(request, f"/policy/drafts/{revised['record']['record_id']}")


@router.post("/drafts/{draft_id}/approve")
async def approve(request: Request, draft_id: str) -> Response:
    persona = request.state.session.persona
    approval = await console(request).backend.approve_policy(persona, draft_id)
    return done(request, "partials/policy_approved.html", "/policy", approval=approval["record"])
