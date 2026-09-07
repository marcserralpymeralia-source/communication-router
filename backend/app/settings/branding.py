import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import BrandingSettings, utcnow


DEFAULT_THEME: dict[str, Any] = {
    "colors": {
        "primary": "#123A32",
        "primary_dark": "#0B2924",
        "primary_light": "#EEF6EF",
        "accent_green": "#6BAA45",
        "accent_red": "#D61F2C",
        "text": "#1B1F22",
        "muted": "#5F6B73",
        "background": "#F5F7F6",
        "surface": "#FFFFFF",
        "border": "#DDE5E2",
        "kraft": "#F3EFE5",
        "table_alt": "#F8FAF9",
    },
    "sidebar": {
        "background": "#123A32",
        "text": "#FFFFFF",
        "muted": "#D7E5DF",
        "active_background": "#FFFFFF",
        "active_text": "#123A32",
        "hover": "#1E4A40",
        "width": 260,
    },
    "buttons": {
        "primary": "#123A32",
        "primary_hover": "#0B2924",
        "primary_text": "#FFFFFF",
        "secondary": "#EAF0EE",
        "secondary_text": "#1B1F22",
        "danger": "#D61F2C",
        "success": "#6BAA45",
        "radius": 8,
        "font_size": 14,
    },
    "cards": {
        "background": "#FFFFFF",
        "border": "#DDE5E2",
        "radius": 14,
        "shadow": "0 8px 24px rgba(18,58,50,.08)",
        "padding": 16,
        "page_background": "#F5F7F6",
    },
    "tables": {
        "header_background": "#EEF3F1",
        "header_text": "#31443F",
        "row_odd": "#FFFFFF",
        "row_even": "#F8FAF9",
        "row_hover": "#EEF6EF",
        "border": "#DDE5E2",
        "vertical_padding": 14,
        "alternate_rows": True,
        "scoring_left_border": True,
    },
    "scoring": {
        "safe": "#2E8B57",
        "reviewable": "#D6A700",
        "doubtful": "#E67E22",
        "not_importable": "#D61F2C",
        "without_score": "#8A969D",
        "show_bar": True,
        "show_label": True,
        "show_percentage": True,
    },
    "status_badges": {
        "pending_review_bg": "#EAF0EE",
        "pending_review_text": "#1B1F22",
        "confirmed_bg": "#EEF6EF",
        "confirmed_text": "#2E8B57",
        "exported_bg": "#DFF3E5",
        "exported_text": "#1F6B43",
        "error_bg": "#FDECEC",
        "error_text": "#D61F2C",
        "no_order_bg": "#ECEFF1",
        "no_order_text": "#5F6B73",
        "doubtful_bg": "#FFF3E0",
        "doubtful_text": "#E67E22",
        "discarded_bg": "#F2F2F2",
        "discarded_text": "#5F6B73",
    },
    "login": {
        "show_logo": True,
        "title": "KIBAK",
        "subtitle": "Comunicaciones y routing inteligente",
        "background": "#F5F7F6",
        "card": "#FFFFFF",
        "button": "#123A32",
    },
    "typography": {"font_family": "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"},
    "layout": {"border_radius": 8},
}


DEFAULT_MICROCOPY = {
    "create_test_order": "Crear pedido de prueba",
    "review_order": "Revisar pedido",
    "confirm_order": "Confirmar pedido",
    "generate_file": "Generar archivo",
    "send_to_management": "Enviar a gestion",
    "recalculate_score": "Recalcular scoring",
    "save_changes": "Guardar cambios",
    "empty_orders": "No hay pedidos para los filtros seleccionados.",
    "empty_results": "No se han encontrado resultados.",
    "generic_error": "Ha ocurrido un error. Revisa los detalles o intentalo de nuevo.",
    "save_success": "Cambios guardados correctamente.",
    "export_success": "Pedido enviado correctamente al sistema de gestion.",
}

BRANDING_UPLOAD_DIR = Path(__file__).resolve().parents[1] / "static" / "uploads" / "branding"
ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg", ".webp"}


