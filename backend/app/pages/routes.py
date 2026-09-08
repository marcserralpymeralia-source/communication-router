from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, case, exists, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import current_user
from app.core.config import get_settings
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.dashboard.kibak import kibak_dashboard_summary
from app.dashboard.service import orders_workbench_summary, workbench_summary
from app.db.models import (
    Communication,
    Customer,
    Department,
    Email,
    Order,
    OrderLine,
    RoutingAction,
    RoutingCorrection,
    RoutingDecision,
    ScoringSettings,
)
from app.dashboard.service import _customer_suggestion_maps, _load_order_line_metrics, email_workbench_item, load_order_view_data, order_workbench_item, suggest_customer_for_email
from app.orders.state import ERROR_ORDER_STATUSES, PENDING_ORDER_STATUSES, REVIEW_ORDER_STATUSES, TERMINAL_ORDER_STATUSES
from app.master.service import TenantUser
from app.settings.service import get_or_create_settings
from app.setup.service import get_setup_status, setup_operational_context
from app.tenancy.database import get_tenant_db

router = APIRouter(tags=["pages"])


def _is_postgresql_session(db: Session) -> bool:
    get_bind = getattr(db, "get_bind", None)
    if not callable(get_bind):
        return False
    try:
        return get_bind().dialect.name == "postgresql"
    except AttributeError:
        return False


def _is_kibak_runtime(db: Session) -> bool:
    return get_settings().app_slug.strip().lower() == "kibak" and _is_postgresql_session(db)


def _history_pagination_url(request: Request, page: int, page_size: int) -> str:
    query = dict(request.query_params)
    query["page"] = page
    query["page_size"] = page_size
    return f"/history?{urlencode(query)}"


