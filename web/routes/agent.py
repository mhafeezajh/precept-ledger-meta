"""Agent: give payments-ops a task and watch each step go through Gate (beat 2)."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from agent.graph import MAX_TASK_CHARS
from web.agent_runs import AgentRuns
from web.app import console, is_htmx, render
from web.backend import WebError

router = APIRouter(prefix="/agent")
EXAMPLES = (
    "Pay Northwind Supply $48,000.00 for invoice INV-4471.",
    "Pay Keystone IT $120.00 for invoice INV-2.",
    "Pay Acme Paper $300,000.00 for invoice INV-9.",
)


def runs(request: Request) -> AgentRuns:
    found = console(request).extras.get("agent_runs")
    if not isinstance(found, AgentRuns):
        raise WebError("the agent is not configured for this console", code="web.no_agent",
                       status=503)  # fmt: skip
    return found


@router.get("", response_class=HTMLResponse)
async def agent_page(request: Request) -> HTMLResponse:
    available = isinstance(console(request).extras.get("agent_runs"), AgentRuns)
    recent = runs(request).recent() if available else []
    return render(request, "pages/agent.html", examples=EXAMPLES, recent=recent,
                  available=available, max_chars=MAX_TASK_CHARS)  # fmt: skip


@router.post("/runs")
async def start(request: Request, task: str = Form(...)) -> Response:
    task = " ".join(task.split())
    if not task or len(task) > MAX_TASK_CHARS:
        raise WebError(f"a task is 1 to {MAX_TASK_CHARS} characters", code="web.bad_request",
                       status=400)  # fmt: skip
    run = runs(request).start(task)
    if is_htmx(request):
        return render(request, "partials/run.html", run=run)
    return RedirectResponse(f"/agent/runs/{run.id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def show(request: Request, run_id: str) -> HTMLResponse:
    run = runs(request).get(run_id)
    if run is None:
        raise WebError("no such run (runs are kept while the console runs)",
                       code="web.not_found", status=404)  # fmt: skip
    template = "partials/run.html" if is_htmx(request) else "pages/agent_run.html"
    return render(request, template, run=run)
