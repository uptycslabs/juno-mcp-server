"""Tool registry — all Juno MCP tools in one place.

Handlers, schemas, response filtering, and markdown formatting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

import httpx
from mcp.types import ImageContent, TextContent, Tool

from .client import JunoClient
from .charts import render_viz_b64

logger = logging.getLogger("juno_mcp.tools")

_POLL_INTERVAL = 5

@dataclass(frozen=True)
class ToolDef:
    """Single tool definition."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[JunoClient, dict[str, Any]], Awaitable[str]]


def _text(content: str) -> list[TextContent]:
    return [TextContent(type="text", text=content)]


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _validate_uuid(args: dict[str, Any], *keys: str) -> None:
    """Raise ``ValueError`` if any *keys* in *args* are not valid UUIDs."""
    for key in keys:
        value = args.get(key)
        if value is not None and not _UUID_RE.match(str(value)):
            raise ValueError(
                f"'{key}' must be a plain UUID "
                f"(e.g. 684ff06a-1234-5678-9abc-def012345678), "
                f"got: {value!r}"
            )


# Chart images accumulated by _md_viz(), consumed by dispatch().
_pending_images: list[ImageContent] = []


_FULL_RESPONSE = (
    os.environ.get("JUNO_MCP_FULL_RESPONSE", "false")
    .lower() == "true"
)

_RESPONSE_FORMAT = os.environ.get(
    "JUNO_RESPONSE_FORMAT", "markdown",
).lower()

_MAX_TABLE_ROWS = 5


def prepare_run_response(data: dict) -> dict:
    """Filter a run response before returning to the client."""
    if _FULL_RESPONSE:
        return data

    data.pop("findings", None)
    data.pop("tasks", None)

    _truncate_table_rows(data)
    _truncate_viz_data(data)
    return data


_RUN_SUMMARY_KEYS = frozenset({
    "id", "uptCreatedAt", "uptUpdatedAt",
    "question", "agent", "status",
})

_FINDING_SUMMARY_KEYS = frozenset({
    "title", "severity", "threatType",
})


def prepare_investigation_response(data: dict) -> dict:
    """Filter an investigation response."""
    if _FULL_RESPONSE:
        return data

    data.pop("tasks", None)
    data.pop("customerId", None)
    data.pop("ownerId", None)
    slim_runs = []
    for run in data.get("runs", []):
        slim = {k: v for k, v in run.items() if k in _RUN_SUMMARY_KEYS}

        if "summarySections" in run:
            slim["summarySections"] = run["summarySections"]
            _truncate_table_rows(slim)
            _truncate_viz_data(slim)

        findings = run.get("findings", [])
        if findings:
            slim["findings"] = [
                {k: v for k, v in f.items() if k in _FINDING_SUMMARY_KEYS}
                for f in findings
            ]
        slim_runs.append(slim)
    data["runs"] = slim_runs

    return data


def prepare_findings_response(
    findings: list[dict],
) -> list[dict]:
    """Filter a findings list before returning."""
    if _FULL_RESPONSE:
        return findings

    for finding in findings:
        viz = finding.get("visualization")
        if not viz:
            continue
        if isinstance(viz.get("data"), list):
            total = len(viz["data"])
            if total > _MAX_TABLE_ROWS:
                viz["data"] = viz["data"][:_MAX_TABLE_ROWS]
                viz["truncated"] = True
                viz["total_rows"] = total

    return findings


def _truncate_table_rows(data: dict) -> None:
    """Cap tableRef.rows in summarySections."""
    for section in data.get("summarySections", []):
        ref = (
            section.get("tableRef")
            or section.get("table_ref")
        )
        if ref and isinstance(ref.get("rows"), list):
            total = len(ref["rows"])
            if total > _MAX_TABLE_ROWS:
                ref["rows"] = ref["rows"][:_MAX_TABLE_ROWS]
                ref["truncated"] = True
                ref["total_rows"] = total


def _truncate_viz_data(data: dict) -> None:
    """Cap visualization data rows in summarySections."""
    for section in data.get("summarySections", []):
        viz = section.get("visualization")
        if not viz:
            continue
        if isinstance(viz.get("data"), list):
            total = len(viz["data"])
            if total > _MAX_TABLE_ROWS:
                viz["data"] = viz["data"][:_MAX_TABLE_ROWS]
                viz["truncated"] = True
                viz["total_rows"] = total

