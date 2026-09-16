from __future__ import annotations

import ipaddress

from fastapi import Request

from app.core.config import get_settings


def _trusted_networks() -> list[ipaddress._BaseNetwork]:  # type: ignore[attr-defined]
    networks: list[ipaddress._BaseNetwork] = []  # type: ignore[attr-defined]
    for value in get_settings().trusted_proxy_ips:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return networks


def _is_trusted(value: str, networks: list[ipaddress._BaseNetwork]) -> bool:  # type: ignore[attr-defined]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in networks)


def resolve_client_ip(request: Request) -> str:
    """Use forwarded client IP only when the immediate peer is trusted."""
    peer = request.client.host if request.client else "unknown"
    networks = _trusted_networks()
    if not networks or not _is_trusted(peer, networks):
        return peer

    forwarded = [item.strip() for item in (request.headers.get("x-forwarded-for") or "").split(",") if item.strip()]
    chain = [*forwarded, peer]
    for candidate in reversed(chain):
        if not _is_trusted(candidate, networks):
            return candidate
    return peer