def deep_merge(default: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(default)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def default_branding_payload() -> dict[str, Any]:
    settings = get_settings()
    company_name = settings.default_company_name
    app_name = settings.branding_app_name or settings.app_name
    return {
        "company_name": company_name,
        "app_name": app_name,
        "primary_claim": settings.branding_primary_claim,
        "secondary_claim": settings.branding_secondary_claim,
        "short_description": "Gestiona comunicaciones, departamentos y routing en un solo lugar.",
        "logo_url": settings.branding_logo_url,
        "dark_logo_url": settings.branding_dark_logo_url,
        "favicon_url": settings.branding_favicon_url,
        "show_logo_sidebar": True,
        "show_app_name_sidebar": True,
        "show_claim_sidebar": True,
        "show_claim_login": True,
        "theme": deepcopy(DEFAULT_THEME),
        "microcopy": deepcopy(DEFAULT_MICROCOPY),
    }


def get_or_create_branding(db: Session, company_id: int) -> BrandingSettings:
    branding = db.query(BrandingSettings).filter(BrandingSettings.company_id == company_id).one_or_none()
    if branding:
        return branding
    payload = default_branding_payload()
    branding = BrandingSettings(
        company_id=company_id,
        app_name=payload["app_name"],
        company_name=payload["company_name"],
        primary_claim=payload["primary_claim"],
        secondary_claim=payload["secondary_claim"],
        short_description=payload["short_description"],
        logo_url=payload["logo_url"],
        dark_logo_url=payload["dark_logo_url"],
        favicon_url=payload["favicon_url"],
        show_logo_sidebar=True,
        show_app_name_sidebar=True,
        show_claim_sidebar=True,
        show_claim_login=True,
        theme_json=json.dumps(payload["theme"]),
        microcopy_json=json.dumps(payload["microcopy"]),
    )
    db.add(branding)
    db.commit()
    db.refresh(branding)
    return branding


def branding_to_dict(branding: BrandingSettings) -> dict[str, Any]:
    if isinstance(branding, dict):
        theme = deep_merge(DEFAULT_THEME, deepcopy(branding.get("theme") or {}))
        microcopy = deep_merge(DEFAULT_MICROCOPY, deepcopy(branding.get("microcopy") or {}))
        return {
            "company_name": branding.get("company_name", ""),
            "app_name": branding.get("app_name", ""),
            "primary_claim": branding.get("primary_claim", ""),
            "secondary_claim": branding.get("secondary_claim", ""),
            "short_description": branding.get("short_description", ""),
            "logo_url": branding.get("logo_url") or "",
            "dark_logo_url": branding.get("dark_logo_url") or "",
            "favicon_url": branding.get("favicon_url") or "",
            "show_logo_sidebar": branding.get("show_logo_sidebar", True),
            "show_app_name_sidebar": branding.get("show_app_name_sidebar", True),
            "show_claim_sidebar": branding.get("show_claim_sidebar", True),
            "show_claim_login": branding.get("show_claim_login", True),
            "theme": theme,
            "microcopy": microcopy,
        }
    theme = deep_merge(DEFAULT_THEME, json.loads(branding.theme_json or "{}"))
    microcopy = deep_merge(DEFAULT_MICROCOPY, json.loads(branding.microcopy_json or "{}"))
    return {
        "company_name": branding.company_name,
        "app_name": branding.app_name,
        "primary_claim": branding.primary_claim,
        "secondary_claim": branding.secondary_claim,
        "short_description": branding.short_description,
        "logo_url": branding.logo_url or "",
        "dark_logo_url": branding.dark_logo_url or "",
        "favicon_url": branding.favicon_url or "",
        "show_logo_sidebar": branding.show_logo_sidebar,
        "show_app_name_sidebar": branding.show_app_name_sidebar,
        "show_claim_sidebar": branding.show_claim_sidebar,
        "show_claim_login": branding.show_claim_login,
        "theme": theme,
        "microcopy": microcopy,
    }


def set_nested(data: dict[str, Any], path: str, value: Any) -> None:
    current = data
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def parse_form_value(value: str) -> Any:
    if value in {"on", "true", "True"}:
        return True
    if value in {"off", "false", "False"}:
        return False
    if value.isdigit():
        return int(value)
    return value


def update_branding_from_form(branding: BrandingSettings, form: dict[str, str], user_id: int | None) -> None:
    for field in [
        "company_name",
        "app_name",
        "primary_claim",
        "secondary_claim",
        "short_description",
        "logo_url",
        "dark_logo_url",
        "favicon_url",
    ]:
        if field in form:
            setattr(branding, field, form[field])
    for field in ["show_logo_sidebar", "show_app_name_sidebar", "show_claim_sidebar", "show_claim_login"]:
        setattr(branding, field, form.get(field) == "on")
    theme = deep_merge(DEFAULT_THEME, json.loads(branding.theme_json or "{}"))
    microcopy = deep_merge(DEFAULT_MICROCOPY, json.loads(branding.microcopy_json or "{}"))
    for key, value in form.items():
        if key.startswith("theme."):
            set_nested(theme, key.removeprefix("theme."), parse_form_value(value))
        elif key.startswith("microcopy."):
            set_nested(microcopy, key.removeprefix("microcopy."), value)
    branding.theme_json = json.dumps(theme)
    branding.microcopy_json = json.dumps(microcopy)
    branding.updated_by = user_id
    branding.updated_at = utcnow()


def is_internal_brand_asset(value: str | None) -> bool:
    return bool(value) and value.startswith("/static/uploads/branding/")


def delete_brand_asset(value: str | None) -> None:
    if not is_internal_brand_asset(value):
        return
    relative = value.removeprefix("/static/")
    path = Path(__file__).resolve().parents[1] / "static" / relative
    if path.exists():
        path.unlink()


async def store_brand_asset(company_id: int, upload: UploadFile, prefix: str) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in ALLOWED_LOGO_EXTENSIONS:
        raise ValueError("Formato de archivo no permitido. Usa PNG, JPG, SVG o WEBP.")
    BRANDING_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{company_id}-{prefix}-{uuid4().hex}{suffix}"
    path = BRANDING_UPLOAD_DIR / filename
    path.write_bytes(await upload.read())
    return f"/static/uploads/branding/{filename}"


def reset_branding(branding: BrandingSettings, user_id: int | None) -> None:
    payload = default_branding_payload()
    for key in ["company_name", "app_name", "primary_claim", "secondary_claim", "short_description", "logo_url", "dark_logo_url", "favicon_url"]:
        setattr(branding, key, payload[key])
    branding.show_logo_sidebar = True
    branding.show_app_name_sidebar = True
    branding.show_claim_sidebar = True
    branding.show_claim_login = True
    branding.theme_json = json.dumps(payload["theme"])
    branding.microcopy_json = json.dumps(payload["microcopy"])
    branding.updated_by = user_id
    branding.updated_at = utcnow()


def branding_css_vars(payload: dict[str, Any]) -> str:
    theme = payload["theme"]
    vars_map = {
        "--bg": theme["colors"]["background"],
        "--panel": theme["colors"]["surface"],
        "--text": theme["colors"]["text"],
        "--muted": theme["colors"]["muted"],
        "--line": theme["colors"]["border"],
        "--accent": theme["buttons"]["primary"],
        "--accent-dark": theme["buttons"]["primary_hover"],
        "--danger": theme["buttons"]["danger"],
        "--sidebar-bg": theme["sidebar"]["background"],
        "--sidebar-text": theme["sidebar"]["text"],
        "--sidebar-muted": theme["sidebar"]["muted"],
        "--sidebar-hover": theme["sidebar"]["hover"],
        "--sidebar-active-bg": theme["sidebar"]["active_background"],
        "--sidebar-active-text": theme["sidebar"]["active_text"],
        "--sidebar-width": f"{theme['sidebar']['width']}px",
        "--button-primary-text": theme["buttons"]["primary_text"],
        "--button-secondary-bg": theme["buttons"]["secondary"],
        "--button-secondary-text": theme["buttons"]["secondary_text"],
        "--button-radius": f"{theme['buttons']['radius']}px",
        "--button-font-size": f"{theme['buttons']['font_size']}px",
        "--card-bg": theme["cards"]["background"],
        "--card-border": theme["cards"]["border"],
        "--card-radius": f"{theme['cards']['radius']}px",
        "--card-shadow": theme["cards"]["shadow"],
        "--card-padding": f"{theme['cards']['padding']}px",
        "--table-head-bg": theme["tables"]["header_background"],
        "--table-head-text": theme["tables"]["header_text"],
        "--table-row-odd": theme["tables"]["row_odd"],
        "--table-row-even": theme["tables"]["row_even"],
        "--table-row-hover": theme["tables"]["row_hover"],
        "--table-border": theme["tables"]["border"],
        "--table-padding-y": f"{theme['tables']['vertical_padding']}px",
        "--score-safe": theme["scoring"]["safe"],
        "--score-reviewable": theme["scoring"]["reviewable"],
        "--score-doubtful": theme["scoring"]["doubtful"],
        "--score-not-importable": theme["scoring"]["not_importable"],
        "--score-without": theme["scoring"]["without_score"],
        "--login-bg": theme["login"]["background"],
        "--login-card": theme["login"]["card"],
        "--login-button": theme["login"]["button"],
        "--font-family": theme["typography"]["font_family"],
        "--status-pending-bg": theme["status_badges"]["pending_review_bg"],
        "--status-pending-text": theme["status_badges"]["pending_review_text"],
        "--status-confirmed-bg": theme["status_badges"]["confirmed_bg"],
        "--status-confirmed-text": theme["status_badges"]["confirmed_text"],
        "--status-exported-bg": theme["status_badges"]["exported_bg"],
        "--status-exported-text": theme["status_badges"]["exported_text"],
        "--status-error-bg": theme["status_badges"]["error_bg"],
        "--status-error-text": theme["status_badges"]["error_text"],
        "--status-no-order-bg": theme["status_badges"]["no_order_bg"],
        "--status-no-order-text": theme["status_badges"]["no_order_text"],
        "--status-doubtful-bg": theme["status_badges"]["doubtful_bg"],
        "--status-doubtful-text": theme["status_badges"]["doubtful_text"],
        "--status-discarded-bg": theme["status_badges"]["discarded_bg"],
        "--status-discarded-text": theme["status_badges"]["discarded_text"],
    }
    return ":root{" + "".join(f"{key}:{value};" for key, value in vars_map.items()) + "}"
