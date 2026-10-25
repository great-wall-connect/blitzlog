"""Local-LLM endpoint IP-safety guard.

Hard-coded CIDR rules that classify a resolved IP as:
    - always-blocked (loopback, link-local — covers IMDS)
    - opt-in only    (RFC1918, ULA, Tailscale CGNAT — requires an
                       explicit per-user `allow_private_cidrs=true` flag)
    - allowed        (public IPs are hard-rejected; everything else is OK)

The guard sits behind `bot_pool.get_local_llm_config` and is consulted on
every LLM endpoint lookup so a malicious or misconfigured SSM param can't
trick the bootstrap into shipping credentials to loopback or to an
attacker-controlled public endpoint.

Two validation modes, because "local endpoint" and "BYO cloud endpoint"
have opposite public/private postures:

  * `validate_local_endpoint` — a LOCAL LLM must be reachable only
    from a private network. Public IPs are hard-rejected: the whole
    point of the local-LLM feature is that the model never leaves
    the user's own network.

  * `validate_byo_endpoint` — a BYO (bring-your-own) provider
    endpoint is normally a PUBLIC API (`api.openai.com`), so public
    IPs are fine. Loopback and link-local stay hard-blocked (that is
    the IMDS / metadata-service path), and private ranges need an
    explicit opt-in — a self-hosted endpoint on a Tailnet or VPC.
"""

import ipaddress
import socket
from urllib.parse import urlparse

_LOCAL_LLM_ALWAYS_BLOCKED_V4 = [
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
]
_LOCAL_LLM_ALWAYS_BLOCKED_V6 = [
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fe80::/10"),
    ipaddress.IPv6Network("::ffff:127.0.0.0/104"),
    ipaddress.IPv6Network("::ffff:169.254.0.0/112"),
]
_LOCAL_LLM_OPTIN_V4 = [
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("100.64.0.0/10"),
]
_LOCAL_LLM_OPTIN_V6 = [
    ipaddress.IPv6Network("fc00::/7"),
]


def _ip_always_blocked(ip_obj: ipaddress._BaseAddress) -> bool:
    pools = (
        _LOCAL_LLM_ALWAYS_BLOCKED_V4
        if isinstance(ip_obj, ipaddress.IPv4Address)
        else _LOCAL_LLM_ALWAYS_BLOCKED_V6
    )
    return any(ip_obj in net for net in pools)


def _ip_is_opt_in_only(ip_obj: ipaddress._BaseAddress) -> bool:
    pools = (
        _LOCAL_LLM_OPTIN_V4
        if isinstance(ip_obj, ipaddress.IPv4Address)
        else _LOCAL_LLM_OPTIN_V6
    )
    return any(ip_obj in net for net in pools)


def _resolve_endpoint_ips(endpoint: str) -> list[str]:
    parsed = urlparse(endpoint)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"endpoint {endpoint!r} has no hostname")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip = sockaddr[0]
        if "%" in ip:
            ip = ip.split("%", 1)[0]
        ips.append(ip)
    return ips


def _parse_http_url(url: str) -> tuple[str, str]:
    """Validate the scheme/hostname of an endpoint URL.

    Returns `(hostname, reason)` — `reason` is "" when the URL is
    structurally fine, otherwise a human-readable explanation.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "", f"non-http(s) scheme {parsed.scheme!r}"
    if not parsed.hostname:
        return "", "no hostname"
    return parsed.hostname, ""


def validate_byo_endpoint(url: str, allow_private_cidrs: bool = False) -> tuple[bool, str]:
    """Validate a BYO (bring-your-own) provider endpoint URL.

    Returns `(ok, reason)`. `reason` is "" when `ok` is True.

    Rules:
      - Always blocked: loopback, link-local (covers the EC2/Aliyun
        metadata service).
      - Opt-in (requires `allow_private_cidrs`): RFC1918, ULA,
        Tailscale CGNAT — a self-hosted endpoint on a private
        network.
      - Public IPs are ALLOWED — a cloud provider's API endpoint
        normally resolves to one.

    Used by `bot_pool.get_opencode_profile` for the per-user
    `opencode_base_url` parameter (issue #165).
    """
    url = (url or "").strip()
    if not url:
        return False, "empty URL"

    hostname, reason = _parse_http_url(url)
    if reason:
        return False, reason

    try:
        resolved_ips = _resolve_endpoint_ips(url)
    except (OSError, ValueError) as e:
        # OSError covers socket.gaierror (the usual DNS failure) and
        # any other resolver-level error.
        return False, f"DNS resolution failed: {e}"

    if not resolved_ips:
        return False, "no resolved addresses"

    for ip_str in resolved_ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"cannot parse resolved IP {ip_str!r}"

        if _ip_always_blocked(ip_obj):
            return False, (
                f"resolves to always-blocked IP {ip_obj} (loopback or link-local)"
            )

        if _ip_is_opt_in_only(ip_obj) and not allow_private_cidrs:
            return False, (
                f"resolves to private IP {ip_obj} but private endpoints are not "
                f"enabled"
            )

    return True, ""


__all__ = [
    "_ip_always_blocked",
    "_ip_is_opt_in_only",
    "_resolve_endpoint_ips",
    "validate_byo_endpoint",
]