async def _list_investigations(
    client: JunoClient, args: dict[str, Any],
) -> str:
    _validate_uuid(args, "project_id")
    data = await client.list_investigations(
        search=args.get("search"),
        limit=args.get("limit", 5),
        cursor=args.get("cursor"),
        project_id=args.get("project_id"),
    )
    return json.dumps(data, indent=2, default=str)


async def _get_investigation(
    client: JunoClient, args: dict[str, Any],
) -> str:
    _validate_uuid(args, "investigation_id")
    data = await client.get_investigation(
        args["investigation_id"],
    )
    prepare_investigation_response(data)
    return json.dumps(data, indent=2, default=str)


async def _create_investigation(
    client: JunoClient, args: dict[str, Any],
) -> str:
    _validate_uuid(args, "project_id")
    data = await client.create_investigation(
        question=args["question"],
        project_id=args.get("project_id"),
    )
    return json.dumps(data, indent=2, default=str)


async def _delete_investigation(
    client: JunoClient, args: dict[str, Any],
) -> str:
    _validate_uuid(args, "investigation_id")
    await client.delete_investigation(
        args["investigation_id"],
    )
    return f"Investigation {args['investigation_id']} deleted."

async def _get_run(client: JunoClient, args: dict[str, Any]) -> str:
    _validate_uuid(args, "investigation_id", "run_id")
    data = await client.get_run(
        args["investigation_id"], args["run_id"],
    )
    status = data.get("status", "unknown")
    if status not in ("completed", "failed", "error"):
        return (
            f"Run is still **{status}**. "
            f"Use **stream_run** (not get_run) to wait for completion."
        )
    prepare_run_response(data)
    return json.dumps(data, indent=2, default=str)


async def _get_findings(client: JunoClient, args: dict[str, Any]) -> str:
    _validate_uuid(args, "investigation_id", "run_id")
    data = await client.get_run(
        args["investigation_id"], args["run_id"],
    )
    status = data.get("status", "unknown")
    if status not in ("completed", "failed", "error"):
        return (
            f"Run is still **{status}**. "
            f"Use **stream_run** to wait for completion before fetching findings."
        )
    findings = data.get("findings", [])
    if not findings:
        return "No findings for this run."
    prepare_findings_response(findings)
    return json.dumps(findings, indent=2, default=str)


async def _stream_run_handler(
    client: JunoClient,
    args: dict[str, Any],
    on_progress: ProgressCallback | None = None,
) -> str:
    _validate_uuid(args, "investigation_id", "run_id")
    inv_id = args["investigation_id"]
    run_id = args["run_id"]
    timeout = 20
    return await _poll_run(client, inv_id, run_id, timeout, on_progress)


async def _create_follow_up(client: JunoClient, args: dict[str, Any]) -> str:
    _validate_uuid(args, "investigation_id", "parent_run_id")
    data = await client.create_follow_up(
        investigation_id=args["investigation_id"],
        parent_run_id=args["parent_run_id"],
        question=args["question"],
    )
    return json.dumps(data, indent=2, default=str)


async def _publish_run(client: JunoClient, args: dict[str, Any]) -> str:
    _validate_uuid(args, "investigation_id", "run_id")
    await client.publish_run(args["investigation_id"], args["run_id"])
    return f"Run {args['run_id']} published."


async def _unpublish_run(client: JunoClient, args: dict[str, Any]) -> str:
    _validate_uuid(args, "investigation_id", "run_id")
    await client.unpublish_run(args["investigation_id"], args["run_id"])
    return f"Run {args['run_id']} unpublished."


async def _list_published_runs(client: JunoClient, args: dict[str, Any]) -> str:
    data = await client.list_published_runs(
        search=args.get("search"),
        limit=args.get("limit", 5),
        cursor=args.get("cursor"),
    )
    return json.dumps(data, indent=2, default=str)

async def _list_projects(
    client: JunoClient, args: dict[str, Any],
) -> str:
    data = await client.list_projects(
        limit=args.get("limit", 5),
        cursor=args.get("cursor"),
    )
    return json.dumps(data, indent=2, default=str)


