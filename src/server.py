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

from importlib.metadata import version as pkg_version

from .auth import ApiKey
from .client import JunoClient
from .tools import dispatch, get_all_tools

logger = logging.getLogger("juno_mcp")

try:
    __version__ = pkg_version("juno-mcp-server")
except Exception:
    __version__ = "0.0.0-dev"

_INSTRUCTIONS = """\
You are using the Uptycs Juno AI Analyst MCP server.

## What is Juno?
Uptycs Juno is an AI-powered security analyst. Given a security question, Juno autonomously
investigates across Uptycs telemetry and, on demand based on context, MCP connectors
(CloudWatch, GitHub, Nuclei, etc.) to produce structured findings with severity, evidence,
affected assets, recommendations, and executive summaries.

Telemetry covers: alerts, detections, risk factors, cloud resources (AWS, GCP, Azure),
endpoint events (processes, network), Kubernetes, and compliance findings.

Agent types (auto-selected or user-specified): `security_analyst` (default), `incident_response`, `ciso`.

## ID Format
All IDs are plain UUIDs. Never add prefixes like "inv_" or "run_".
  ✅ 684ff06a-1234-5678-9abc-def012345678

## Before Creating an Investigation
1. Improve vague or inventory-like questions into specific security questions. Confirm with user.
2. Call list_investigations with search to check for duplicates first.
3. Prefer create_follow_up over create_investigation when deepening existing results.

## Polling for Results
After create_investigation or create_follow_up:
1. Extract investigation_id and run_id from the response.
2. Poll with get_run. On pending/running: show partial data, wait ~10s (back off to ~30s after 3 polls).
3. Keep polling until completed or failed. Show progress incrementally.

## Response Rules
- Present tool output verbatim. Do not reformat, summarize, or reorder.
- Always present suggestedPrompts from get_run as actionable next steps.
- Do not auto-paginate — only use cursor when user asks for more.
- Only publish/unpublish when user explicitly asks.
- Issue independent tool calls in parallel.
"""

server = Server("juno", version=__version__, instructions=_INSTRUCTIONS)

_client: JunoClient | None = None


@server.list_tools()
async def list_tools():
    return get_all_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if _client is None:
        return [TextContent(type="text", text="Server not initialized.")]

    logger.info("Tool %s called", name)

    try:
        result = await dispatch(name, arguments or {}, _client)
    except KeyError:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=f"Error: {exc}")]
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
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)


def _init() -> None:
    """Initialize logging and API client."""
    global _client

    _setup_logging()

    key_file = os.environ.get("UPTYCS_API_KEY_FILE")
    if not key_file:
        raise SystemExit("UPTYCS_API_KEY_FILE is required")

    api_key = ApiKey.from_file(key_file)
    _client = JunoClient(api_key)

    logger.info(
        "Juno MCP server v%s starting — %s",
        __version__, api_key.base_url,
    )


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


def main() -> None:
    _init()
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
