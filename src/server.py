"""Juno MCP server entry point."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from .auth import ApiKey
from .client import JunoClient
from .tools import dispatch, get_all_tools, is_write_tool

logger = logging.getLogger("juno_mcp")

_INSTRUCTIONS = """\
You are using the Uptycs Juno AI Analyst MCP server.

## ID Format (CRITICAL)
- All IDs (investigation_id, run_id, project_id, parent_run_id) are \
**plain UUIDs**. **NEVER** add prefixes like "inv_", "run_", or \
"proj_". **ALWAYS** strip prefixes before passing IDs to any \
tool call, even if the tool response or display text includes them.
  - ❌ `inv_684ff06a-1234-5678-9abc-def012345678`
  - ✅ `684ff06a-1234-5678-9abc-def012345678`
- Copy IDs **exactly** as the raw UUID returned by previous tool \
calls — no prefixes, no modifications.

## Anti-patterns (CRITICAL — NEVER do these)
- **Never** prefix IDs with `inv_`, `run_`, or `proj_` \
(see ID Format above).
- **Never** add your own section headers like "## Run 2 — Title".
  - ❌ Adding "## Key Takeaways" after tool output
  - ✅ Present output verbatim, then ask about findings
- **Never** rewrite tables into a different format.
- **Never** replace an image with a text description.
- **Never** add "Key takeaways", "Summary", or interpretive sections.
- **Never** call get_run on a run that is not completed.
  - ❌ Run still in progress → call get_run
  - ✅ Run still in progress → call stream_run
- **Never** call get_run after get_investigation.
- **Never** re-embed ImageContent using markdown image syntax \
(e.g., `![alt](url)`) — images render automatically.

