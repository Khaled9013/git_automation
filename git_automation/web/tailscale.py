"""Tailscale (tailnet) detection: the one network trusted beyond loopback.

Why this exists
---------------
The app's baseline trust model is loopback-only (see
:mod:`git_automation.web.__main__` and :mod:`git_automation.web.security`). A
tailnet is the single other network that model extends to: a Tailscale tailnet
is a private WireGuard mesh in which every peer is a device the operator
explicitly enrolled, so "reachable over Tailscale" means "one of the
operator's own, authenticated devices" -- unlike a LAN, where any nearby
machine can connect. This module is the single source of truth for:

* which literal IPs are Tailscale addresses (:func:`is_tailscale_ip`) --
  Tailscale assigns IPv4 from the CGNAT block ``100.64.0.0/10`` and IPv6 from
  ``fd7a:115c:a1e0::/48``;
* this machine's own tailnet identity (:func:`self_identity`): its Tailscale
  IPs (used as extra bind hosts) and its MagicDNS names (accepted by the
  Host/Origin guards so browsers can use ``http://<machine>.<tailnet>.ts.net``
  or the short ``http://<machine>``).

Detection shells out to the ``tailscale`` CLI once per process and caches the
result. Any failure -- no CLI installed, daemon stopped, unexpected JSON --
degrades to ``None``, and every caller then behaves exactly as if this module
did not exist (loopback-only). Parsed IPs are additionally filtered through
:func:`is_tailscale_ip`, so no CLI output can ever smuggle a non-Tailscale
address into the trusted set.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import ip_address, ip_network

# Tailscale's address allocations: IPv4 from the CGNAT range, IPv6 from a
# fixed ULA /48. See https://tailscale.com/kb/1015/100.x-addresses.
TAILSCALE_IPV4_NETWORK = ip_network("100.64.0.0/10")
TAILSCALE_IPV6_NETWORK = ip_network("fd7a:115c:a1e0::/48")

# Keep CLI detection snappy: the daemon answers instantly when running, and a
# hung/absent daemon must not stall server boot or a first guarded request.
_STATUS_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class TailnetIdentity:
    """This machine's own addresses and names on its tailnet.

    ``ips`` are literal Tailscale IPs (already validated to be in the
    Tailscale ranges). ``dns_names`` are the lowercase MagicDNS names browsers
    may present in ``Host``/``Origin``: the full ``machine.tailnet.ts.net``
    plus the short ``machine`` label (empty when MagicDNS is off).
    """

    ips: tuple[str, ...]
    dns_names: tuple[str, ...]


def is_tailscale_ip(host: str) -> bool:
    """Return True when ``host`` is a literal IP in a Tailscale range.

    Purely syntactic (no CLI needed): accepts ``100.64.0.0/10`` and
    ``fd7a:115c:a1e0::/48`` literals, normalizing brackets/case like the
    loopback check. Hostnames are never Tailscale IPs -- they could resolve
    anywhere, so callers must fail closed on them.
    """
    normalized = host.strip().strip("[]").lower()
    try:
        address = ip_address(normalized)
    except ValueError:
        return False
    return address in TAILSCALE_IPV4_NETWORK or address in TAILSCALE_IPV6_NETWORK


def _parse_status(raw: str) -> TailnetIdentity | None:
    """Parse ``tailscale status --json`` output into a :class:`TailnetIdentity`.

    Returns ``None`` unless the backend is actually ``Running`` with at least
    one valid Tailscale IP -- a stopped daemon's addresses are not bound to
    any interface and must not be trusted or bound. Pure for unit testing.
    """
    try:
        status = json.loads(raw)
        backend_state = status["BackendState"]
        raw_ips = status.get("TailscaleIPs") or status.get("Self", {}).get("TailscaleIPs")
        dns_name = str(status.get("Self", {}).get("DNSName", ""))
    except (ValueError, TypeError, KeyError):
        return None

    if backend_state != "Running" or not isinstance(raw_ips, list):
        return None

    ips = tuple(ip for ip in raw_ips if isinstance(ip, str) and is_tailscale_ip(ip))
    if not ips:
        return None

    dns_names: tuple[str, ...] = ()
    full_name = dns_name.strip().rstrip(".").lower()
    if full_name:
        dns_names = (full_name, full_name.split(".", 1)[0])

    return TailnetIdentity(ips=ips, dns_names=dns_names)


@lru_cache(maxsize=1)
def self_identity() -> TailnetIdentity | None:
    """Return this machine's tailnet identity, or ``None`` when unavailable.

    Cached for the process lifetime: the guards consult it per request, and a
    tailnet identity only changes on `tailscale up/down` (restart the app to
    pick that up, exactly like a `GITAUTO_*` env change).
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_STATUS_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_status(result.stdout)
