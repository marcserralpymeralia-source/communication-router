from __future__ import annotations

from typing import Any


DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
LEGACY_OPENAI_MODEL_FALLBACK = "gpt-4.1-mini"

OPENAI_MODEL_PRESETS: list[dict[str, str]] = [
    {
        "value": "gpt-5.6-luna",
        "label": "GPT-5.6 Luna",
        "description": "recomendado para alto volumen, baja latencia/coste.",
    },
    {
        "value": "gpt-5.6-terra",
        "label": "GPT-5.6 Terra",
        "description": "equilibrio calidad/coste.",
    },
    {
        "value": "gpt-5.6-sol",
        "label": "GPT-5.6 Sol",
        "description": "máxima capacidad.",
    },
    {
        "value": "gpt-4.1-mini",
        "label": "GPT-4.1 mini",
        "description": "ligero y rápido para extracción sencilla.",
    },
    {
        "value": "gpt-4.1",
        "label": "GPT-4.1",
        "description": "modelo generalista equilibrado.",
    },
]

OPENAI_MODEL_PRESET_VALUES = {preset["value"] for preset in OPENAI_MODEL_PRESETS}


def completion_token_parameter(model: str | None) -> str:
    """Return the Chat Completions output-limit parameter supported by a model family."""

    normalized = (model or "").strip().lower()
    return "max_completion_tokens" if normalized.startswith("gpt-5") else "max_tokens"


def is_openai_model_preset(model: str | None) -> bool:
    return bool((model or "").strip()) and (model or "").strip() in OPENAI_MODEL_PRESET_VALUES


def resolve_openai_runtime_model(model: str | None, *, fallback: str | None = None) -> str:
    normalized = (model or "").strip()
    if normalized:
        return normalized
    return fallback or LEGACY_OPENAI_MODEL_FALLBACK


def resolve_openai_model_choice(choice: str | None, custom_model: str | None = None, fallback: str | None = None) -> str:
    normalized_choice = (choice or "").strip()
    normalized_custom = (custom_model or "").strip()
    if normalized_choice == "custom":
        return normalized_custom or (fallback or DEFAULT_OPENAI_MODEL)
    if normalized_choice in OPENAI_MODEL_PRESET_VALUES:
        return normalized_choice
    if normalized_custom:
        return normalized_custom
    if normalized_choice:
        return normalized_choice
    return (fallback or DEFAULT_OPENAI_MODEL)


def openai_model_label(model: str | None) -> str:
    normalized = (model or "").strip()
    for preset in OPENAI_MODEL_PRESETS:
        if preset["value"] == normalized:
            return preset["label"]
    return "Personalizado" if normalized else "GPT-5.6 Luna"


def openai_model_description(model: str | None) -> str:
    normalized = (model or "").strip()
    for preset in OPENAI_MODEL_PRESETS:
        if preset["value"] == normalized:
            return preset["description"]
    return "Modelo personalizado definido por el tenant."


def openai_model_option_payload() -> list[dict[str, Any]]:
    return [dict(preset) for preset in OPENAI_MODEL_PRESETS]
