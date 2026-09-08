import logging

from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.auth.redirects import DEFAULT_LOGIN_DESTINATION, safe_internal_next
from app.auth.service import authenticate_user
from app.core.templating import templates
from app.master.models import MasterCompany
from app.core.config import get_settings
from app.master.database import get_master_db
from app.setup.service import get_setup_status
from app.tenancy.database import tenant_db_session
from app.settings.branding import branding_to_dict, default_branding_payload

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/login")
def login_page(request: Request, next: str = ""):
    next_url = safe_internal_next(next)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "next_url": next_url,
            "login_branding": branding_to_dict(default_branding_payload()),
        },
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(DEFAULT_LOGIN_DESTINATION),
    master_db: Session = Depends(get_master_db),
):
    next_url = safe_internal_next(next)
    user = authenticate_user(master_db, email, password)

    if not user:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Credenciales no validas",
                "next_url": next_url,
                "login_branding": branding_to_dict(default_branding_payload()),
            },
            status_code=401,
        )

    request.session["user_id"] = user.id
    request.session["company_id"] = user.company_id
    request.session["membership_id"] = user.membership_id
    request.session["company_slug"] = user.company_slug

    if (
        next_url == DEFAULT_LOGIN_DESTINATION
        and getattr(user, "database_url", None)
        and (
            get_settings().app_slug.strip().lower() != "kibak"
            or not str(user.database_url).startswith("postgresql")
        )
    ):
        TenantSession = tenant_db_session(user.database_url)
        tenant_db = TenantSession()
        try:
            if not get_setup_status(tenant_db, user.company_id).is_operational:
                next_url = "/setup"
        finally:
            tenant_db.close()

    company = master_db.get(MasterCompany, user.company_id)
    settings = get_settings()

    logger.info(
        "Login correcto: user_id=%s email=%s company_id=%s company=%s env=%s",
        user.id,
        user.email,
        user.company_id,
        company.name if company else "",
        settings.environment,
    )

    return RedirectResponse(next_url, status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
