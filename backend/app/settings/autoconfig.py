"""Automatic mail-server discovery and credential verification.

The account wizard deliberately keeps discovery separate from persistence.  A
password is used only for the probes in this module and is never returned or
stored here.  The existing email settings form remains the single place where
an administrator can persist the verified IMAP configuration.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import email.utils
import imaplib
import ipaddress
import poplib
import re
import smtplib
import socket
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

try:
    import dns.resolver as dns_resolver
    import dns.reversename as dns_reversename
except ImportError:  # pragma: no cover - exercised only in minimal local installs
    dns_resolver = None
    dns_reversename = None


AUTOCONFIG_TIMEOUT_SECONDS = 4.5
MAX_AUTOCONFIG_BYTES = 512 * 1024
MAX_AUTOCONFIG_REDIRECTS = 2
MAX_INCOMING_ATTEMPTS = 8
MAX_OUTGOING_ATTEMPTS = 6
MAX_PROBE_WORKERS = 4
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")


@dataclass(frozen=True)
class MailEndpoint:
    protocol: str
    host: str
    port: int
    security: str
    username: str
    folder: str = "INBOX"
    source: str = "common"

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "host": self.host,
            "port": self.port,
            "security": self.security,
            "username": self.username,
            "folder": self.folder,
            "source": self.source,
        }


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    label: str
    incoming: tuple[MailEndpoint, ...]
    outgoing: tuple[MailEndpoint, ...]


def _endpoint(protocol: str, host: str, port: int, security: str, source: str) -> MailEndpoint:
    return MailEndpoint(protocol, host, port, security, "", source=source)


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "gmail": ProviderPreset(
        "gmail",
        "Gmail",
        (
            _endpoint("imap", "imap.gmail.com", 993, "ssl_tls", "known_provider"),
            _endpoint("imap", "imap.gmail.com", 143, "starttls", "known_provider"),
            _endpoint("pop3", "pop.gmail.com", 995, "ssl_tls", "known_provider"),
            _endpoint("pop3", "pop.gmail.com", 110, "starttls", "known_provider"),
        ),
        (
            _endpoint("smtp", "smtp.gmail.com", 465, "ssl_tls", "known_provider"),
            _endpoint("smtp", "smtp.gmail.com", 587, "starttls", "known_provider"),
        ),
    ),
    "microsoft365": ProviderPreset(
        "microsoft365",
        "Microsoft 365 / Outlook",
        (
            _endpoint("imap", "outlook.office365.com", 993, "ssl_tls", "known_provider"),
            _endpoint("imap", "imap-mail.outlook.com", 993, "ssl_tls", "known_provider"),
            _endpoint("pop3", "outlook.office365.com", 995, "ssl_tls", "known_provider"),
        ),
        (
            _endpoint("smtp", "smtp.office365.com", 587, "starttls", "known_provider"),
            _endpoint("smtp", "smtp-mail.outlook.com", 587, "starttls", "known_provider"),
        ),
    ),
    "yahoo": ProviderPreset(
        "yahoo",
        "Yahoo",
        (
            _endpoint("imap", "imap.mail.yahoo.com", 993, "ssl_tls", "known_provider"),
            _endpoint("pop3", "pop.mail.yahoo.com", 995, "ssl_tls", "known_provider"),
        ),
        (_endpoint("smtp", "smtp.mail.yahoo.com", 465, "ssl_tls", "known_provider"),),
    ),
    "zoho": ProviderPreset(
        "zoho",
        "Zoho Mail",
        (
            _endpoint("imap", "imap.zoho.com", 993, "ssl_tls", "known_provider"),
            _endpoint("pop3", "pop.zoho.com", 995, "ssl_tls", "known_provider"),
        ),
        (_endpoint("smtp", "smtp.zoho.com", 465, "ssl_tls", "known_provider"),),
    ),
    "gmx": ProviderPreset(
        "gmx",
        "GMX",
        (
            _endpoint("imap", "imap.gmx.com", 993, "ssl_tls", "known_provider"),
            _endpoint("pop3", "pop.gmx.com", 995, "ssl_tls", "known_provider"),
        ),
        (_endpoint("smtp", "mail.gmx.com", 465, "ssl_tls", "known_provider"),),
    ),
    "ionos": ProviderPreset(
        "ionos",
        "IONOS",
        (
            _endpoint("imap", "imap.ionos.com", 993, "ssl_tls", "known_provider"),
            _endpoint("pop3", "pop.ionos.com", 995, "ssl_tls", "known_provider"),
        ),
        (_endpoint("smtp", "smtp.ionos.com", 465, "ssl_tls", "known_provider"),),
    ),
    "dinahosting": ProviderPreset(
        "dinahosting",
        "DinaHosting",
        (_endpoint("imap", "correoseguro.dinaserver.com", 993, "ssl_tls", "mx_provider"), _endpoint("pop3", "correoseguro.dinaserver.com", 995, "ssl_tls", "mx_provider")),
        (_endpoint("smtp", "correoseguro.dinaserver.com", 465, "ssl_tls", "mx_provider"),),
    ),
    "ovh": ProviderPreset(
        "ovh",
        "OVH",
        (_endpoint("imap", "ssl0.ovh.net", 993, "ssl_tls", "hosting_provider"), _endpoint("pop3", "ssl0.ovh.net", 995, "ssl_tls", "hosting_provider")),
        (_endpoint("smtp", "ssl0.ovh.net", 465, "ssl_tls", "hosting_provider"),),
    ),
    "hostinger": ProviderPreset(
        "hostinger",
        "Hostinger",
        (_endpoint("imap", "imap.hostinger.com", 993, "ssl_tls", "hosting_provider"), _endpoint("pop3", "pop.hostinger.com", 995, "ssl_tls", "hosting_provider")),
        (_endpoint("smtp", "smtp.hostinger.com", 465, "ssl_tls", "hosting_provider"),),
    ),
    "titan": ProviderPreset(
        "titan",
        "Titan Email",
        (_endpoint("imap", "imap.titan.email", 993, "ssl_tls", "hosting_provider"), _endpoint("pop3", "pop.titan.email", 995, "ssl_tls", "hosting_provider")),
        (_endpoint("smtp", "smtp.titan.email", 465, "ssl_tls", "hosting_provider"),),
    ),
}


PROVIDER_DOMAIN_HINTS = {
    "gmail.com": "gmail",
    "googlemail.com": "gmail",
    "outlook.com": "microsoft365",
    "hotmail.com": "microsoft365",
    "live.com": "microsoft365",
    "msn.com": "microsoft365",
    "yahoo.com": "yahoo",
    "yahoo.es": "yahoo",
    "yahoo.fr": "yahoo",
    "zoho.com": "zoho",
    "zoho.eu": "zoho",
    "gmx.com": "gmx",
    "gmx.es": "gmx",
    "ionos.com": "ionos",
    "ionos.es": "ionos",
    "1and1.com": "ionos",
    "1and1.es": "ionos",
}


IONOS_MX_RE = re.compile(r"(?:^|\.)(?:ionos|1and1)\.[a-z0-9-]+(?:\.[a-z0-9-]+)?$", re.IGNORECASE)
MX_PROVIDER_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:google|googlemail)\.com$", re.IGNORECASE), "gmail"),
    (re.compile(r"(?:outlook|office365|protection\.outlook)\.com$", re.IGNORECASE), "microsoft365"),
    (re.compile(r"yahoodns\.net$", re.IGNORECASE), "yahoo"),
    (re.compile(r"zoho\.(?:com|eu)$", re.IGNORECASE), "zoho"),
    (IONOS_MX_RE, "ionos"),
    (re.compile(r"(?:dinahosting|dinaserver|correoseguro)\.", re.IGNORECASE), "dinahosting"),
)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def normalize_email(value: str) -> str:
    """Return a normalized address or raise ValueError for malformed input."""

    candidate = (value or "").strip().lower()
    parsed = email.utils.parseaddr(candidate)[1]
    if parsed != candidate or not EMAIL_RE.fullmatch(candidate):
        raise ValueError("Introduce una dirección de correo válida.")
    local, domain = candidate.rsplit("@", 1)
    if len(candidate) > 254 or len(local) > 64 or len(domain) > 253:
        raise ValueError("La dirección de correo no es válida.")
    if any(label.startswith("-") or label.endswith("-") for label in domain.split(".")):
        raise ValueError("La dirección de correo no es válida.")
    return candidate


def _safe_host(host: str) -> bool:
    host = (host or "").strip().rstrip(".")
    return bool(host and len(host) <= 253 and HOST_RE.fullmatch(host) and not host.startswith("."))


def _public_ip(ip_value: str) -> bool:
    try:
        address = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _host_is_public(host: str) -> bool:
    """Reject local/private destinations before any discovery or probe."""

    if not _safe_host(host) or host.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            if info[4]
        }
    except (OSError, socket.gaierror):
        return False
    return bool(addresses) and all(_public_ip(address) for address in addresses)


def _replace_xml_token(value: str, email_address: str) -> str:
    local, domain = email_address.rsplit("@", 1)
    return (
        (value or "")
        .replace("%EMAILADDRESS%", email_address)
        .replace("%EMAILDOMAIN%", domain)
        .replace("%EMAILLOCALPART%", local)
    )


def _xml_text(parent: ET.Element, name: str, default: str = "") -> str:
    child = parent.find(name)
    return (child.text or "").strip() if child is not None and child.text else default


def _socket_security(socket_type: str) -> str:
    normalized = (socket_type or "").strip().lower().replace("-", "")
    if normalized in {"ssl", "ssl/tls", "tls", "alwaysssl", "ssl_tls"}:
        return "ssl_tls"
    if normalized in {"starttls", "tlsifavailable", "starttlsrequired"}:
        return "starttls"
    return "none"


def _parse_autoconfig_xml(payload: bytes, email_address: str) -> tuple[str, str, list[MailEndpoint], list[MailEndpoint]] | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    provider = root.find(".//emailProvider")
    if provider is None:
        return None
    provider_name = _xml_text(provider, "displayName") or _xml_text(provider, "incomingServer/hostname")
    provider_id = _xml_text(provider, "domain") or (provider.attrib.get("id") or "")
    incoming: list[MailEndpoint] = []
    outgoing: list[MailEndpoint] = []
    for server in provider.findall("incomingServer"):
        protocol = (server.attrib.get("type") or _xml_text(server, "type")).lower()
        if protocol not in {"imap", "pop3"}:
            continue
        host = _replace_xml_token(_xml_text(server, "hostname"), email_address).lower().rstrip(".")
        try:
            port = int(_xml_text(server, "port"))
        except ValueError:
            continue
        username = _replace_xml_token(_xml_text(server, "username", email_address), email_address)
        if _safe_host(host) and 1 <= port <= 65535:
            incoming.append(MailEndpoint(protocol, host, port, _socket_security(_xml_text(server, "socketType")), username, source="published_config"))
    for server in provider.findall("outgoingServer"):
        if (server.attrib.get("type") or _xml_text(server, "type")).lower() != "smtp":
            continue
        host = _replace_xml_token(_xml_text(server, "hostname"), email_address).lower().rstrip(".")
        try:
            port = int(_xml_text(server, "port"))
        except ValueError:
            continue
        username = _replace_xml_token(_xml_text(server, "username", email_address), email_address)
        if _safe_host(host) and 1 <= port <= 65535:
            outgoing.append(MailEndpoint("smtp", host, port, _socket_security(_xml_text(server, "socketType")), username, source="published_config"))
    if not incoming and not outgoing:
        return None
    return provider_id or provider_name, provider_name, incoming, outgoing


def _fetch_autoconfig(url: str, email_address: str, redirect_count: int = 0) -> tuple[str, str, list[MailEndpoint], list[MailEndpoint]] | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or not _host_is_public(parsed.hostname):
        return None
    request = Request(url, headers={"Accept": "application/xml,text/xml,text/plain;q=0.9", "User-Agent": "Anchi-Mail-Autoconfig/1.0"})
    opener = build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=AUTOCONFIG_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return None
            payload = response.read(MAX_AUTOCONFIG_BYTES + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400 and redirect_count < MAX_AUTOCONFIG_REDIRECTS:
            location = exc.headers.get("Location")
            if location:
                return _fetch_autoconfig(urljoin(url, location), email_address, redirect_count + 1)
        return None
    except (URLError, OSError, TimeoutError, ssl.SSLError):
        return None
    if len(payload) > MAX_AUTOCONFIG_BYTES:
        return None
    return _parse_autoconfig_xml(payload, email_address)


def _published_configuration(email_address: str, domain: str) -> tuple[str, str, list[MailEndpoint], list[MailEndpoint]] | None:
    encoded_email = quote(email_address, safe="@")
    urls = (
        f"https://autoconfig.{domain}/mail/config-v1.1.xml?emailaddress={encoded_email}",
        f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml?emailaddress={encoded_email}",
        f"https://autoconfig.thunderbird.net/v1.1/{domain}",
    )
    results: dict[int, tuple[str, str, list[MailEndpoint], list[MailEndpoint]] | None] = {}
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futures = {pool.submit(_fetch_autoconfig, url, email_address): index for index, url in enumerate(urls)}
        for future in as_completed(futures):
            try:
                results[futures[future]] = future.result()
            except Exception:  # noqa: BLE001
                results[futures[future]] = None
    for index in range(len(urls)):
        result = results.get(index)
        # An SMTP-only XML document cannot configure Anchi's inbox. Keep
        # looking so MX-derived ISPDB discovery can still provide IMAP.
        if result and result[2]:
            return result
    return None


_COMMON_SECOND_LEVEL_TLDS = {
    "ac.uk",
    "co.uk",
    "gov.uk",
    "ltd.uk",
    "me.uk",
    "net.uk",
    "org.uk",
    "plc.uk",
    "sch.uk",
    "com.au",
    "com.br",
    "com.cn",
    "com.hk",
    "com.mx",
    "com.sg",
    "com.tr",
    "com.tw",
    "co.nz",
    "co.za",
    "com.ar",
    "com.co",
    "com.pe",
}


def _registrable_like_domain(host: str) -> str | None:
    """Return a conservative provider-domain approximation without DNS writes.

    The reference engine uses a public-suffix database. Anchi intentionally
    keeps discovery lightweight, so this covers common multi-label suffixes
    and falls back to the final two labels. It is only used as a bounded input
    to Thunderbird ISPDB and provider-host candidates.
    """

    labels = [label for label in (host or "").rstrip(".").lower().split(".") if label]
    if len(labels) < 2:
        return None
    suffix = ".".join(labels[-2:])
    if suffix in _COMMON_SECOND_LEVEL_TLDS and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix


def _mx_provider_domains(exchanges: list[str], domain: str) -> list[str]:
    """Derive mail-provider domains from the domain's MX exchanges.

    MX records usually point at a provider hostname (for example
    ``mx.customer-mail.example`` or ``mail.provider.example``), while IMAP
    and SMTP live on sibling hosts. Querying the registrable provider domain
    lets us use the provider's published ISPDB profile and its own host
    naming, without trying an unbounded catalogue of generic servers.
    """

    result: list[str] = []
    domain = domain.rstrip(".").lower()
    for exchange in exchanges:
        candidate = _registrable_like_domain(exchange)
        if not candidate or candidate == domain or candidate in result:
            continue
        result.append(candidate)
        if len(result) >= 3:
            break
    return result


def _mx_provider_candidates(provider_domains: list[str]) -> tuple[list[MailEndpoint], list[MailEndpoint]]:
    """Build bounded candidates from MX-derived provider hostnames only."""

    incoming: list[MailEndpoint] = []
    outgoing: list[MailEndpoint] = []
    for provider_domain in provider_domains:
        for host, protocol, port in (
            (f"imap.{provider_domain}", "imap", 993),
            (f"imap.{provider_domain}", "imap", 143),
            (f"pop.{provider_domain}", "pop3", 995),
            (f"pop.{provider_domain}", "pop3", 110),
            (f"pop3.{provider_domain}", "pop3", 995),
            (f"mail.{provider_domain}", "imap", 993),
            (f"mail.{provider_domain}", "imap", 143),
            (f"mail.{provider_domain}", "pop3", 995),
            (f"mail.{provider_domain}", "pop3", 110),
        ):
            security = "ssl_tls" if port in {993, 995} else "starttls"
            incoming.append(_endpoint(protocol, host, port, security, "mx_provider_pattern"))
        for host, port, security in (
            (f"smtp.{provider_domain}", 465, "ssl_tls"),
            (f"smtp.{provider_domain}", 587, "starttls"),
            (f"mail.{provider_domain}", 465, "ssl_tls"),
            (f"mail.{provider_domain}", 587, "starttls"),
        ):
            outgoing.append(_endpoint("smtp", host, port, security, "mx_provider_pattern"))
    return incoming, outgoing


def _published_configuration_from_mx_provider(
    email_address: str,
    provider_domains: list[str],
) -> tuple[str, str, list[MailEndpoint], list[MailEndpoint]] | None:
    """Read Thunderbird ISPDB profiles for providers discovered via MX."""

    if not provider_domains:
        return None
    urls = [
        f"https://autoconfig.thunderbird.net/v1.1/{quote(provider_domain, safe='')}"
        for provider_domain in provider_domains
    ]
    results: dict[int, tuple[str, str, list[MailEndpoint], list[MailEndpoint]] | None] = {}
    with ThreadPoolExecutor(max_workers=min(3, len(urls))) as pool:
        futures = {pool.submit(_fetch_autoconfig, url, email_address): index for index, url in enumerate(urls)}
        for future in as_completed(futures):
            try:
                results[futures[future]] = future.result()
            except Exception:  # noqa: BLE001
                results[futures[future]] = None
    for index in range(len(urls)):
        result = results.get(index)
        if result and result[2]:
            return result
    return None


def _common_candidates(email_address: str, domain: str) -> tuple[list[MailEndpoint], list[MailEndpoint]]:
    candidates = [
        _endpoint("imap", f"imap.{domain}", 993, "ssl_tls", "common_pattern"),
        _endpoint("imap", f"imap.{domain}", 143, "starttls", "common_pattern"),
        _endpoint("imap", f"mail.{domain}", 993, "ssl_tls", "common_pattern"),
        _endpoint("imap", f"mail.{domain}", 143, "starttls", "common_pattern"),
        _endpoint("pop3", f"mail.{domain}", 995, "ssl_tls", "common_pattern"),
        _endpoint("pop3", f"mail.{domain}", 110, "starttls", "common_pattern"),
        _endpoint("pop3", f"pop.{domain}", 995, "ssl_tls", "common_pattern"),
        _endpoint("pop3", f"pop.{domain}", 110, "starttls", "common_pattern"),
        _endpoint("pop3", f"pop3.{domain}", 995, "ssl_tls", "common_pattern"),
        _endpoint("pop3", f"pop3.{domain}", 110, "starttls", "common_pattern"),
        _endpoint("smtp", f"smtp.{domain}", 465, "ssl_tls", "common_pattern"),
        _endpoint("smtp", f"smtp.{domain}", 587, "starttls", "common_pattern"),
        _endpoint("smtp", f"mail.{domain}", 587, "starttls", "common_pattern"),
    ]
    public = [candidate for candidate in candidates if _host_is_public_or_unresolved(candidate.host)]
    return [candidate for candidate in public if candidate.protocol in {"imap", "pop3"}], [candidate for candidate in public if candidate.protocol == "smtp"]


def _dns_candidates(domain: str) -> tuple[list[MailEndpoint], list[MailEndpoint], list[str], list[str]]:
    """Read standard SRV/MX hints when dnspython is available in the runtime."""

    if dns_resolver is None:
        return [], [], [], []
    try:
        resolver = dns_resolver.Resolver()
        resolver.timeout = 1.25
        resolver.lifetime = 2.5
    except Exception:  # noqa: BLE001
        return [], [], [], []

    incoming: list[MailEndpoint] = []
    outgoing: list[MailEndpoint] = []
    records = (
        ("_imaps._tcp", "imap", "ssl_tls", incoming),
        ("_imap._tcp", "imap", "starttls", incoming),
        ("_pop3s._tcp", "pop3", "ssl_tls", incoming),
        ("_pop3._tcp", "pop3", "starttls", incoming),
        ("_submissions._tcp", "smtp", "ssl_tls", outgoing),
        ("_submission._tcp", "smtp", "starttls", outgoing),
        ("_smtp._tcp", "smtp", "none", outgoing),
    )
    for service, protocol, security, target_list in records:
        try:
            answers = resolver.resolve(f"{service}.{domain}", "SRV")
        except Exception:  # noqa: BLE001
            continue
        ordered = sorted(answers, key=lambda answer: (int(answer.priority), -int(answer.weight)))
        for answer in ordered:
            host = str(answer.target).rstrip(".").lower()
            try:
                port = int(answer.port)
            except (AttributeError, TypeError, ValueError):
                continue
            if _safe_host(host) and 1 <= port <= 65535:
                target_list.append(MailEndpoint(protocol, host, port, security, "", source="dns_srv"))

    mx_exchanges: list[str] = []
    try:
        mx_answers = resolver.resolve(domain, "MX")
    except Exception:  # noqa: BLE001
        mx_answers = []
    for answer in sorted(mx_answers, key=lambda item: int(item.preference)):
        host = str(answer.exchange).rstrip(".").lower()
        if not _safe_host(host):
            continue
        mx_exchanges.append(host)
        # MX identifies the provider's SMTP delivery endpoint. It is not an
        # IMAP/POP3 endpoint, so keep it only as a discovery fingerprint.
    fingerprints = list(mx_exchanges)
    try:
        ns_answers = resolver.resolve(domain, "NS")
        fingerprints.extend(str(answer).rstrip(".").lower() for answer in ns_answers)
    except Exception:  # noqa: BLE001
        pass
    # A hosted provider may hide behind a custom MX hostname. Its reverse DNS
    # often exposes the actual mail platform (for example dinaserver.com),
    # which is a stronger signal than the domain's nameservers. Keep this
    # bounded to the primary MX and at most two resolved addresses.
    if dns_reversename is not None and mx_exchanges:
        for record_type in ("A", "AAAA"):
            try:
                addresses = list(resolver.resolve(mx_exchanges[0], record_type))[:2]
            except Exception:  # noqa: BLE001
                continue
            for address in addresses:
                try:
                    reverse_name = dns_reversename.from_address(str(address))
                    ptr_answers = resolver.resolve(reverse_name, "PTR")
                    fingerprints.extend(str(answer).rstrip(".").lower() for answer in ptr_answers)
                except Exception:  # noqa: BLE001
                    continue
    return incoming, outgoing, mx_exchanges, fingerprints


def _preset_from_mx(exchanges: list[str]) -> ProviderPreset | None:
    for exchange in exchanges:
        for pattern, provider_key in MX_PROVIDER_HINTS:
            if pattern.search(exchange):
                return PROVIDER_PRESETS.get(provider_key)
    return None


def _provider_from_fingerprints(fingerprints: list[str]) -> ProviderPreset | None:
    """Identify hosting infrastructure from MX reverse DNS/NS fingerprints."""

    fingerprint_text = " ".join(fingerprints).lower()
    if re.search(r"(?:dinahosting|dinaserver|correoseguro)\.", fingerprint_text):
        return PROVIDER_PRESETS["dinahosting"]
    if re.search(r"(?:\bovh\b|ovh\.net|mail-out\.ovh\.net)", fingerprint_text):
        return PROVIDER_PRESETS["ovh"]
    if re.search(r"(?:ionos|1and1)\.", fingerprint_text):
        return PROVIDER_PRESETS["ionos"]
    if "hostinger" in fingerprint_text:
        return PROVIDER_PRESETS["hostinger"]
    if "titan.email" in fingerprint_text:
        return PROVIDER_PRESETS["titan"]
    return None


def _hosting_candidates(domain: str, fingerprints: list[str]) -> tuple[list[MailEndpoint], list[MailEndpoint]]:
    """Add provider-specific hosted-mail fallbacks discovered from MX/NS names."""

    fingerprint_text = " ".join(fingerprints).lower()
    incoming_hosts: list[tuple[str, str]] = []
    outgoing_hosts: list[str] = []
    domain_slug = domain.replace(".", "-")
    if re.search(r"(?:dinahosting|dinaserver|correoseguro)\.", fingerprint_text):
        incoming_hosts.extend(((f"{domain_slug}.correoseguro.dinaserver.com", "imap"), (f"{domain_slug}.correoseguro.dinaserver.com", "pop3"), ("correoseguro.dinaserver.com", "imap"), ("correoseguro.dinaserver.com", "pop3")))
        outgoing_hosts.extend((f"{domain_slug}.correoseguro.dinaserver.com", "correoseguro.dinaserver.com"))
    if re.search(r"(?:\bovh\b|ovh\.net|mx\d+\.mail-out\.ovh\.net)", fingerprint_text):
        incoming_hosts.extend((("ssl0.ovh.net", "imap"), ("ssl0.ovh.net", "pop3")))
        outgoing_hosts.append("ssl0.ovh.net")
    if re.search(r"(?:ionos|1and1)\.", fingerprint_text):
        incoming_hosts.extend((("imap.ionos.com", "imap"), ("pop.ionos.com", "pop3"), ("imap.ionos.es", "imap"), ("pop.ionos.es", "pop3")))
        outgoing_hosts.extend(("smtp.ionos.com", "smtp.ionos.es"))
    if "hostinger" in fingerprint_text:
        incoming_hosts.extend((("imap.hostinger.com", "imap"), ("pop.hostinger.com", "pop3")))
        outgoing_hosts.append("smtp.hostinger.com")
    if "titan.email" in fingerprint_text:
        incoming_hosts.extend((("imap.titan.email", "imap"), ("pop.titan.email", "pop3")))
        outgoing_hosts.append("smtp.titan.email")

    incoming = []
    for host, protocol in incoming_hosts:
        incoming.append(_endpoint(protocol, host, 993 if protocol == "imap" else 995, "ssl_tls", "hosting_provider"))
    outgoing = [_endpoint("smtp", host, 465, "ssl_tls", "hosting_provider") for host in outgoing_hosts]
    return incoming, outgoing


def _unique_endpoints(endpoints: list[MailEndpoint]) -> list[MailEndpoint]:
    unique: list[MailEndpoint] = []
    seen: set[tuple[str, str, int, str]] = set()
    for endpoint in endpoints:
        key = (endpoint.protocol, endpoint.host, endpoint.port, endpoint.security)
        if key not in seen:
            seen.add(key)
            unique.append(endpoint)
    return unique


def _public_endpoints(endpoints: list[MailEndpoint]) -> list[MailEndpoint]:
    """Validate every discovered mail host before opening a socket to it."""

    result: list[MailEndpoint] = []
    host_status: dict[str, bool] = {}
    for endpoint in endpoints:
        if endpoint.host not in host_status:
            host_status[endpoint.host] = _host_is_public_or_unresolved(endpoint.host)
        if host_status[endpoint.host]:
            result.append(endpoint)
    return result


def _host_is_public_or_unresolved(host: str) -> bool:
    """Allow unresolved public hostnames while rejecting resolved private hosts."""

    if not _safe_host(host) or host.lower().rstrip(".") in {"localhost", "localhost.localdomain"} or host.lower().endswith(".local"):
        return False
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            if info[4]
        }
    except (OSError, socket.gaierror):
        return True
    return bool(addresses) and all(_public_ip(address) for address in addresses)


def _username_variants(email_address: str, configured: str) -> tuple[str, ...]:
    local = email_address.split("@", 1)[0]
    values = [configured.strip() if configured else "", email_address, local]
    return tuple(dict.fromkeys(value for value in values if value))


def _probe_specs(endpoints: list[MailEndpoint], email_address: str, limit: int) -> list[tuple[MailEndpoint, str]]:
    """Interleave protocols and username variants like the reference engine."""

    grouped: dict[tuple[str, str, int, str, str], tuple[MailEndpoint, list[str]]] = {}
    for endpoint in _unique_endpoints(endpoints):
        key = (endpoint.protocol, endpoint.host, endpoint.port, endpoint.security, endpoint.folder)
        grouped[key] = (endpoint, list(_username_variants(email_address, endpoint.username)))
    pop3_groups = [group for group in grouped.values() if group[0].protocol == "pop3"]
    imap_groups = [group for group in grouped.values() if group[0].protocol == "imap"]
    interleaved: list[tuple[MailEndpoint, list[str]]] = []
    for index in range(max(len(pop3_groups), len(imap_groups))):
        if index < len(pop3_groups):
            interleaved.append(pop3_groups[index])
        if index < len(imap_groups):
            interleaved.append(imap_groups[index])
    specs: list[tuple[MailEndpoint, str]] = []
    for round_number in range(max((len(usernames) for _, usernames in interleaved), default=0)):
        for endpoint, usernames in interleaved:
            if round_number < len(usernames) and len(specs) < limit:
                specs.append((endpoint, usernames[round_number]))
    return specs


def _probe_imap(endpoint: MailEndpoint, username: str, password: str) -> bool:
    client = None
    try:
        if endpoint.security == "ssl_tls":
            client = imaplib.IMAP4_SSL(endpoint.host, endpoint.port, timeout=AUTOCONFIG_TIMEOUT_SECONDS)
        else:
            client = imaplib.IMAP4(endpoint.host, endpoint.port, timeout=AUTOCONFIG_TIMEOUT_SECONDS)
            if endpoint.security == "starttls":
                client.starttls()
        status, _ = client.login(username, password)
        return status.upper() == "OK"
    except (OSError, socket.timeout, socket.error, imaplib.IMAP4.error, ssl.SSLError):
        return False
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass


def _probe_pop3(endpoint: MailEndpoint, username: str, password: str) -> bool:
    client = None
    try:
        if endpoint.security == "ssl_tls":
            client = poplib.POP3_SSL(endpoint.host, endpoint.port, timeout=AUTOCONFIG_TIMEOUT_SECONDS)
        else:
            client = poplib.POP3(endpoint.host, endpoint.port, timeout=AUTOCONFIG_TIMEOUT_SECONDS)
            if endpoint.security == "starttls":
                client.stls()
        client.user(username)
        client.pass_(password)
        return True
    except (OSError, socket.timeout, socket.error, poplib.error_proto, ssl.SSLError):
        return False
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:  # noqa: BLE001
                pass


def _probe_smtp(endpoint: MailEndpoint, username: str, password: str) -> bool:
    client = None
    try:
        if endpoint.security == "ssl_tls":
            client = smtplib.SMTP_SSL(endpoint.host, endpoint.port, timeout=AUTOCONFIG_TIMEOUT_SECONDS)
        else:
            client = smtplib.SMTP(endpoint.host, endpoint.port, timeout=AUTOCONFIG_TIMEOUT_SECONDS)
            client.ehlo()
            if endpoint.security == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
        client.login(username, password)
        return True
    except (OSError, socket.timeout, socket.error, smtplib.SMTPException, ssl.SSLError):
        return False
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:  # noqa: BLE001
                pass


def _probe_incoming(endpoint: MailEndpoint, username: str, password: str) -> bool:
    if endpoint.protocol == "imap":
        return _probe_imap(endpoint, username, password)
    return _probe_pop3(endpoint, username, password)


def _provider_for_domain(domain: str) -> ProviderPreset | None:
    key = PROVIDER_DOMAIN_HINTS.get(domain)
    return PROVIDER_PRESETS.get(key) if key else None


def _provider_from_published(identifier: str, label: str) -> tuple[str, str]:
    text = f"{identifier} {label}".lower()
    for key, preset in PROVIDER_PRESETS.items():
        if key in text or preset.label.lower() in text:
            return key, preset.label
    return "imap", label or "Proveedor detectado"


def _verified_endpoints(
    endpoints: list[MailEndpoint],
    email_address: str,
    password: str,
    limit: int,
) -> tuple[list[MailEndpoint], int]:
    specs = _probe_specs(endpoints, email_address, limit)
    if not specs:
        return [], 0
    successful: set[int] = set()
    with ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(specs))) as pool:
        futures = {
            pool.submit(_probe_incoming, endpoint, username, password): index
            for index, (endpoint, username) in enumerate(specs)
        }
        for future in as_completed(futures):
            try:
                if future.result():
                    successful.add(futures[future])
            except Exception:  # noqa: BLE001
                continue
    verified: list[MailEndpoint] = []
    for index, (endpoint, username) in enumerate(specs):
        if index in successful and not any(item.host == endpoint.host and item.port == endpoint.port and item.protocol == endpoint.protocol for item in verified):
            verified.append(replace(endpoint, username=username))
    return verified, len(specs)


def _verified_smtp(
    endpoints: list[MailEndpoint],
    email_address: str,
    password: str,
    limit: int,
) -> tuple[list[MailEndpoint], int]:
    specs = _probe_specs(endpoints, email_address, limit)
    if not specs:
        return [], 0
    successful: set[int] = set()
    with ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(specs))) as pool:
        futures = {
            pool.submit(_probe_smtp, endpoint, username, password): index
            for index, (endpoint, username) in enumerate(specs)
        }
        for future in as_completed(futures):
            try:
                if future.result():
                    successful.add(futures[future])
            except Exception:  # noqa: BLE001
                continue
    verified: list[MailEndpoint] = []
    for index, (endpoint, username) in enumerate(specs):
        if index in successful and not any(item.host == endpoint.host and item.port == endpoint.port for item in verified):
            verified.append(replace(endpoint, username=username))
    return verified, len(specs)


def _safe_message(verified_imap: list[MailEndpoint], verified_pop3: list[MailEndpoint], discovered: bool) -> str:
    if verified_imap:
        return "Configuración IMAP encontrada y verificada."
    if verified_pop3:
        return "Se ha verificado POP3, pero Anchi necesita IMAP para sincronizar el correo."
    if discovered:
        return "Se ha encontrado una configuración, pero no se ha podido verificar con esas credenciales."
    return "No se ha encontrado una configuración verificable. Puedes introducirla manualmente."


def detect_email_configuration(email_address: str, password: str) -> dict[str, Any]:
    """Discover and verify incoming/outgoing mail settings for one account."""

    normalized_email = normalize_email(email_address)
    if not (password or "").strip():
        raise ValueError("Introduce la contraseña de la cuenta de correo.")
    domain = normalized_email.rsplit("@", 1)[1]
    preset = _provider_for_domain(domain)
    provider_key = preset.name if preset else "imap"
    provider_label = preset.label if preset else "Proveedor no identificado"
    incoming: list[MailEndpoint] = []
    outgoing: list[MailEndpoint] = []

    if preset:
        incoming.extend(preset.incoming)
        outgoing.extend(preset.outgoing)
    else:
        # DNS discovery comes first for custom domains: the MX target is the
        # only reliable clue about which hosted-mail provider owns the inbox.
        # It feeds both the provider's published ISPDB profile and its
        # provider-specific IMAP/SMTP hostname patterns.
        dns_incoming, dns_outgoing, mx_exchanges, fingerprints = _dns_candidates(domain)
        mx_provider_domains = _mx_provider_domains(mx_exchanges, domain)
        published = _published_configuration(normalized_email, domain)
        if not published:
            published = _published_configuration_from_mx_provider(normalized_email, mx_provider_domains)
        published_found = False
        if published:
            identifier, label, published_incoming, published_outgoing = published
            provider_key, provider_label = _provider_from_published(identifier, label)
            incoming.extend(published_incoming)
            outgoing.extend(published_outgoing)
            published_found = True
        mx_preset = _preset_from_mx(mx_exchanges)
        if mx_preset:
            incoming.extend(mx_preset.incoming)
            outgoing.extend(mx_preset.outgoing)
            if not published_found:
                provider_key, provider_label = mx_preset.name, mx_preset.label
        fingerprint_provider = _provider_from_fingerprints(fingerprints)
        if fingerprint_provider and not published_found and not mx_preset:
            provider_key, provider_label = fingerprint_provider.name, fingerprint_provider.label
        mx_incoming, mx_outgoing = _mx_provider_candidates(mx_provider_domains)
        incoming.extend(mx_incoming)
        outgoing.extend(mx_outgoing)
        hosting_incoming, hosting_outgoing = _hosting_candidates(domain, fingerprints)
        incoming.extend(hosting_incoming)
        outgoing.extend(hosting_outgoing)
        incoming.extend(dns_incoming)
        outgoing.extend(dns_outgoing)
        common_incoming, common_outgoing = _common_candidates(normalized_email, domain)
        incoming.extend(common_incoming)
        outgoing.extend(common_outgoing)
    incoming = _public_endpoints(_unique_endpoints(incoming))
    outgoing = _public_endpoints(_unique_endpoints(outgoing))

    verified_incoming, incoming_attempts = _verified_endpoints(incoming, normalized_email, password, MAX_INCOMING_ATTEMPTS)
    verified_imap = [endpoint for endpoint in verified_incoming if endpoint.protocol == "imap"]
    verified_pop3 = [endpoint for endpoint in verified_incoming if endpoint.protocol == "pop3"]
    verified_smtp, smtp_attempts = _verified_smtp(outgoing, normalized_email, password, MAX_OUTGOING_ATTEMPTS)
    discovered = bool(incoming)
    suggested_imap = next((endpoint for endpoint in incoming if endpoint.protocol == "imap"), None)
    suggested_pop3 = next((endpoint for endpoint in incoming if endpoint.protocol == "pop3"), None)
    discovery_sources = sorted({endpoint.source for endpoint in (*incoming, *outgoing)})
    return {
        "ok": True,
        "detected": bool(verified_incoming),
        "discovered": discovered,
        "verified": bool(verified_incoming),
        "email": normalized_email,
        "domain": domain,
        "provider": provider_key,
        "provider_label": provider_label,
        "message": _safe_message(verified_imap, verified_pop3, discovered),
        "can_use_in_anchi": bool(verified_imap),
        "incoming": verified_incoming[0].as_dict() if verified_incoming else None,
        "imap": verified_imap[0].as_dict() if verified_imap else None,
        "pop3": verified_pop3[0].as_dict() if verified_pop3 else None,
        "suggested_imap": suggested_imap.as_dict() if suggested_imap else None,
        "suggested_pop3": suggested_pop3.as_dict() if suggested_pop3 else None,
        "smtp": verified_smtp[0].as_dict() if verified_smtp else None,
        "attempts": {"incoming": incoming_attempts, "smtp": smtp_attempts},
        "discovery": {
            "sources": discovery_sources,
            "mx_provider_domains": _mx_provider_domains(mx_exchanges, domain) if not preset else [],
            "candidate_count": len(incoming) + len(outgoing),
        },
    }