def _kibak_history_context(
    db: Session,
    user: TenantUser,
    *,
    request: Request,
    search: str,
    page: int,
    page_size: int,
    start: datetime | None,
    end: datetime | None,
    department_id: int | None,
    kind: str,
    state: str,
    source: str,
) -> dict:
    filters = [Communication.company_id == user.company_id]
    if start:
        filters.append(Communication.received_at >= start)
    if end:
        filters.append(Communication.received_at <= end)
    if search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                Communication.subject.ilike(pattern),
                Communication.sender_email.ilike(pattern),
                Communication.sender_name.ilike(pattern),
            )
        )
    decision_scope = select(RoutingDecision.id).where(
        RoutingDecision.company_id == Communication.company_id,
        RoutingDecision.communication_id == Communication.id,
        RoutingDecision.status != "superseded",
    )
    if department_id:
        filters.append(
            exists(
                decision_scope.where(
                    or_(
                        RoutingDecision.department_id == department_id,
                        RoutingDecision.alternative_department_id == department_id,
                        RoutingDecision.final_department_id == department_id,
                    )
                )
            )
        )
    if kind and kind != "all":
        filters.append(exists(decision_scope.where(or_(RoutingDecision.category == kind, RoutingDecision.final_category == kind))))
    if source and source != "all":
        filters.append(exists(decision_scope.where(RoutingDecision.source == source)))
    if state == "review":
        filters.append(
            or_(
                Communication.routing_status == "pending_review",
                exists(decision_scope.where(or_(RoutingDecision.requires_review.is_(True), RoutingDecision.status == "pending_review"))),
            )
        )
    elif state == "automatic":
        filters.append(exists(decision_scope.where(RoutingDecision.status == "routed", RoutingDecision.requires_review.is_(False))))
    elif state == "reviewed":
        filters.append(
            or_(
                Communication.routing_status == "reviewed",
                exists(decision_scope.where(RoutingDecision.status.in_(("confirmed", "corrected", "reviewed")))),
            )
        )
    elif state == "error":
        filters.append(
            or_(
                Communication.processing_status == "error",
                Communication.routing_status.in_(("error", "routing_error")),
            )
        )
    page, page_size = normalize_page(page, page_size)
    total = db.scalar(select(func.count(Communication.id)).where(*filters)) or 0
    communications = db.scalars(
        select(Communication)
        .where(*filters)
        .order_by(Communication.received_at.desc().nullslast(), Communication.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    communication_ids = [item.id for item in communications]
    decisions = (
        db.scalars(
            select(RoutingDecision)
            .where(
                RoutingDecision.company_id == user.company_id,
                RoutingDecision.communication_id.in_(communication_ids or [-1]),
                RoutingDecision.status != "superseded",
            )
            .order_by(RoutingDecision.analysis_number.desc(), RoutingDecision.id.desc())
        ).all()
        if communication_ids
        else []
    )
    decision_by_communication = {}
    for decision in decisions:
        decision_by_communication.setdefault(decision.communication_id, decision)
    all_departments = db.scalars(
        select(Department)
        .where(Department.company_id == user.company_id, Department.active.is_(True))
        .order_by(Department.name)
    ).all()
    department_ids = {
        department_id
        for decision in decisions
        for department_id in (decision.department_id, decision.final_department_id)
        if department_id
    }
    departments = (
        db.scalars(select(Department).where(Department.company_id == user.company_id, Department.id.in_(department_ids))).all()
        if department_ids
        else []
    )
    department_names = {department.id: department.name for department in departments}
    items = []
    for communication in communications:
        decision = decision_by_communication.get(communication.id)
        department_id = decision.final_department_id if decision and decision.final_department_id else decision.department_id if decision else None
        items.append(
            {
                "id": communication.id,
                "subject": communication.subject or "Sin asunto",
                "sender": communication.sender_name or communication.sender_email or "Remitente no disponible",
                "received_at": communication.received_at,
                "routing_status": communication.routing_status,
                "routing_status_label": {
                    "routed": "Enrutada",
                    "pending_review": "Pendiente de revisión",
                    "reviewed": "Revisada",
                    "error": "Error",
                    "unclassified": "Sin clasificar",
                }.get(communication.routing_status, communication.routing_status or "Sin estado"),
                "department": department_names.get(department_id, "Sin departamento"),
                "confidence": decision.confidence if decision else None,
                "reason": decision.reason if decision else None,
                "category": (decision.final_category or decision.category) if decision else "Sin categoría",
                "source": decision.source if decision else "unknown",
            }
        )
    correction_count = db.scalar(
        select(func.count(RoutingCorrection.id)).where(RoutingCorrection.company_id == user.company_id)
    ) or 0
    action_count = db.scalar(
        select(func.count(RoutingAction.id)).where(RoutingAction.company_id == user.company_id)
    ) or 0
    return {
        "request": request,
        "user": user,
        "title": "Historial",
        "items": items,
        "search": search,
        "filters": {
            "date_range": request.query_params.get("date_range", "90d"),
            "date_from": request.query_params.get("date_from", ""),
            "date_to": request.query_params.get("date_to", ""),
            "department_id": department_id or "",
            "kind": kind,
            "state": state,
            "source": source,
        },
        "departments": all_departments,
        "categories": db.scalars(
            select(RoutingDecision.category)
            .where(RoutingDecision.company_id == user.company_id)
            .distinct()
            .order_by(RoutingDecision.category)
        ).all(),
        "sources": db.scalars(
            select(RoutingDecision.source)
            .where(RoutingDecision.company_id == user.company_id)
            .distinct()
            .order_by(RoutingDecision.source)
        ).all(),
        "summary": {
            "communications": total,
            "corrections": correction_count,
            "actions": action_count,
        },
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_items": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
            "has_next": page < ((total + page_size - 1) // page_size if total else 0),
            "has_previous": page > 1,
            "start_item": (page - 1) * page_size + 1 if total else 0,
            "end_item": min(page * page_size, total),
            "allowed_page_sizes": (25, 50, 100),
            "previous_url": _history_pagination_url(request, page - 1, page_size) if page > 1 else "",
            "next_url": _history_pagination_url(request, page + 1, page_size) if page < ((total + page_size - 1) // page_size if total else 0) else "",
        },
    }
HISTORY_EMAIL_CURRENT_STATUSES = (
    "not_processed",
    "pending",
    "queued",
    "processing",
    "pending_reprocess",
    "doubtful",
    "processed_doubtful",
    "review",
    "error",
    "processing_error",
)
HISTORY_EMAIL_REVIEW_STATUSES = (
    "doubtful",
    "processed_doubtful",
    "pending_reprocess",
    "review",
    "error",
    "processing_error",
)
HISTORY_EMAIL_READY_STATUSES = (
    "processed",
    "processed_order_detected",
    "order_detected",
    "matched",
)


def _empty_workbench_summary(filters: dict) -> dict:
    return {
        "tab_counts": {"all": 0, "not_processed": 0, "attention": 0, "processed": 0, "errors": 0, "no_order": 0},
        "items": [],
        "pagination": {
            "page": int(filters.get("page") or 1),
            "page_size": int(filters.get("page_size") or 25),
            "total_items": 0,
            "total_pages": 0,
            "has_next": False,
            "has_previous": False,
            "start_item": 0,
            "end_item": 0,
            "allowed_page_sizes": (10, 25, 50, 100),
        },
        "filters_applied": filters,
    }


def _normalize_featured_process_item(item: dict) -> dict:
    normalized = dict(item)
    normalized["date"] = normalized.get("date") or normalized.get("received_at")
    normalized["customer_name"] = (
        normalized.get("customer_name")
        or normalized.get("customer")
        or normalized.get("from_email")
        or normalized.get("sender")
        or "Cliente no identificado"
    )
    normalized["origin"] = normalized.get("origin") or normalized.get("channel") or "PDF"
    normalized["score"] = normalized.get("score") if normalized.get("score") is not None else 0
    normalized["category_label"] = normalized.get("category_label") or normalized.get("category") or "Sin analizar"
    normalized["subject"] = normalized.get("subject") or "Pedido compra"
    normalized["detail_url"] = normalized.get("detail_url") or (
        f"/orders/{normalized['order_id']}" if normalized.get("order_id") else f"/?focus=email-{normalized['email_id']}" if normalized.get("email_id") else "/"
    )
    return normalized


def _history_bounds(date_range: str, date_from: str, date_to: str) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(timezone.utc)
    if date_from or date_to:
        start = _aware(datetime.fromisoformat(date_from)) if date_from else None
        end = _aware(datetime.fromisoformat(f"{date_to}T23:59:59")) if date_to else None
        return start, end
    ranges = {
        "today": (datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc), None),
        "yesterday": (
            datetime.combine((now - timedelta(days=1)).date(), datetime.min.time(), tzinfo=timezone.utc),
            datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc),
        ),
        "7d": (now - timedelta(days=7), None),
        "30d": (now - timedelta(days=30), None),
        "90d": (now - timedelta(days=90), None),
        "365d": (now - timedelta(days=365), None),
    }
    return ranges.get(date_range, (None, None))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _history_scoring_category_expr(settings: ScoringSettings):
    return case(
        (Order.score.is_(None), literal("without_score")),
        (Order.score >= settings.safe_threshold, literal("safe")),
        (Order.score >= settings.review_threshold, literal("reviewable")),
        (Order.score >= settings.doubtful_threshold, literal("doubtful")),
        else_=literal("not_importable"),
    )


def _history_blocked_expr(settings: ScoringSettings, metrics):
    return or_(
        and_(settings.block_without_customer, Order.validated_customer_id.is_(None)),
        and_(settings.block_without_reference, func.coalesce(metrics.c.missing_product_count, 0) > 0),
        and_(settings.block_without_quantity, func.coalesce(metrics.c.invalid_quantity_count, 0) > 0),
        and_(settings.block_below_threshold, or_(Order.score.is_(None), Order.score < settings.doubtful_threshold)),
    )


def _history_order_state_expr(settings: ScoringSettings, metrics):
    scoring_category = _history_scoring_category_expr(settings)
    blocked_expr = _history_blocked_expr(settings, metrics)
    return case(
        (Order.status.in_(tuple(ERROR_ORDER_STATUSES)), literal("error")),
        (blocked_expr, literal("blocked")),
        (
            and_(scoring_category == "safe", Order.status.in_(tuple(PENDING_ORDER_STATUSES))),
            literal("ready"),
        ),
        (
            or_(scoring_category.in_(("reviewable", "doubtful")), Order.status.in_(tuple(REVIEW_ORDER_STATUSES))),
            literal("review"),
        ),
        (Order.status == "pedido_exportado", literal("exported")),
        else_=literal("normal"),
    )


def _history_order_metrics_subquery(company_id: int):
    return (
        select(
            OrderLine.order_id.label("order_id"),
            func.count(OrderLine.id).label("line_count"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            (OrderLine.validation_status != "validated")
                            | (OrderLine.validated_product_id.is_(None))
                            | (OrderLine.doubt_reason.is_not(None)),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("doubt_count"),
            func.coalesce(func.sum(case((OrderLine.validated_product_id.is_(None), 1), else_=0)), 0).label("missing_product_count"),
            func.coalesce(func.sum(case(((OrderLine.quantity.is_(None)) | (OrderLine.quantity <= 0), 1), else_=0)), 0).label("invalid_quantity_count"),
        )
        .where(OrderLine.company_id == company_id)
        .group_by(OrderLine.order_id)
        .subquery()
    )


def _history_order_rows_stmt(
    company_id: int,
    scoring_settings: ScoringSettings,
    *,
    start: datetime | None,
    end: datetime | None,
    customer_id: str,
    search: str,
    state: str,
):
    metrics = _history_order_metrics_subquery(company_id)
    scoring_category = _history_scoring_category_expr(scoring_settings)
    blocked_expr = _history_blocked_expr(scoring_settings, metrics)
    op_state = _history_order_state_expr(scoring_settings, metrics)
    stmt = (
        select(
            literal("order").label("kind"),
            Order.id.label("item_id"),
            Order.created_at.label("sort_date"),
            scoring_category.label("scoring_category"),
            op_state.label("order_state"),
            Order.status.label("order_status"),
            func.coalesce(metrics.c.line_count, 0).label("line_count"),
            func.coalesce(metrics.c.doubt_count, 0).label("doubt_count"),
            func.coalesce(metrics.c.missing_product_count, 0).label("missing_product_count"),
            func.coalesce(metrics.c.invalid_quantity_count, 0).label("invalid_quantity_count"),
            Order.customer_detected_name.label("customer_detected_name"),
            Order.score.label("score"),
            Order.validated_customer_id.label("validated_customer_id"),
            Order.customer_id.label("customer_id"),
            Order.conversation_id.label("conversation_id"),
            func.coalesce(Email.agent_status, literal("not_processed")).label("agent_status"),
            Email.subject.label("subject"),
            Email.sender.label("sender"),
        )
        .select_from(Order)
        .outerjoin(Email, Order.email_id == Email.id)
        .outerjoin(metrics, metrics.c.order_id == Order.id)
        .where(Order.company_id == company_id)
    )
    if start:
        stmt = stmt.where(Order.created_at >= start)
    if end:
        stmt = stmt.where(Order.created_at <= end)
    if customer_id and customer_id != "0":
        cid = int(customer_id)
        stmt = stmt.where((Order.customer_id == cid) | (Order.validated_customer_id == cid))
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(Email.subject.ilike(like), Email.sender.ilike(like), Order.customer_detected_name.ilike(like)))
    if state == "current":
        stmt = stmt.where(Order.status.not_in(tuple(TERMINAL_ORDER_STATUSES)))
    elif state == "review":
        stmt = stmt.where(
            or_(
                scoring_category.in_(("reviewable", "doubtful")),
                blocked_expr,
                Order.status.in_(tuple(REVIEW_ORDER_STATUSES)),
            )
        )
    elif state == "ready":
        stmt = stmt.where(and_(scoring_category == "safe", Order.status.in_(tuple(PENDING_ORDER_STATUSES))))
    elif state == "confirmed":
        stmt = stmt.where(Order.status.in_(("pedido_confirmado", "pedido_validado")))
    elif state == "sent":
        stmt = stmt.where(Order.status == "pedido_exportado")
    elif state == "blocked":
        stmt = stmt.where(or_(blocked_expr, Order.status.in_(tuple(REVIEW_ORDER_STATUSES))))
    return stmt


def _history_email_rows_stmt(
    company_id: int,
    *,
    start: datetime | None,
    end: datetime | None,
    customer_id: str,
    search: str,
):
    stmt = (
        select(
            literal("email").label("kind"),
            Email.id.label("item_id"),
            Email.received_at.label("sort_date"),
            literal("without_score").label("scoring_category"),
            literal("processed").label("order_state"),
            literal("").label("order_status"),
            literal(0).label("line_count"),
            literal(0).label("doubt_count"),
            literal(0).label("missing_product_count"),
            literal(0).label("invalid_quantity_count"),
            literal("").label("customer_detected_name"),
            literal(None).label("score"),
            literal(None).label("validated_customer_id"),
            literal(None).label("customer_id"),
            Email.conversation_id.label("conversation_id"),
            func.coalesce(Email.agent_status, literal("not_processed")).label("agent_status"),
            Email.subject.label("subject"),
            Email.sender.label("sender"),
        )
        .where(Email.company_id == company_id)
    )
    if start:
        stmt = stmt.where(Email.received_at >= start)
    if end:
        stmt = stmt.where(Email.received_at <= end)
    if customer_id and customer_id != "0":
        cid = int(customer_id)
        stmt = stmt.where(
            exists(
                select(1).where(
                    Order.company_id == company_id,
                    Order.email_id == Email.id,
                    or_(Order.customer_id == cid, Order.validated_customer_id == cid),
                )
            )
        )
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(Email.subject.ilike(like), Email.sender.ilike(like), Email.body.ilike(like)))
    return stmt