## Response Style (CRITICAL)
- The tool output block is collapsed by default — the user \
**CANNOT** see it unless you present it in your response.
- Present tool responses **VERBATIM**. Copy-paste the markdown \
exactly as returned — including all headings, tables, section \
order, formatting, and images.
- Do **NOT** reformat, add your own headers, rename sections, \
reorder, restructure, summarize, condense, or interpret the output.
- Tool responses may include images as ImageContent blocks. \
Claude renders these automatically — do **NOT** attempt to \
re-embed them using markdown image syntax.
- Some responses include ```mermaid fenced code blocks for \
diagrams (architecture, flow, sequence). Render these using \
the Mermaid Chart validate_and_render tool. Do **NOT** display \
raw mermaid code blocks.
- If tables show "Showing X of Y rows", preserve that note. \
Do **not** fetch remaining rows unless the user asks.
- Your only additions should come **AFTER** the full verbatim \
output: brief follow-up suggestions or asking if the user wants \
findings. No analysis, no "key takeaways."
- Show each result immediately as received. **Never** block \
output waiting for all calls.

## Parallel Execution
- If call B does not depend on call A, issue both in the same \
response.
- Parallelizable: list_investigations + list_published_runs, \
multiple get_findings calls for different runs.
- Sequential only: stream_run → get_findings (needs completed \
status), create_follow_up → stream_run → get_findings.

## Tool Workflows

### Starting investigations
- Use create_investigation to start. It returns immediately with \
investigation and run IDs.
- Then call stream_run with the investigation_id and run_id.
- If stream_run returns a completed run, present the results and \
ask if the user wants findings.
- If stream_run says the run is still in progress, display the \
task progress with status icons (✅/🔄/⏳) and tell the user. \
When the user asks to check again, call stream_run again.

### get_investigation
- The response already includes run summaries, sections, and \
images — **NEVER** call get_run after get_investigation.
- After presenting, ask if the user wants detailed findings \
(get_findings).
- If a run is not 'completed', show status and offer stream_run.
- Multiple runs → present all run summaries, then ask which \
run's findings to fetch.

### get_run
- **ONLY** use get_run on runs that are already **completed**. \
**Never** call get_run on a running or in-progress run.
- After presenting, ask if they want findings: specific \
(get_finding) or all (get_findings).

### get_findings / get_finding
- If findings contain images, reference them per the \
Response Style rules.
- After presenting, offer follow-up investigation or ask if \
the user wants to explore a specific finding in more detail.

### create_follow_up
- After creating, call stream_run with the returned run_id.
- If stream_run returns completed, present results and ask \
about findings.
- If still in progress, tell the user and offer to check \
again with stream_run.

### list_investigations / list_published_runs
- Do **NOT** auto-paginate. Only use the cursor if the user \
explicitly asks for more.

### sql_translate
- Offer to explain the query if the user asks.

### publish_run / unpublish_run
- Only publish or unpublish when the user **explicitly** asks. \
**Never** auto-publish.

## Reminder
**Strip ID prefixes. Present output verbatim. No summaries.**
"""

server = Server("juno", instructions=_INSTRUCTIONS)

# Globals — initialized in main()
_client: JunoClient | None = None
_read_only: bool = True
_beta: bool = False
_connectors: dict[str, list[str]] = {}  # domain → connector IDs
_oauth_provider = None  # Set in _run_sse() for BYOK mode
_byok: bool = False  # True when API key file has empty key/secret
_domain_suffix: str = ".uptycs.net"  # default suffix for multi-tenant routing


@server.list_tools()
async def list_tools():
    return get_all_tools(read_only=_read_only)


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if _read_only and is_write_tool(name):
        return [
            TextContent(
                type="text",
                text=f"Blocked: {name} is not available in read-only mode.",
            )
        ]

    client = _client
    if _byok and _oauth_provider is not None:
        # Use the authenticated user's own API key/secret
        from mcp.server.auth.middleware.auth_context import (
            get_access_token,
        )
        token = get_access_token()
        if token is None:
            return [TextContent(type="text", text="Not authenticated.")]
        user_key = _oauth_provider.get_user_api_key(token.client_id)
        if user_key is None:
            return [TextContent(type="text",
                                text="API key not found. Re-authenticate.")]

        # Multi-tenant guard: verify tenant path matches saved key
        from .tenant import get_tenant, resolve_host
        tenant = get_tenant()
        if tenant and _domain_suffix:
            t_domain, t_suffix = resolve_host(
                tenant, _domain_suffix,
            )
            if (user_key.domain != t_domain
                    or user_key.domain_suffix != t_suffix):
                return [TextContent(
                    type="text",
                    text="Tenant mismatch. Re-authenticate.",
                )]

        host = f"{user_key.domain}{user_key.domain_suffix}"
        client = JunoClient(
            user_key,
            beta=_beta,
            connector_ids=_connectors.get(host, []),
        )

    if client is None:
        return [TextContent(type="text", text="Server not initialized.")]

    # Build progress callback for stream_run if client sent a progress token
    on_progress = None
    ctx = server.request_context
    progress_token = ctx.meta.progressToken if ctx.meta else None
    logger.info("Tool %s: progressToken=%s", name, progress_token)
    if progress_token is not None:
        async def on_progress(progress: float, total: float | None, message: str) -> None:
            await ctx.session.send_progress_notification(
                progress_token=progress_token,
                progress=progress,
                total=total,
                message=message,
            )

    try:
        result = await dispatch(name, arguments or {}, client, on_progress)
    except KeyError:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=f"Error: {exc}")]
    finally:
        # Close per-request client to avoid connection leaks
        if client is not _client:
            await client.close()
    return result


def _log_dir() -> Path:
    """Determine writable log directory.

    Uses ``logs/`` next to project root when running from a local
    checkout, otherwise falls back to ``~/.local/share/juno-mcp/logs``.
    """
    project_dir = Path(__file__).resolve().parent.parent
    if (project_dir / "pyproject.toml").exists():
        return project_dir / "logs"
    return Path.home() / ".local" / "share" / "juno-mcp" / "logs"


def _setup_logging() -> None:
    level = os.environ.get("JUNO_MCP_LOG_LEVEL", "INFO").upper()
    fmt = logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    log_path = _log_dir()
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / "juno-mcp.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.addHandler(file_handler)

    # Also log to stderr so `docker logs` shows app-level messages
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)


def _init() -> None:
    """Initialize logging, API client, and read-only flag."""
    global _client, _read_only, _byok, _beta, _connectors

    _setup_logging()

    key_file = os.environ.get("UPTYCS_API_KEY_FILE")
    _beta = os.environ.get("JUNO_BETA", "false").lower() != "false"
    _read_only = os.environ.get("JUNO_MCP_READ_ONLY", "false").lower() != "false"


    # Load domain → connector-ID mappings from JSON file
    connectors_file = os.environ.get("JUNO_CONNECTORS_FILE")
    if connectors_file:
        import json as _json
        _connectors = _json.loads(Path(connectors_file).read_text())

    if key_file:
        api_key = ApiKey.from_file(key_file)
        _byok = not api_key.key or not api_key.secret
        if not _byok:
            host = f"{api_key.domain}{api_key.domain_suffix}"
            _client = JunoClient(
                api_key,
                beta=_beta,
                connector_ids=_connectors.get(host, []),
            )
    else:
        # No key file — BYOK mode, expect credentials from clients
        _byok = True

    logger.info(
        "Juno MCP server starting (transport=%s, read_only=%s, byok=%s, beta=%s)",
        os.environ.get("JUNO_MCP_TRANSPORT", "stdio"),
        _read_only,
        _byok,
        _beta,
    )


# ------------------------------------------------------------------
# stdio transport (default) — for local Claude Desktop
# ------------------------------------------------------------------

async def _run_stdio() -> None:
    try:
        async with stdio_server() as streams:
            read_stream, write_stream = streams
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if _client is not None:
            await _client.close()


# ------------------------------------------------------------------
# Streamable HTTP transport — for remote Claude connectors
#
#   JUNO_MCP_TRANSPORT=sse
#   JUNO_MCP_HOST=0.0.0.0   (default)
#   JUNO_MCP_PORT=39271      (default)
# ------------------------------------------------------------------

def _run_sse() -> None:
    global _oauth_provider

    import uvicorn
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
    from mcp.server.auth.provider import ProviderTokenVerifier
    from mcp.server.auth.routes import (
        create_auth_routes,
        create_protected_resource_routes,
    )
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from pydantic import AnyHttpUrl
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.authentication import AuthenticationMiddleware
    from starlette.routing import Mount, Route

    from .oauth import UptycsOAuthProvider

    host = os.environ.get("JUNO_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("JUNO_MCP_PORT", "39271"))

    # The issuer URL must match what Claude.ai sees (the public HTTPS URL)
    public_url = os.environ.get("JUNO_MCP_PUBLIC_URL", f"http://localhost:{port}")
    issuer_url = AnyHttpUrl(public_url)
    resource_url = AnyHttpUrl(f"{public_url}/mcp")

    # Read base config from API key file if available; otherwise
    # clients must supply tenant info via hostname:key format.
    key_file = os.environ.get("UPTYCS_API_KEY_FILE")
    if key_file:
        api_key = ApiKey.from_file(key_file)
        keys_dir = str(Path(key_file).resolve().parent / "keys")
        domain, customer_id, domain_suffix = (
            api_key.domain, api_key.customer_id, api_key.domain_suffix,
        )
    else:
        keys_dir = str(
            Path.home() / ".local" / "share" / "juno-mcp" / "keys"
        )
        domain, customer_id, domain_suffix = "", "", ""

    oauth_provider = UptycsOAuthProvider(
        domain=domain,
        customer_id=customer_id,
        domain_suffix=domain_suffix,
        issuer_url=public_url,
        default_domain_suffix=_domain_suffix,
        _keys_dir=keys_dir,
    )
    _oauth_provider = oauth_provider
    token_verifier = ProviderTokenVerifier(oauth_provider)

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
    )

    streamable_http_app = session_manager.handle_request

    # OAuth endpoints: /.well-known/*, /authorize, /token
    routes: list[Route | Mount] = list(
        create_auth_routes(
            provider=oauth_provider,
            issuer_url=issuer_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["api"],
            ),
            revocation_options=RevocationOptions(enabled=False),
        )
    )

    # Wrap the /token route to capture client_secret from the form
    # before the SDK processes (and discards) it.  The secret is
    # needed in exchange_authorization_code() for Juno validation.
    from urllib.parse import parse_qs

    for route in routes:
        if getattr(route, "path", None) == "/token":
            _orig_token_app = route.app

            async def _token_wrapper(scope, receive, send,
                                     _app=_orig_token_app):
                if scope["type"] != "http":
                    await _app(scope, receive, send)
                    return

                # Buffer the request body so we can peek at
                # client_secret, then replay it for the SDK.
                body_parts: list[bytes] = []
                body_complete = False

                async def _buffering_receive():
                    nonlocal body_complete
                    msg = await receive()
                    if msg["type"] == "http.request":
                        body_parts.append(msg.get("body", b""))
                        if not msg.get("more_body", False):
                            body_complete = True
                    return msg

                # Consume the full body via buffering receive
                while not body_complete:
                    await _buffering_receive()

                raw = b"".join(body_parts)
                params = parse_qs(raw.decode("utf-8", errors="replace"))
                cid_list = params.get("client_id", [])
                csec_list = params.get("client_secret", [])
                if cid_list and csec_list:
                    oauth_provider.capture_client_secret(
                        cid_list[0], csec_list[0],
                    )

                # Replay the buffered body for the downstream app
                body_sent = False

                async def _replay_receive():
                    nonlocal body_sent
                    if not body_sent:
                        body_sent = True
                        return {
                            "type": "http.request",
                            "body": raw,
                            "more_body": False,
                        }
                    # After body, just wait (disconnect, etc.)
                    return await receive()

                await _app(scope, _replay_receive, send)

            route.app = _token_wrapper
            break

    # Protected resource metadata (RFC 9728) — static /mcp route
    routes.extend(
        create_protected_resource_routes(
            resource_url=resource_url,
            authorization_servers=[issuer_url],
            scopes_supported=["api"],
        )
    )

    # MCP endpoint (protected by bearer auth)
    from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
    from mcp.server.auth.routes import build_resource_metadata_url

    resource_metadata_url = build_resource_metadata_url(resource_url)
    routes.append(
        Route(
            "/mcp",
            endpoint=RequireAuthMiddleware(
                streamable_http_app, ["api"], resource_metadata_url,
            ),
        ),
    )

    # --- Multi-tenant path routing: /{tenant}/mcp ---
    if _domain_suffix:
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from .tenant import tenant_context_var

        # Dynamic protected-resource metadata for /{tenant}/mcp
        async def _tenant_resource_metadata(request: Request):
            tenant = request.path_params["tenant"]
            t_resource = f"{public_url}/{tenant}/mcp"
            return JSONResponse({
                "resource": t_resource,
                "authorization_servers": [public_url],
                "scopes_supported": ["api"],
                "bearer_methods_supported": ["header"],
            })

        routes.append(Route(
            "/.well-known/oauth-protected-resource/{tenant:path}/mcp",
            endpoint=_tenant_resource_metadata,
            methods=["GET"],
        ))

        # Tenant MCP endpoint — raw ASGI app that sets tenant
        # contextvar, then delegates to auth-protected MCP handler.
        auth_app = RequireAuthMiddleware(
            streamable_http_app, ["api"], resource_metadata_url,
        )

        async def _tenant_mcp_asgi(scope, receive, send):
            tenant = scope.get("path_params", {}).get("tenant", "")
            tok = tenant_context_var.set(tenant)
            try:
                await auth_app(scope, receive, send)
            finally:
                tenant_context_var.reset(tok)

        # Use Route then override .app so Starlette treats it as
        # a raw ASGI app (accepts all HTTP methods, no wrapping).
        _tenant_route = Route("/{tenant:path}/mcp", endpoint=auth_app)
        _tenant_route.app = _tenant_mcp_asgi
        routes.append(_tenant_route)

        logger.info(
            "Multi-tenant routing enabled (suffix=%s)",
            _domain_suffix,
        )

    middleware = [
        Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(token_verifier)),
        Middleware(AuthContextMiddleware),
    ]

    app = Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=lambda app: session_manager.run(),
    )

    logger.info("OAuth enabled (issuer=%s)", public_url)
    logger.info("Streamable HTTP transport on %s:%d/mcp", host, port)
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    _init()

    transport = os.environ.get("JUNO_MCP_TRANSPORT", "stdio").lower()
    if transport == "sse":
        _run_sse()
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
