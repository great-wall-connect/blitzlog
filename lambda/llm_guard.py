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