@router.get("/")
def root_page(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    customer_id: str = "",
    status: str = "",
    email_type: str = "",
    score_min: str = "",
    score_max: str = "",
    scoring_category: str = "",
    agent_status: str = "",
    date_range: str = "7d",
    customer_or_sender: str = "",
    has_attachments: str = "",
    order_status: str = "",
    mode: str = "",
    tab: str = "",
    work_status: str = "",
    quick_range: str = "today",
    has_pdf: str = "",
    requires_review: str = "",
    issue_type: str = "",
    origin: str = "",
    sender: str = "",
    search: str = "",
    reason: str = "",
    sort: str = "date_desc",
    partial: str = "",
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    return dashboard(
        request,
        date_from=date_from,
        date_to=date_to,
        customer_id=customer_id,
        status=status,
        email_type=email_type,
        score_min=score_min,
        score_max=score_max,
        scoring_category=scoring_category,
        agent_status=agent_status,
        date_range=date_range,
        customer_or_sender=customer_or_sender,
        has_attachments=has_attachments,
        order_status=order_status,
        mode=mode,
        tab=tab,
        work_status=work_status,
        quick_range=quick_range,
        has_pdf=has_pdf,
        requires_review=requires_review,
        issue_type=issue_type,
        origin=origin,
        sender=sender,
        search=search,
        reason=reason,
        sort=sort,
        partial=partial,
        page=page,
        page_size=page_size,
        db=db,
        user=user,
    )


@router.get("/inicio")
def dashboard(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    customer_id: str = "",
    status: str = "",
    email_type: str = "",
    score_min: str = "",
    score_max: str = "",
    scoring_category: str = "",
    agent_status: str = "",
    date_range: str = "7d",
    customer_or_sender: str = "",
    has_attachments: str = "",
    order_status: str = "",
    mode: str = "",
    tab: str = "",
    work_status: str = "",
    quick_range: str = "today",
    has_pdf: str = "",
    requires_review: str = "",
    issue_type: str = "",
    origin: str = "",
    sender: str = "",
    search: str = "",
    reason: str = "",
    sort: str = "date_desc",
    partial: str = "",
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if request.url.path == "/inicio":
        redirect_target = "/" if not request.url.query else f"/?{request.url.query}"
        return RedirectResponse(redirect_target, status_code=303)

    if (
        request.url.path == "/"
        and get_settings().app_slug.strip().lower() == "kibak"
        and _is_postgresql_session(db)
    ):
        request.state.enabled_channels = ("email",)
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "title": "Dashboard",
                "dashboard": kibak_dashboard_summary(db, user.company_id),
            },
        )

    setup_operational, enabled_channels = setup_operational_context(db, user.company_id)
    request.state.enabled_channels = enabled_channels
    if not setup_operational:
        setup_status = get_setup_status(db, user.company_id)
        missing = [
            item["label"]
            for item in setup_status.steps
            if item["status"] not in {"Completado", "Opcional"}
        ]
        return templates.TemplateResponse(
            "setup/required.html",
            {
                "request": request,
                "user": user,
                "title": "Configuración pendiente",
                "setup_status": setup_status,
                "missing_steps": missing,
            },
        )

    active_mode = mode or tab or "all"
    is_orders_cards = request.url.path.startswith("/orders")
    if (
        not is_orders_cards
        and partial != "workbench"
        and get_settings().app_slug.strip().lower() == "kibak"
        and _is_postgresql_session(db)
    ):
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "title": "Dashboard",
                "dashboard": kibak_dashboard_summary(db, user.company_id),
            },
        )
    default_date_range = "" if is_orders_cards else "7d"
    resolved_date_range = date_range or quick_range or default_date_range
    filters = {"date_from": date_from, "date_to": date_to, "customer_id": customer_id, "status": status, "email_type": email_type, "score_min": score_min, "score_max": score_max, "scoring_category": scoring_category, "agent_status": agent_status, "date_range": resolved_date_range, "customer_or_sender": customer_or_sender, "has_attachments": has_attachments, "order_status": order_status, "mode": active_mode, "tab": active_mode, "work_status": work_status, "quick_range": resolved_date_range, "has_pdf": has_pdf, "requires_review": requires_review, "issue_type": issue_type, "origin": origin, "sender": sender, "search": search, "reason": reason, "page": page, "page_size": page_size, "sort": sort, "archived": False}

    try:
        if is_orders_cards:
            order_view = load_order_view_data(db, user.company_id, filters)
            workbench = orders_workbench_summary(order_view, filters)
        else:
            workbench = workbench_summary(db, user.company_id, filters, include_metrics=False)
    except Exception:
        workbench = _empty_workbench_summary(filters)

    featured_process_item = None
    if workbench["items"]:
        featured_process_item = _normalize_featured_process_item(workbench["items"][0])

    template_name = (
        "dashboard/_workbench.html"
        if partial == "workbench"
        else "dashboard.html"
        if _is_kibak_runtime(db)
        else "dashboard_legacy.html"
    )
    if is_orders_cards:
        orders_view_query = {key: value for key, value in request.query_params.items() if key != "partial"}
        orders_view_query["view"] = "cards"
        view_cards_url = f"/orders?{urlencode(orders_view_query)}"
        orders_view_query["view"] = "list"
        view_list_url = f"/orders?{urlencode(orders_view_query)}"
    else:
        view_cards_url = "/"
        view_list_url = "/orders?view=list"

    return templates.TemplateResponse(
        template_name,
        {
            "request": request,
            "user": user,
            "workbench": workbench,
            "featured_process_item": featured_process_item,
            "filters": filters,
            "pagination": workbench["pagination"],
            "current_view": "cards",
            "view_cards_url": view_cards_url,
            "view_list_url": view_list_url,
            "dashboard_action": "/orders" if request.url.path.startswith("/orders") else "/",
        },
    )
