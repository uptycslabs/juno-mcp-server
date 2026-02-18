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
- After presenting, ask if they want findings (get_findings).

### get_findings
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

### publish_run / unpublish_run
- Only publish or unpublish when the user **explicitly** asks. \
**Never** auto-publish.

## Reminder
**Strip ID prefixes. Present output verbatim. No summaries.**
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

    ctx = server.request_context
    progress_token = ctx.meta.progressToken if ctx.meta else None
    logger.info("Tool %s: progressToken=%s", name, progress_token)
    on_progress = None
    if progress_token is not None:
        async def _on_progress(
            progress: float,
            total: float | None,
            message: str,
        ) -> None:
            await ctx.session.send_progress_notification(
                progress_token=progress_token,
                progress=progress,
                total=total,
                message=message,
            )
        on_progress = _on_progress

    try:
        result = await dispatch(name, arguments or {}, _client, on_progress)
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
