from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import require_tenant_role
from app.core.templating import templates
from app.db.models import (
    Mailbox,
    RoutingEvaluationSet,
    RoutingEvaluationCase,
    RoutingEvaluationRun,
    RoutingEvaluationResult,
)
from app.master.service import TenantUser
from app.routing.evaluations import (
    PLAYGROUND_ROLES,
    compare_runs,
    create_evaluation_case,
    create_evaluation_set,
    evaluation_context,
    get_evaluation_set,
    playground_analysis,
    run_evaluation,
    seed_demo_evaluation_set,
    simulate_threshold,
    update_evaluation_case,
    update_evaluation_set,
)
from app.routing.policy import build_kibak_readiness, load_routing_policy
from app.routing.organization_config import (
    OrganizationConfigError,
    apply_organization_config,
    export_organization_config,
    preview_organization_config,
)
from app.tenancy.database import get_tenant_db


router = APIRouter(prefix="/routing", tags=["routing-lab"])
EVALUATION_PAGE_SIZE = 25


def _page_params(request: Request) -> tuple[int, int]:
    try:
        page = max(int(request.query_params.get("page", "1")), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(max(int(request.query_params.get("page_size", str(EVALUATION_PAGE_SIZE))), 1), 100)
    except (TypeError, ValueError):
        page_size = EVALUATION_PAGE_SIZE
    return page, page_size


def _pagination(page: int, page_size: int, total: int) -> dict[str, int | bool]:
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max((total + page_size - 1) // page_size, 1),
        "has_previous": page > 1,
        "has_next": page * page_size < total,
    }


def _run_simulation(db: Session, company_id: int, run_id: int, threshold: float) -> dict[str, float | int]:
    threshold = min(max(float(threshold), 0.0), 1.0)
    candidate = (
        RoutingEvaluationResult.company_id == company_id,
        RoutingEvaluationResult.run_id == run_id,
        RoutingEvaluationResult.predicted_department_id.is_not(None),
        RoutingEvaluationResult.requires_review.is_(False),
        RoutingEvaluationResult.confidence >= threshold,
    )
    false_route = (*candidate, RoutingEvaluationResult.expected_department_id != RoutingEvaluationResult.predicted_department_id)
    critical_false = (*false_route, RoutingEvaluationResult.criticality == "critical")
    candidate_count = db.scalar(select(func.count(RoutingEvaluationResult.id)).where(*candidate)) or 0
    false_count = db.scalar(select(func.count(RoutingEvaluationResult.id)).where(*false_route)) or 0
    critical_false_count = db.scalar(select(func.count(RoutingEvaluationResult.id)).where(*critical_false)) or 0
    total = db.scalar(
        select(func.count(RoutingEvaluationResult.id)).where(
            RoutingEvaluationResult.company_id == company_id,
            RoutingEvaluationResult.run_id == run_id,
        )
    ) or 0
    return {
        "threshold": threshold,
        "candidate_count": candidate_count,
        "automation_rate": round(candidate_count / total, 4) if total else 0.0,
        "false_auto_route_count": false_count,
        "critical_false_auto_routes": critical_false_count,
    }


def _redirect(path: str, *, error: str | None = None, message: str | None = None) -> RedirectResponse:
    query = {key: value for key, value in (("error", error), ("message", message)) if value}
    suffix = f"?{urlencode(query)}" if query else ""
    return RedirectResponse(f"{path}{suffix}", status_code=303)


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: str(value) for key, value in form.multi_items() if not hasattr(value, "filename")}


async def _json_payload(request: Request):
    if "application/json" in (request.headers.get("content-type") or ""):
        return await request.json()
    data = await _form(request)
    try:
        return json.loads(data.get("payload", "{}"))
    except json.JSONDecodeError as exc:
        raise OrganizationConfigError("El contenido no es JSON válido.") from exc


def _context_view(db: Session, user: TenantUser) -> dict:
    context = evaluation_context(db, user.company_id)
    return {"raw": json.dumps(context, ensure_ascii=False, indent=2, default=str), "departments": context.get("departments", [])}


@router.get("/organization/export")
def organization_export(
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*PLAYGROUND_ROLES)),
):
    return JSONResponse(export_organization_config(db, user.company_id), headers={"Content-Disposition": "attachment; filename=kibak-organization-v1.json"})