@router.get("/history")
def history_page(
    request: Request,
    date_range: str = "90d",
    date_from: str = "",
    date_to: str = "",
    kind: str = "all",
    state: str = "all",
    department_id: int | None = None,
    source: str = "all",
    customer_id: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = 50,
    selected_id: int | None = None,
    selected_kind: str = "email",
    partial: str = "",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    start, end = _history_bounds(date_range, date_from, date_to)
    if _is_kibak_runtime(db):
        return templates.TemplateResponse(
            "history/kibak.html",
            _kibak_history_context(
                db,
                user,
                request=request,
                search=search,
                page=page,
                page_size=page_size,
                start=start,
                end=end,
                department_id=department_id,
                kind=kind,
                state=state,
                source=source,
            ),
        )
    scoring_settings = get_or_create_settings(db, ScoringSettings, user.company_id)
    allowed_kind = kind if kind in {"all", "orders", "emails"} else "all"
    allowed_state = state if state in {"all", "current", "review", "ready", "confirmed", "sent", "blocked"} else "all"
    suggestion_maps = _customer_suggestion_maps(db, user.company_id)

    order_base_stmt = _history_order_rows_stmt(
        user.company_id,
        scoring_settings,
        start=start,
        end=end,
        customer_id=customer_id,
        search=search,
        state="all",
    )
    email_base_stmt = _history_email_rows_stmt(
        user.company_id,
        start=start,
        end=end,
        customer_id=customer_id,
        search=search,
    )
    if allowed_kind == "all":
        email_base_stmt = email_base_stmt.where(
            ~exists(
                select(1).where(
                    Order.company_id == user.company_id,
                    Order.email_id == Email.id,
                )
            )
        )

    base_parts = []
    if allowed_kind in {"all", "orders"}:
        base_parts.append(order_base_stmt)
    if allowed_kind in {"all", "emails"}:
        base_parts.append(email_base_stmt)

    base_union = union_all(*base_parts).subquery() if base_parts else None

    page, page_size = normalize_page(page, page_size)
    start_index = (page - 1) * page_size

    tab_counts = {key: 0 for key in ("all", "current", "review", "ready", "confirmed", "sent", "blocked")}
    summary = {"events": 0, "orders": 0, "emails": 0, "review": 0, "ready": 0}
    paged_items: list[dict] = []
    total_items = 0
    total_pages = 0
    start_item = 0
    end_item = 0
    current_union = None

    if base_union is not None:
        pred_current = or_(
            and_(base_union.c.kind == "order", base_union.c.order_status.notin_(TERMINAL_ORDER_STATUSES)),
            and_(base_union.c.kind == "email", base_union.c.agent_status.in_(HISTORY_EMAIL_CURRENT_STATUSES)),
        )
        pred_review = or_(
            and_(
                base_union.c.kind == "order",
                or_(
                    base_union.c.order_state == "blocked",
                    base_union.c.scoring_category.in_(("reviewable", "doubtful")),
                    base_union.c.order_status.in_(tuple(REVIEW_ORDER_STATUSES)),
                ),
            ),
            and_(base_union.c.kind == "email", base_union.c.agent_status.in_(HISTORY_EMAIL_REVIEW_STATUSES)),
        )
        pred_ready = or_(
            and_(base_union.c.kind == "order", base_union.c.order_state == "ready"),
            and_(base_union.c.kind == "email", base_union.c.agent_status.in_(HISTORY_EMAIL_READY_STATUSES)),
        )
        pred_confirmed = and_(base_union.c.kind == "order", base_union.c.order_status.in_(("pedido_confirmado", "pedido_validado")))
        pred_sent = and_(base_union.c.kind == "order", base_union.c.order_status == "pedido_exportado")
        pred_blocked = and_(base_union.c.kind == "order", or_(base_union.c.order_state == "blocked", base_union.c.order_status.in_(tuple(REVIEW_ORDER_STATUSES))))

        base_counts_row = db.execute(
            select(
                func.count().label("all_count"),
                func.coalesce(func.sum(case((base_union.c.kind == "order", 1), else_=0)), 0).label("orders"),
                func.coalesce(func.sum(case((base_union.c.kind == "email", 1), else_=0)), 0).label("emails"),
                func.coalesce(func.sum(case((pred_current, 1), else_=0)), 0).label("current"),
                func.coalesce(func.sum(case((pred_review, 1), else_=0)), 0).label("review"),
                func.coalesce(func.sum(case((pred_ready, 1), else_=0)), 0).label("ready"),
                func.coalesce(func.sum(case((pred_confirmed, 1), else_=0)), 0).label("confirmed"),
                func.coalesce(func.sum(case((pred_sent, 1), else_=0)), 0).label("sent"),
                func.coalesce(func.sum(case((pred_blocked, 1), else_=0)), 0).label("blocked"),
            ).select_from(base_union)
        ).one()
        base_counts = base_counts_row._mapping
        tab_counts = {
            "all": int(base_counts["all_count"] or 0),
            "current": int(base_counts["current"] or 0),
            "review": int(base_counts["review"] or 0),
            "ready": int(base_counts["ready"] or 0),
            "confirmed": int(base_counts["confirmed"] or 0),
            "sent": int(base_counts["sent"] or 0),
            "blocked": int(base_counts["blocked"] or 0),
        }

        state_preds = {
            "current": pred_current,
            "review": pred_review,
            "ready": pred_ready,
            "confirmed": pred_confirmed,
            "sent": pred_sent,
            "blocked": pred_blocked,
        }

        if allowed_state != "all" and allowed_state in state_preds:
            current_union = select(base_union).where(state_preds[allowed_state]).subquery()
            total_items = tab_counts[allowed_state]
        else:
            current_union = base_union
            total_items = tab_counts["all"]

        summary = {
            "events": total_items,
            "orders": int(base_counts["orders"] or 0),
            "emails": int(base_counts["emails"] or 0),
            "review": int(base_counts["review"] or 0),
            "ready": int(base_counts["ready"] or 0),
        }

        total_pages = (total_items + page_size - 1) // page_size if total_items else 0
        paged_rows = db.execute(
            select(current_union).order_by(current_union.c.sort_date.desc(), current_union.c.kind.asc(), current_union.c.item_id.desc()).offset(start_index).limit(page_size)
        ).all()
        start_item = start_index + 1 if total_items else 0
        end_item = min(start_index + page_size, total_items)

        order_ids = [row.item_id for row in paged_rows if row.kind == "order"]
        email_ids = [row.item_id for row in paged_rows if row.kind == "email"]
        orders_by_id = {}
        emails_by_id = {}
        line_metrics_by_order = _load_order_line_metrics(db, user.company_id, order_ids) if order_ids else {}
        if order_ids:
            orders = db.scalars(
                select(Order)
                .where(Order.company_id == user.company_id, Order.id.in_(order_ids))
                .options(
                    selectinload(Order.email).selectinload(Email.attachments),
                    selectinload(Order.customer),
                    selectinload(Order.validated_customer),
                )
            ).unique().all()
            orders_by_id = {order.id: order for order in orders}
        if email_ids:
            emails = db.scalars(
                select(Email)
                .where(Email.company_id == user.company_id, Email.id.in_(email_ids))
                .options(selectinload(Email.attachments))
            ).all()
            emails_by_id = {email.id: email for email in emails}

        for row in paged_rows:
            if row.kind == "order":
                order = orders_by_id.get(row.item_id)
                if not order:
                    continue
                item = order_workbench_item(order, scoring_settings, line_metrics_by_order=line_metrics_by_order)
                item.update(
                    {
                        "kind_label": "Pedido",
                        "date": _aware(order.created_at),
                        # An order without a linked email is not an unread email.
                        "is_read": True if order.email is None else bool(order.email.is_read),
                        "is_favorite": bool(order.email and getattr(order.email, "is_favorite", False)),
                        "url": f"/orders/{order.id}",
                        "secondary_url": f"/?tab=processed&date_range=30d&search={quote_plus(order.email.subject or order.email.sender or order.customer_detected_name or '')}" if order.email else "/orders",
                        "title": item["customer_name"] or order.customer_detected_name or "Pedido",
                        "subtitle": order.email.subject if order.email else "",
                    }
                )
                paged_items.append(item)
            else:
                email = emails_by_id.get(row.item_id)
                if not email:
                    continue
                item = email_workbench_item(email)
                item.update(
                    {
                        "kind_label": "Correo",
                        "date": _aware(email.received_at),
                        "is_read": bool(email.is_read),
                        "is_favorite": bool(getattr(email, "is_favorite", False)),
                        "url": f"/?tab=processed&date_range=30d&search={quote_plus(email.subject or email.sender or '')}",
                        "secondary_url": f"/?tab=email&date_range=30d&search={quote_plus(email.sender or email.subject or '')}",
                        "title": email.subject or "Correo sin asunto",
                        "subtitle": email.sender,
                        "customer_name": suggest_customer_for_email(db, user.company_id, email, suggestion_maps=suggestion_maps) or item["customer_name"],
                    }
                )
                paged_items.append(item)

    selected_email = None
    selected_order = None
    selected_item = None
    if selected_id:
        if selected_kind == "order":
            selected_order = db.scalar(
                select(Order)
                .where(Order.company_id == user.company_id, Order.id == selected_id)
                .options(
                    selectinload(Order.email).selectinload(Email.attachments),
                    selectinload(Order.lines),
                    selectinload(Order.customer),
                    selectinload(Order.validated_customer),
                )
            )
            if selected_order and selected_order.email:
                selected_email = selected_order.email
        else:
            selected_email = db.scalar(
                select(Email)
                .where(Email.company_id == user.company_id, Email.id == selected_id)
                .options(selectinload(Email.attachments))
            )
            if selected_email:
                selected_order = db.scalar(
                    select(Order)
                    .where(Order.company_id == user.company_id, Order.email_id == selected_email.id)
                    .options(
                        selectinload(Order.lines),
                        selectinload(Order.customer),
                        selectinload(Order.validated_customer),
                    )
                )

    if selected_email and not selected_email.is_read:
        selected_email.is_read = True
        db.commit()
        for row_item in paged_items:
            if row_item.get("email_id") == selected_email.id:
                row_item["is_read"] = True

    customers = db.scalars(select(Customer).where(Customer.company_id == user.company_id).order_by(Customer.fiscal_name)).all()
    template_name = "history/_list_pane.html" if partial == "list" else "history/list.html"
    return templates.TemplateResponse(
        template_name,
        {
            "request": request,
            "user": user,
            "summary": summary,
            "items": paged_items,
            "all_items": paged_items,
            "tab_counts": tab_counts,
            "customers": customers,
            "selected_id": selected_id,
            "selected_kind": selected_kind,
            "selected_email": selected_email,
            "selected_order": selected_order,
            "selected_item": selected_item,
            "filters": {"date_range": date_range, "date_from": date_from, "date_to": date_to, "kind": kind, "state": state, "customer_id": customer_id, "search": search},
            "pagination": {"page": page, "page_size": page_size, "total_items": total_items, "total_pages": total_pages, "has_previous": page > 1, "has_next": page < total_pages, "start_item": start_item, "end_item": end_item, "allowed_page_sizes": (10, 25, 50, 100)},
        },
    )


@router.get("/pedidos")
def pedidos_page(
    request: Request,
    user: TenantUser = Depends(current_user),
):
    query = request.url.query
    dest = f"/history?{query}" if query else "/history"
    return RedirectResponse(dest, status_code=303)


@router.get("/history/pane/{kind}/{item_id}")
def history_detail_pane(
    kind: str,
    item_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if _is_kibak_runtime(db):
        communication = db.scalar(
            select(Communication).where(
                Communication.company_id == user.company_id,
                Communication.id == item_id,
            )
        )
        return templates.TemplateResponse(
            "history/_kibak_detail_pane.html",
            {"request": request, "user": user, "communication": communication},
        )
    email = None
    order = None
    item = None
    if kind == "order":
        order = db.scalar(
            select(Order)
            .where(Order.company_id == user.company_id, Order.id == item_id)
            .options(
                selectinload(Order.email).selectinload(Email.attachments),
                selectinload(Order.lines),
                selectinload(Order.customer),
                selectinload(Order.validated_customer),
            )
        )
        if order and order.email:
            email = order.email
            item = email_workbench_item(email)
    else:
        email = db.scalar(
            select(Email)
            .where(Email.company_id == user.company_id, Email.id == item_id)
            .options(selectinload(Email.attachments))
        )
        if email:
            order = db.scalar(
                select(Order)
                .where(Order.company_id == user.company_id, Order.email_id == email.id)
                .options(
                    selectinload(Order.lines),
                    selectinload(Order.customer),
                    selectinload(Order.validated_customer),
                )
            )
            item = email_workbench_item(email)
            if order:
                item["order_id"] = order.id
                item["order_status"] = order.status
                item["order_status_label"] = order.status
                item["score"] = order.score

    if email and not email.is_read:
        email.is_read = True
        db.commit()

    return templates.TemplateResponse(
        "history/_mail_detail_pane.html",
        {
            "request": request,
            "user": user,
            "email": email,
            "order": order,
            "item": item,
        },
    )
