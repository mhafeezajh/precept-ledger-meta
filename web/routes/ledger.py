"""Ledger: every record, verified as an auditor would, and one record with its own proof."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web import views
from web.app import console, render
from web.views import TYPE_LABELS

router = APIRouter(prefix="/ledger")
PAGE_SIZE = 50


@router.get("", response_class=HTMLResponse)
async def ledger(request: Request, kind: str = "", page: int = 1) -> HTMLResponse:
    state = await console(request).backend.ledger_state()
    records = [r for r in state.records if not kind or r.get("type") == kind]
    page = max(1, page)
    shown = records[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    broken = views.seal(state.report).seq
    return render(
        request, "pages/ledger.html", state=state, proof=views.seal(state.report),
        records=shown, kind=kind, kinds=TYPE_LABELS, page_number=page,
        more=len(records) > page * PAGE_SIZE, total=len(records), broken=broken,
        findings=state.report.findings,
    )  # fmt: skip


@router.get("/records/{record_id}", response_class=HTMLResponse)
async def record(request: Request, record_id: str) -> HTMLResponse:
    found = await console(request).backend.proven_record(record_id)
    pretty = json.dumps(found.record.get("body"), indent=2, sort_keys=True)
    return render(request, "pages/record.html", found=found, record=found.record, pretty=pretty)