@router.post("/organization/import/validate")
async def organization_import_validate(
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    try:
        payload = await _json_payload(request)
        return JSONResponse({"ok": True, "preview": preview_organization_config(db, user.company_id, payload)})
    except OrganizationConfigError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)


@router.post("/organization/import/apply")
async def organization_import_apply(
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    try:
        payload = await _json_payload(request)
        result = apply_organization_config(db, user.company_id, payload)
        db.commit()
        return JSONResponse({"ok": True, "result": result})
    except OrganizationConfigError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)


@router.get("/playground")
def playground_page(
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*PLAYGROUND_ROLES)),
):
    return templates.TemplateResponse(
        "routing/playground.html",
        {
            "request": request,
            "user": user,
            "title": "Routing Playground",
            "mailboxes": db.scalars(select(Mailbox).where(Mailbox.company_id == user.company_id).order_by(Mailbox.name)).all(),
            "context_view": _context_view(db, user),
            "form": {},
            "result": None,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/playground/analyze")
async def playground_analyze(
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*PLAYGROUND_ROLES)),
):
    data = await _form(request)
    form = {key: value for key, value in data.items() if key not in {"mailbox_id"}}
    try:
        result = playground_analysis(
            db,
            user.company_id,
            subject=data.get("subject", ""),
            body=data.get("body", ""),
            sender=data.get("sender") or None,
            recipients=data.get("recipients") or None,
            cc_recipients=data.get("cc_recipients") or None,
            attachment_text=data.get("attachment_text") or None,
            mailbox_id=int(data["mailbox_id"]) if data.get("mailbox_id") else None,
            user_id=user.id,
        )
        db.commit()
        error = None
    except Exception as exc:  # Provider/configuration errors remain reviewable and human-readable.
        error = str(exc)[:500]
        result = None
        db.commit()
    return templates.TemplateResponse(
        "routing/playground.html",
        {
            "request": request,
            "user": user,
            "title": "Routing Playground",
            "mailboxes": db.scalars(select(Mailbox).where(Mailbox.company_id == user.company_id).order_by(Mailbox.name)).all(),
            "context_view": _context_view(db, user),
            "form": form,
            "result": result,
            "error": error,
        },
    )


