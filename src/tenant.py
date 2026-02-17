"""Per-request tenant resolution for multi-tenant path routing."""

from __future__ import annotations

from contextvars import ContextVar

# Set per-request by the tenant ASGI wrapper in server.py.
# Value is the raw path segment (e.g. "marvel" or "demo.uptycs.io").
tenant_context_var: ContextVar[str | None] = ContextVar(
    "tenant_context_var", default=None,
)


def get_tenant() -> str | None:
    """Return the tenant slug for the current request, or None."""
    return tenant_context_var.get()


def resolve_host(tenant: str, default_suffix: str) -> tuple[str, str]:
    """Parse tenant path segment into (domain, domain_suffix).

    - 'marvel'           → ('marvel', '.uptycs.net')  # short name + default suffix
    - 'demo.uptycs.io'  → ('demo', '.uptycs.io')     # full hostname
    """
    if "." in tenant:
        dot = tenant.index(".")
        return tenant[:dot], tenant[dot:]
    return tenant, default_suffix
