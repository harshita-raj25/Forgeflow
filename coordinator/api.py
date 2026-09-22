"""Minimal local HTTP UI/API. Binds to loopback by default. Same-origin only; no remote agent
execution endpoint is exposed. Server-rendered HTML with light polling, per PROJECT-SPEC §10.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import PROMPTS_DIR
from .export import export_bundle
from .metrics import aggregate_metrics, run_metrics
from .scenarios import list_scenarios

TEMPLATES = Jinja2Templates(directory=str(PROMPTS_DIR.parent / "ui" / "templates"))


def build_app(coord) -> FastAPI:
    app = FastAPI(title="ForgeFlow")
    csrf_tokens: set[str] = set()

    def new_csrf() -> str:
        t = secrets.token_urlsafe(24)
        csrf_tokens.add(t)
        return t

    def check_csrf(request: Request, token: str) -> None:
        origin = request.headers.get("origin")
        if origin and origin not in (f"http://{request.url.netloc}", f"https://{request.url.netloc}"):
            raise HTTPException(403, "cross-origin request rejected")
        if token not in csrf_tokens:
            raise HTTPException(403, "invalid or reused CSRF token")
        csrf_tokens.discard(token)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        runs = coord.store.list_runs()
        return TEMPLATES.TemplateResponse(request, "index.html", {"runs": runs, "scenarios": list_scenarios(), "csrf": new_csrf()})

    @app.post("/runs")
    def create_run(request: Request, scenario: str = Form(...), csrf: str = Form(...), inject_fault: bool = Form(False)):
        check_csrf(request, csrf)
        run_id = coord.create_run(scenario, inject_fault=inject_fault)
        coord.resume(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str):
        try:
            v = coord.snapshot_view(run_id)
        except KeyError:
            raise HTTPException(404, "unknown run") from None
        return TEMPLATES.TemplateResponse(request, "run.html", {"v": v, "csrf": new_csrf()})

    @app.get("/runs/{run_id}/events.json")
    def run_events(run_id: str):
        return JSONResponse(coord.store.list_events(run_id))

    @app.post("/runs/{run_id}/resume")
    def resume(request: Request, run_id: str, csrf: str = Form(...)):
        check_csrf(request, csrf)
        coord.resume(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/stop")
    def stop(request: Request, run_id: str, csrf: str = Form(...), reason: str = Form("human requested stop")):
        check_csrf(request, csrf)
        coord.stop(run_id, reason)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/clarify")
    def clarify(request: Request, run_id: str, clarification_id: str = Form(...), answer: list[str] = Form(...), csrf: str = Form(...)):
        check_csrf(request, csrf)
        cl = next(c for c in coord.store.list_clarifications(run_id) if c["id"] == clarification_id)
        answers = [{"id": q["id"], "answer": a} for q, a in zip(cl["questions"], answer, strict=False)]
        coord.answer_clarification(run_id, clarification_id, answers)
        coord.resume(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/revise")
    def revise(request: Request, run_id: str, text: str = Form(...), reason: str = Form(...), csrf: str = Form(...)):
        check_csrf(request, csrf)
        coord.revise_requirement(run_id, text, reason)
        coord.resume(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/approvals/{approval_id}")
    def decide(
        request: Request, run_id: str, approval_id: str, decision: str = Form(...), rationale: str = Form(""), csrf: str = Form(...)
    ):
        check_csrf(request, csrf)
        coord.decide_approval(run_id, approval_id, decision == "approve", rationale)
        coord.resume(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/export")
    def export(request: Request, run_id: str, csrf: str = Form(...)):
        check_csrf(request, csrf)
        export_bundle(coord, run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/metrics.json")
    def metrics(mode: str | None = None):
        per_run = []
        for r in coord.store.list_runs():
            per_run.append(run_metrics(r, coord.store.list_events(r["id"]), coord.store.list_attempts(r["id"])))
        return JSONResponse(aggregate_metrics(per_run, mode=mode))

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    return app