@router.get("/evaluations")
def evaluations_page(
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*PLAYGROUND_ROLES)),
):
    page, page_size = _page_params(request)
    total_sets = db.scalar(select(func.count(RoutingEvaluationSet.id)).where(RoutingEvaluationSet.company_id == user.company_id)) or 0
    sets = list(
        db.scalars(
            select(RoutingEvaluationSet)
            .where(RoutingEvaluationSet.company_id == user.company_id)
            .order_by(RoutingEvaluationSet.active.desc(), RoutingEvaluationSet.name)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return templates.TemplateResponse(
        "routing/evaluations.html",
        {"request": request, "user": user, "title": "Evaluation Lab", "sets": sets, "pagination": _pagination(page, page_size, total_sets), "error": request.query_params.get("error"), "message": request.query_params.get("message")},
    )


@router.post("/evaluations/sets")
async def evaluations_create_set(
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    data = await _form(request)
    try:
        create_evaluation_set(db, user.company_id, name=data.get("name", ""), description=data.get("description") or None)
    except Exception as exc:
        db.rollback()
        return _redirect("/routing/evaluations", error=str(exc)[:300])
    return _redirect("/routing/evaluations", message="Conjunto creado.")


@router.post("/evaluations/demo-seed")
def evaluations_demo_seed(
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    try:
        evaluation_set = seed_demo_evaluation_set(db, user.company_id)
    except Exception as exc:
        db.rollback()
        return _redirect("/routing/evaluations", error=str(exc)[:300])
    return _redirect(f"/routing/evaluations/{evaluation_set.id}", message="Dataset demo listo.")


@router.get("/evaluations/{set_id:int}")
def evaluation_set_page(
    set_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*PLAYGROUND_ROLES)),
):
    page, page_size = _page_params(request)
    evaluation_set = get_evaluation_set(db, user.company_id, set_id)
    if evaluation_set is None:
        return _redirect("/routing/evaluations", error="Conjunto no encontrado.")
    total_runs = db.scalar(select(func.count(RoutingEvaluationRun.id)).where(RoutingEvaluationRun.company_id == user.company_id, RoutingEvaluationRun.evaluation_set_id == set_id)) or 0
    total_cases = db.scalar(select(func.count(RoutingEvaluationCase.id)).where(RoutingEvaluationCase.company_id == user.company_id, RoutingEvaluationCase.evaluation_set_id == set_id)) or 0
    runs = db.scalars(select(RoutingEvaluationRun).where(RoutingEvaluationRun.company_id == user.company_id, RoutingEvaluationRun.evaluation_set_id == set_id).order_by(RoutingEvaluationRun.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    cases = db.scalars(select(RoutingEvaluationCase).where(RoutingEvaluationCase.company_id == user.company_id, RoutingEvaluationCase.evaluation_set_id == set_id).order_by(RoutingEvaluationCase.id).offset((page - 1) * page_size).limit(page_size)).all()
    return templates.TemplateResponse(
        "routing/evaluation_set.html",
        {"request": request, "user": user, "title": evaluation_set.name, "evaluation_set": evaluation_set, "cases": cases, "runs": runs, "cases_pagination": _pagination(page, page_size, total_cases), "runs_pagination": _pagination(page, page_size, total_runs), "error": request.query_params.get("error"), "message": request.query_params.get("message")},
    )


@router.post("/evaluations/{set_id}/update")
async def evaluation_set_update(
    set_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    data = await _form(request)
    try:
        update_evaluation_set(
            db,
            user.company_id,
            set_id,
            name=data.get("name"),
            description=data.get("description"),
        )
    except Exception as exc:
        db.rollback()
        return _redirect(f"/routing/evaluations/{set_id}", error=str(exc)[:300])
    return _redirect(f"/routing/evaluations/{set_id}", message="Conjunto actualizado.")


@router.post("/evaluations/{set_id}/deactivate")
def evaluation_set_deactivate(
    set_id: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    try:
        update_evaluation_set(db, user.company_id, set_id, active=False)
    except Exception as exc:
        db.rollback()
        return _redirect(f"/routing/evaluations/{set_id}", error=str(exc)[:300])
    return _redirect("/routing/evaluations", message="Conjunto pausado.")


@router.post("/evaluations/{set_id}/cases")
async def evaluation_case_create(
    set_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    data = await _form(request)
    try:
        create_evaluation_case(
            db,
            user.company_id,
            set_id,
            title=data.get("title", ""),
            subject=data.get("subject", ""),
            body=data.get("body", ""),
            sender=data.get("sender") or None,
            attachment_text=data.get("attachment_text") or None,
            expected_department_id=int(data["expected_department_id"]) if data.get("expected_department_id") else None,
            expected_category=data.get("expected_category") or None,
            expected_requires_review=data.get("expected_requires_review") == "on",
            criticality=data.get("criticality", "normal"),
        )
    except Exception as exc:
        db.rollback()
        return _redirect(f"/routing/evaluations/{set_id}", error=str(exc)[:300])
    return _redirect(f"/routing/evaluations/{set_id}", message="Caso añadido.")


@router.post("/evaluations/{set_id}/cases/{case_id}/update")
async def evaluation_case_update(
    set_id: int,
    case_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    data = await _form(request)
    try:
        update_evaluation_case(
            db,
            user.company_id,
            set_id,
            case_id,
            title=data.get("title"),
            subject=data.get("subject"),
            body=data.get("body"),
            expected_department_id=int(data["expected_department_id"]) if data.get("expected_department_id") else None,
            expected_category=data.get("expected_category"),
            expected_requires_review=data.get("expected_requires_review") == "on",
            criticality=data.get("criticality"),
        )
    except Exception as exc:
        db.rollback()
        return _redirect(f"/routing/evaluations/{set_id}", error=str(exc)[:300])
    return _redirect(f"/routing/evaluations/{set_id}", message="Caso actualizado.")


@router.post("/evaluations/{set_id}/cases/{case_id}/deactivate")
def evaluation_case_deactivate(
    set_id: int,
    case_id: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    try:
        update_evaluation_case(db, user.company_id, set_id, case_id, active=False)
    except Exception as exc:
        db.rollback()
        return _redirect(f"/routing/evaluations/{set_id}", error=str(exc)[:300])
    return _redirect(f"/routing/evaluations/{set_id}", message="Caso pausado.")


@router.post("/evaluations/{set_id}/run")
def evaluation_run(
    set_id: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin")),
):
    try:
        run = run_evaluation(db, user.company_id, set_id, user_id=user.id, commit=True)
    except Exception as exc:
        db.rollback()
        return _redirect(f"/routing/evaluations/{set_id}", error=str(exc)[:400])
    return _redirect(f"/routing/evaluations/runs/{run.id}", message="Evaluación completada.")


@router.get("/evaluations/runs/{run_id}")
def evaluation_run_page(
    run_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*PLAYGROUND_ROLES)),
):
    page, page_size = _page_params(request)
    run = db.scalar(select(RoutingEvaluationRun).where(RoutingEvaluationRun.company_id == user.company_id, RoutingEvaluationRun.id == run_id))
    if run is None:
        return _redirect("/routing/evaluations", error="Run no encontrado.")
    try:
        threshold = float(request.query_params.get("threshold", str(load_routing_policy(db, user.company_id).auto_threshold)))
    except (TypeError, ValueError):
        threshold = 0.90
    metrics = json.loads(run.metrics_json or "{}")
    total_results = db.scalar(select(func.count(RoutingEvaluationResult.id)).where(RoutingEvaluationResult.company_id == user.company_id, RoutingEvaluationResult.run_id == run_id)) or 0
    results = db.scalars(select(RoutingEvaluationResult).where(RoutingEvaluationResult.company_id == user.company_id, RoutingEvaluationResult.run_id == run_id).order_by(RoutingEvaluationResult.id).offset((page - 1) * page_size).limit(page_size)).all()
    simulation = _run_simulation(db, user.company_id, run_id, threshold)
    return templates.TemplateResponse(
        "routing/evaluation_run.html",
        {"request": request, "user": user, "title": f"Run #{run.id}", "run": run, "metrics": metrics, "simulation": simulation, "threshold": threshold, "context": json.loads(run.context_snapshot_json or "{}"), "results": results, "pagination": _pagination(page, page_size, total_results)},
    )


@router.get("/evaluations/results/{result_id}")
def evaluation_result_page(
    result_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*PLAYGROUND_ROLES)),
):
    result = db.scalar(select(RoutingEvaluationResult).where(RoutingEvaluationResult.company_id == user.company_id, RoutingEvaluationResult.id == result_id))
    if result is None:
        return _redirect("/routing/evaluations", error="Resultado no encontrado.")
    run = db.scalar(select(RoutingEvaluationRun).where(RoutingEvaluationRun.company_id == user.company_id, RoutingEvaluationRun.id == result.run_id))
    return templates.TemplateResponse("routing/evaluation_result.html", {"request": request, "user": user, "title": "Detalle de resultado", "result": result, "run": run, "context": json.loads(run.context_snapshot_json or "{}") if run else {}})


@router.get("/evaluations/compare")
def evaluation_compare_page(
    request: Request,
    run_a: int,
    run_b: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*PLAYGROUND_ROLES)),
):
    first = db.scalar(select(RoutingEvaluationRun).options(selectinload(RoutingEvaluationRun.results)).where(RoutingEvaluationRun.company_id == user.company_id, RoutingEvaluationRun.id == run_a))
    second = db.scalar(select(RoutingEvaluationRun).options(selectinload(RoutingEvaluationRun.results)).where(RoutingEvaluationRun.company_id == user.company_id, RoutingEvaluationRun.id == run_b))
    if first is None or second is None:
        return _redirect("/routing/evaluations", error="No se han encontrado ambos runs en este tenant.")
    return templates.TemplateResponse("routing/evaluation_compare.html", {"request": request, "user": user, "title": "Comparación A/B", "first": first, "second": second, "comparison": compare_runs(first, second)})


@router.get("/quality")
def quality_page(
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*PLAYGROUND_ROLES)),
):
    runs = db.scalars(select(RoutingEvaluationRun).options(selectinload(RoutingEvaluationRun.results)).where(RoutingEvaluationRun.company_id == user.company_id).order_by(RoutingEvaluationRun.id.desc()).limit(12)).all()
    latest = runs[0] if runs else None
    metrics = json.loads(latest.metrics_json or "{}") if latest else {}
    policy = load_routing_policy(db, user.company_id)
    try:
        threshold = min(max(float(request.query_params.get("threshold", policy.auto_threshold)), 0), 1)
    except (TypeError, ValueError):
        threshold = policy.auto_threshold
    impact = simulate_threshold(latest.results, threshold) if latest else None
    return templates.TemplateResponse("routing/quality.html", {"request": request, "user": user, "title": "Calidad de RoutingAgent", "runs": runs, "latest": latest, "metrics": metrics, "threshold": threshold, "impact": impact, "readiness": build_kibak_readiness(db, user.company_id)})