async def _create_project(
    client: JunoClient, args: dict[str, Any],
) -> str:
    data = await client.create_project(
        name=args["name"],
        description=args.get("description", ""),
    )
    return json.dumps(data, indent=2, default=str)


async def _delete_project(
    client: JunoClient, args: dict[str, Any],
) -> str:
    _validate_uuid(args, "project_id")
    await client.delete_project(args["project_id"])
    return f"Project {args['project_id']} deleted."

ProgressCallback = Callable[[float, float | None, str], Awaitable[None]]


async def _poll_run(
    client: JunoClient,
    inv_id: str,
    run_id: str,
    timeout: int,
    on_progress: ProgressCallback | None = None,
) -> str:
    try:
        return await asyncio.wait_for(
            _stream_run(client, inv_id, run_id, on_progress),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.info("stream_run timed out after %ds, fetching current state", timeout)
        try:
            data = await asyncio.wait_for(
                client.get_run(inv_id, run_id), timeout=20,
            )
            status = data.get("status", "unknown")
            if status in ("completed", "failed", "error"):
                prepare_run_response(data)
                return json.dumps(data, indent=2, default=str)
            progress_md = _fmt_task_progress(data)
            return (
                f"{progress_md}\n\n"
                f"Still {status}. Use **stream_run** to check again later."
            )
        except Exception:
            logger.warning("get_run after timeout failed", exc_info=True)
            return (
                f"Timed out after {timeout}s. Run {run_id} is still "
                f"running.\nUse **stream_run** to check again later."
            )
    except (OSError, httpx.HTTPError) as exc:
        logger.warning("SSE stream failed (%s), falling back to polling", exc)
        return await _poll_run_legacy(client, inv_id, run_id, timeout)


def _task_progress_message(data: dict) -> str:
    """Build a human-readable progress message from run tasks."""
    tasks = data.get("tasks", [])
    if not tasks:
        return data.get("status", "running")
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    total = len(tasks)
    current = next(
        (t.get("title", "") for t in tasks if t.get("status") == "running"),
        "",
    )
    if current:
        return f"[{completed}/{total}] {current}"
    return f"[{completed}/{total}] Processing..."


def _fmt_task_progress(data: dict) -> str:
    """Format a compact progress line for timeout responses.

    Only shows the count and currently running task — avoids
    repeating the full task list on every poll.
    """
    tasks = data.get("tasks", [])
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    total = len(tasks)
    running = next(
        (t.get("title", "") for t in tasks if t.get("status") == "running"),
        "",
    )
    if running:
        return f"Progress: {completed}/{total} tasks done — now running: {running}"
    return f"Progress: {completed}/{total} tasks done"


async def _stream_run(
    client: JunoClient,
    inv_id: str,
    run_id: str,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Consume SSE events until the run completes."""
    data: dict = {}
    got_update = False
    last_msg = ""
    event_count = 0
    logger.info("_stream_run starting for inv=%s run=%s", inv_id, run_id)
    async for event_type, payload in client.stream_run_events(inv_id, run_id):
        event_count += 1
        logger.info("SSE event #%d: type=%s status=%s", event_count, event_type, payload.get("status", ""))
        if event_type == "update":
            data = payload
            got_update = True
            status = data.get("status", "")
            if status in ("completed", "failed", "error"):
                logger.info("Run reached terminal status: %s", status)
                break
            if on_progress:
                msg = _task_progress_message(data)
                if msg != last_msg:
                    last_msg = msg
                    tasks = data.get("tasks", [])
                    total = len(tasks) or None
                    completed = sum(
                        1 for t in tasks
                        if t.get("status") == "completed"
                    )
                    try:
                        await on_progress(
                            float(completed),
                            float(total) if total else None,
                            msg,
                        )
                    except Exception:
                        logger.debug("Progress notification failed", exc_info=True)
        elif event_type == "done":
            logger.info("SSE done event received")
            break
        elif event_type == "error":
            # Ignore transient errors before first update
            if got_update:
                raise RuntimeError(payload.get("error", "SSE error"))
            logger.debug("SSE transient error (pre-update): %s", payload)
    logger.info("_stream_run finished: %d events, got_update=%s", event_count, got_update)
    if data:
        prepare_run_response(data)
        return json.dumps(data, indent=2, default=str)
    data = await client.get_run(inv_id, run_id)
    prepare_run_response(data)
    return json.dumps(data, indent=2, default=str)


async def _poll_run_legacy(
    client: JunoClient,
    inv_id: str,
    run_id: str,
    timeout: int,
) -> str:
    """Fallback: poll GET /runs/:id every few seconds."""
    deadline = asyncio.get_running_loop().time() + timeout
    status = "unknown"
    while asyncio.get_running_loop().time() < deadline:
        data = await client.get_run(inv_id, run_id)
        status = data.get("status", "unknown")
        if status in ("completed", "failed", "error"):
            prepare_run_response(data)
            return json.dumps(data, indent=2, default=str)
        await asyncio.sleep(_POLL_INTERVAL)

    return (
        f"Timed out after {timeout}s. Run {run_id} is still "
        f"{status}.\nUse **stream_run** to check again later."
    )

_ALL_TOOLS: list[ToolDef] = [
    ToolDef(
        name="list_investigations",
        description="List recent Juno investigations with optional search. Do NOT auto-paginate — only use cursor if the user explicitly asks for more.",
        schema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Search terms"},
                "limit": {"type": "integer", "default": 5, "description": "Max results to return"},
                "cursor": {"type": "string", "description": "Pagination cursor"},
                "project_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
            },
        },
        handler=_list_investigations,
    ),
    ToolDef(
        name="get_investigation",
        description="Get details of a specific investigation including its runs.",
        schema={
            "type": "object",
            "properties": {
                "investigation_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
            },
            "required": ["investigation_id"],
        },
        handler=_get_investigation,
    ),
    ToolDef(
        name="create_investigation",
        description="Start a new Juno security investigation.",
        schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The security question to investigate"},
                "project_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
            },
            "required": ["question"],
        },
        handler=_create_investigation,
    ),
    ToolDef(
        name="delete_investigation",
        description="Delete an investigation and all its runs.",
        schema={
            "type": "object",
            "properties": {
                "investigation_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
            },
            "required": ["investigation_id"],
        },
        handler=_delete_investigation,
    ),
    ToolDef(
        name="get_run",
        description="Get a completed run's full details (summary, tasks, suggested prompts). Only use on runs with status 'completed'. For in-progress runs, use stream_run instead.",
        schema={
            "type": "object",
            "properties": {
                "investigation_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
                "run_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
            },
            "required": ["investigation_id", "run_id"],
        },
        handler=_get_run,
    ),
    ToolDef(
        name="get_findings",
        description="Get all findings with evidence, recommendations, and visualizations. Only use on completed runs.",
        schema={
            "type": "object",
            "properties": {
                "investigation_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
                "run_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
            },
            "required": ["investigation_id", "run_id"],
        },
        handler=_get_findings,
    ),
    ToolDef(
        name="stream_run",
        description=(
            "Wait for a run to complete, streaming SSE updates internally. "
            "Returns the full run result when done, or current progress if "
            "it times out. If still in progress, call stream_run again "
            "when the user asks to check."
        ),
        schema={
            "type": "object",
            "properties": {
                "investigation_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
                "run_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max seconds to wait (default 20)",
                },
            },
            "required": ["investigation_id", "run_id"],
        },
        handler=_stream_run_handler,
    ),
    ToolDef(
        name="create_follow_up",
        description="Ask a follow-up question on an existing investigation run.",
        schema={
            "type": "object",
            "properties": {
                "investigation_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
                "parent_run_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
                "question": {"type": "string", "description": "Follow-up question to ask"},
            },
            "required": ["investigation_id", "parent_run_id", "question"],
        },
        handler=_create_follow_up,
    ),
    ToolDef(
        name="publish_run",
        description="Share a run with your team by publishing it.",
        schema={
            "type": "object",
            "properties": {
                "investigation_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
                "run_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
            },
            "required": ["investigation_id", "run_id"],
        },
        handler=_publish_run,
    ),
    ToolDef(
        name="unpublish_run",
        description="Remove a run from the published list.",
        schema={
            "type": "object",
            "properties": {
                "investigation_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
                "run_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
            },
            "required": ["investigation_id", "run_id"],
        },
        handler=_unpublish_run,
    ),
    ToolDef(
        name="list_published_runs",
        description="Browse team-published investigation runs. Do NOT auto-paginate — only use cursor if the user explicitly asks for more.",
        schema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Search terms"},
                "limit": {"type": "integer", "default": 5, "description": "Max results to return"},
                "cursor": {"type": "string", "description": "Pagination cursor"},
            },
        },
        handler=_list_published_runs,
    ),
    ToolDef(
        name="list_projects",
        description="List Juno projects. Do NOT auto-paginate — only use cursor if the user explicitly asks for more.",
        schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 5, "description": "Max results to return"},
                "cursor": {"type": "string", "description": "Pagination cursor"},
            },
        },
        handler=_list_projects,
    ),
    ToolDef(
        name="create_project",
        description="Create a new project to organize investigations.",
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name"},
                "description": {"type": "string", "description": "Project description"},
            },
            "required": ["name"],
        },
        handler=_create_project,
    ),
    ToolDef(
        name="delete_project",
        description="Delete a project.",
        schema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Plain UUID (no prefix)",
                },
            },
            "required": ["project_id"],
        },
        handler=_delete_project,
    ),
]

_HANDLERS: dict[str, ToolDef] = {td.name: td for td in _ALL_TOOLS}

def get_all_tools() -> list[Tool]:
    """Return all MCP Tool objects."""
    return [
        Tool(name=td.name, description=td.description, inputSchema=td.schema)
        for td in _ALL_TOOLS
    ]


async def dispatch(
    name: str,
    arguments: dict[str, Any],
    client: JunoClient,
    on_progress: ProgressCallback | None = None,
) -> list[TextContent | ImageContent]:
    """Dispatch a tool call by name. Raises KeyError for unknown tools."""
    td = _HANDLERS[name]
    if name == "stream_run" and on_progress is not None:
        result = await td.handler(client, arguments, on_progress)
    else:
        result = await td.handler(client, arguments)

    _pending_images.clear()

    if _RESPONSE_FORMAT == "markdown":
        result = _try_format_markdown(name, result)

    content: list[TextContent | ImageContent] = _text(result)
    if _pending_images:
        content.extend(_pending_images)
        _pending_images.clear()
    return content


def _try_format_markdown(name: str, result: str) -> str:
    """Convert a JSON result string to markdown if possible."""
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return result
    return format_markdown(name, data)

def _esc(value: Any) -> str:
    """Escape a value for use inside a markdown table cell."""
    s = str(value) if value is not None else ""
    return s.replace("|", "\\|").replace("\n", " ")


def _bullet(label: str, value: Any, fallback: str = "\u2014") -> str:
    return f"- **{label}**: {value if value else fallback}"


def _md_table(columns: list[str], rows: list[dict]) -> str:
    """Render a list of dicts as a markdown table."""
    if not columns or not rows:
        return ""
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, sep]
    for row in rows:
        cells = [_esc(row.get(c, "")) for c in columns]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _md_table_ref(ref: dict) -> str:
    """Render a tableRef (from summarySections) as markdown."""
    columns = ref.get("columns", [])
    rows = ref.get("rows", [])
    parts: list[str] = []
    if ref.get("description"):
        parts.append(ref["description"])
    tbl = _md_table(columns, rows)
    if tbl:
        parts.append(tbl)
    if ref.get("truncated"):
        parts.append(
            f"_Showing {len(rows)} of {ref.get('total_rows', '?')} rows_"
        )
    return "\n\n".join(parts)


def _md_viz(viz: dict) -> str:
    """Render a visualization as a chart image with a data table.

    Renders via matplotlib and appends the image to
    ``_pending_images``.  Always includes a data table alongside.
    """
    parts: list[str] = []
    title = viz.get("title", "Visualization")
    parts.append(f"**{title}**")
    if viz.get("description"):
        parts.append(viz["description"])

    schema = viz.get("schema") or {}
    mermaid_code = schema.get("mermaidCode", "")
    if mermaid_code:
        elk_directive = '%%{init: {"flowchart": {"defaultRenderer": "elk"}} }%%'
        parts.append(f"```mermaid\n{elk_directive}\n{mermaid_code}\n```")

    b64 = render_viz_b64(viz)
    if b64:
        _pending_images.append(
            ImageContent(type="image", data=b64, mimeType="image/png")
        )

    data = viz.get("data", [])
    if data:
        cols = list(data[0].keys())
        parts.append(_md_table(cols, data))
        if viz.get("truncated"):
            parts.append(
                f"_Showing {len(data)} of "
                f"{viz.get('total_rows', '?')} rows_"
            )
    return "\n\n".join(parts)


def _cursor_line(data: dict) -> str:
    cursor = data.get("nextCursor", "")
    if cursor:
        return (
            f"\n_More results available (cursor: `{cursor}`). "
            f"Only fetch more if the user explicitly asks._"
        )
    return ""

def _fmt_investigation_list(data: dict) -> str:
    items = data.get("items", [])
    if not items:
        return "No investigations found." + _cursor_line(data)

    lines = ["# Investigations", ""]
    header = "| # | ID | Title | Question |"
    sep = "| --- | --- | --- | --- |"
    lines.extend([header, sep])
    for i, inv in enumerate(items, 1):
        inv_id = _esc(inv.get("id", ""))
        title = _esc(inv.get("title", inv.get("question", "")))
        question = _esc(inv.get("question", ""))
        lines.append(f"| {i} | {inv_id} | {title} | {question} |")
    lines.append(_cursor_line(data))
    return "\n".join(lines)


def _fmt_investigation(data: dict) -> str:
    parts: list[str] = []
    title = data.get("title", data.get("question", "Investigation"))
    parts.append(f"# {title}")
    parts.append("")
    parts.append(_bullet("Question", data.get("question")))

    runs = data.get("runs", [])
    if runs:
        parts.append("")
        parts.append("## Runs")
        for run in runs:
            parts.append("")
            parts.append(f"### Run: {run.get('id', '')}")
            parts.append(_bullet("Status", run.get("status")))

            _append_summary_sections(parts, run)

            findings = run.get("findings", [])
            if findings:
                parts.append("")
                parts.append("#### Findings")
                for f in findings:
                    severity = f.get("severity", "")
                    threat = f.get("threatType", "")
                    label = f"**{severity}**" if severity else ""
                    if threat:
                        label += f" | {threat}" if label else threat
                    ftitle = f.get("title", "Untitled")
                    parts.append(f"- {ftitle} ({label})")

    return "\n".join(parts)

def _fmt_run(data: dict) -> str:
    parts: list[str] = []
    parts.append(f"# Run: {data.get('id', '')}")
    parts.append("")
    parts.append(_bullet("Status", data.get("status")))
    parts.append(_bullet("Investigation", data.get("investigationId")))
    parts.append(_bullet("Question", data.get("question")))

    if data.get("errorMessage"):
        parts.append(_bullet("Error", data["errorMessage"]))

    _append_summary_sections(parts, data)
    _append_suggested_prompts(parts, data)

    return "\n".join(parts)


def _append_summary_sections(parts: list[str], data: dict) -> None:
    sections = data.get("summarySections", [])
    if not sections:
        return
    parts.append("")
    parts.append("## Summary")
    for sec in sections:
        parts.append("")
        parts.append(f"### {sec.get('title', 'Section')}")
        content = sec.get("content", "")
        if content:
            parts.append(content)

        ref = sec.get("tableRef") or sec.get("table_ref")
        if ref and ref.get("rows"):
            parts.append("")
            parts.append(_md_table_ref(ref))

        viz = sec.get("visualization")
        if viz:
            parts.append("")
            parts.append(_md_viz(viz))


def _append_suggested_prompts(parts: list[str], data: dict) -> None:
    prompts = data.get("suggestedPrompts", [])
    if not prompts:
        return
    parts.append("")
    parts.append("## Suggested Follow-ups")
    for p in prompts:
        parts.append(f"- {p}")

def _fmt_findings(findings: list[dict]) -> str:
    if not findings:
        return "No findings."
    parts: list[str] = [f"# Findings ({len(findings)})"]

    for finding in findings:
        parts.append("")
        parts.append("---")
        parts.append(f"## {finding.get('title', 'Finding')}")
        parts.append("")
        parts.append(_bullet("Severity", finding.get("severity")))
        parts.append(_bullet("Threat Type", finding.get("threatType")))

        assets = finding.get("affectedAssets", [])
        if assets:
            parts.append(
                _bullet("Affected Assets", ", ".join(str(a) for a in assets))
            )

        desc = finding.get("description", "")
        if desc:
            parts.append("")
            parts.append(desc)

        evidence = finding.get("evidence", {})
        if evidence:
            parts.append("")
            parts.append("### Evidence")
            if isinstance(evidence, dict) and "columns" in evidence:
                parts.append(
                    _md_table(
                        evidence.get("columns", []),
                        evidence.get("rows", []),
                    )
                )
            else:
                parts.append(
                    f"```json\n"
                    f"{json.dumps(evidence, indent=2, default=str)}\n"
                    f"```"
                )

        recs = finding.get("recommendations", [])
        if recs:
            parts.append("")
            parts.append("### Recommendations")
            for i, rec in enumerate(recs, 1):
                desc_r = rec.get("description", "")
                parts.append(f"{i}. {desc_r}")
                cmd = rec.get("command", "")
                if cmd:
                    parts.append(f"   ```\n   {cmd}\n   ```")
                platform = rec.get("platform", "")
                if platform:
                    parts.append(f"   _(Platform: {platform})_")

        viz = finding.get("visualization")
        if viz:
            parts.append("")
            parts.append(_md_viz(viz))

    parts.append("")
    parts.append("---")
    return "\n".join(parts)

def _fmt_published_run_list(data: dict) -> str:
    items = data.get("items", [])
    if not items:
        return "No published runs found." + _cursor_line(data)

    lines = ["# Published Runs", ""]
    header = "| # | Run ID | Investigation ID | Title | Question |"
    sep = "| --- | --- | --- | --- | --- |"
    lines.extend([header, sep])
    for i, run in enumerate(items, 1):
        run_id = _esc(run.get("id", ""))
        inv_id = _esc(run.get("investigationId", ""))
        title = _esc(
            run.get("publishTitle", run.get("question", ""))
        )
        question = _esc(run.get("question", ""))
        lines.append(f"| {i} | {run_id} | {inv_id} | {title} | {question} |")
    lines.append(_cursor_line(data))
    return "\n".join(lines)

def _fmt_project_list(data: dict) -> str:
    items = data.get("items", [])
    if not items:
        return "No projects found." + _cursor_line(data)

    lines = ["# Projects", ""]
    header = "| Name | Description | ID |"
    sep = "| --- | --- | --- |"
    lines.extend([header, sep])
    for proj in items:
        name = _esc(proj.get("name", ""))
        desc = _esc(proj.get("description", ""))
        pid = _esc(proj.get("id", ""))
        lines.append(f"| {name} | {desc} | {pid} |")
    lines.append(_cursor_line(data))
    return "\n".join(lines)


def _fmt_project(data: dict) -> str:
    parts = [
        f"# Project: {data.get('name', '')}",
        "",
        _bullet("ID", data.get("id")),
        _bullet("Name", data.get("name")),
        _bullet("Description", data.get("description")),
    ]
    return "\n".join(parts)

def _fmt_follow_up(data: dict) -> str:
    parts = [
        "# Follow-up Created",
        "",
        _bullet("Run ID", data.get("id")),
        _bullet("Investigation", data.get("investigationId")),
        _bullet("Question", data.get("question")),
        _bullet("Status", data.get("status")),
    ]
    return "\n".join(parts)

_FORMATTERS: dict[str, Any] = {
    "list_investigations": _fmt_investigation_list,
    "get_investigation": _fmt_investigation,
    "create_investigation": _fmt_investigation,
    "get_run": _fmt_run,
    "stream_run": _fmt_run,
    "get_findings": _fmt_findings,
    "create_follow_up": _fmt_follow_up,
    "list_published_runs": _fmt_published_run_list,
    "list_projects": _fmt_project_list,
    "create_project": _fmt_project,
    "publish_run": _fmt_run,
    "unpublish_run": _fmt_run,
}


def format_markdown(name: str, data: Any) -> str:
    """Convert *data* to markdown using a formatter keyed by tool *name*."""
    formatter = _FORMATTERS.get(name)
    if formatter is None:
        return json.dumps(data, indent=2, default=str)
    return formatter(data)
