"""Tool registry — all Juno MCP tools in one place.

Handlers, schemas, and markdown formatting.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from mcp.types import ImageContent, TextContent, Tool

from .client import JunoClient
from .charts import render_viz_b64

logger = logging.getLogger("juno_mcp.tools")

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
    """Raise ValueError if any keys in args are not valid UUIDs."""
    for key in keys:
        value = args.get(key)
        if value is not None and not _UUID_RE.match(str(value)):
            raise ValueError(
                f"'{key}' must be a plain UUID "
                f"(e.g. 684ff06a-1234-5678-9abc-def012345678), "
                f"got: {value!r}"
            )


def _inject_url(client: JunoClient, data: dict, key: str = "id") -> None:
    """Add uptycsConsoleUrl pointing to the Uptycs console."""
    inv_id = data.get(key)
    if inv_id:
        data["uptycsConsoleUrl"] = client.console_url(str(inv_id))


_INVESTIGATION_EXCLUDE_KEYS: set[str] = {
    "customerId",
    "ownerId",
    "tasks",
    "findings",
    "summarySections",
    "suggestedPrompts",
}


def _strip_keys(obj: Any) -> Any:
    """Recursively remove _INVESTIGATION_EXCLUDE_KEYS from dicts/lists."""
    if isinstance(obj, dict):
        return {k: _strip_keys(v) for k, v in obj.items() if k not in _INVESTIGATION_EXCLUDE_KEYS}
    if isinstance(obj, list):
        return [_strip_keys(item) for item in obj]
    return obj


_RESPONSE_FORMAT = os.environ.get("JUNO_RESPONSE_FORMAT", "json").lower()

# Chart images accumulated by _md_viz(), consumed by dispatch().
_pending_images: list[ImageContent] = []


async def _list_investigations(client: JunoClient, args: dict[str, Any]) -> str:
    data = await client.list_investigations(
        search=args.get("search"),
        limit=args.get("limit", 5),
        cursor=args.get("cursor"),
    )
    for item in data.get("items", []):
        _inject_url(client, item)
    data = _strip_keys(data)
    return json.dumps(data, indent=2, default=str)


async def _get_investigation(client: JunoClient, args: dict[str, Any]) -> str:
    _validate_uuid(args, "investigation_id")
    data = await client.get_investigation(args["investigation_id"])
    _inject_url(client, data)
    data = _strip_keys(data)
    return json.dumps(data, indent=2, default=str)


async def _create_investigation(client: JunoClient, args: dict[str, Any]) -> str:
    data = await client.create_investigation(
        question=args["question"],
    )
    _inject_url(client, data)
    return json.dumps(data, indent=2, default=str)


async def _delete_investigation(client: JunoClient, args: dict[str, Any]) -> str:
    _validate_uuid(args, "investigation_id")
    await client.delete_investigation(args["investigation_id"])
    return f"Investigation {args['investigation_id']} deleted."


async def _get_run(client: JunoClient, args: dict[str, Any]) -> str:
    _validate_uuid(args, "investigation_id", "run_id")
    data = await client.get_run(args["investigation_id"], args["run_id"])
    _inject_url(client, data, "investigationId")
    return json.dumps(data, indent=2, default=str)


async def _create_follow_up(client: JunoClient, args: dict[str, Any]) -> str:
    _validate_uuid(args, "investigation_id", "parent_run_id")
    data = await client.create_follow_up(
        investigation_id=args["investigation_id"],
        parent_run_id=args["parent_run_id"],
        question=args["question"],
    )
    _inject_url(client, data, "investigationId")
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
    for item in data.get("items", []):
        inv_id = item.get("investigationId")
        run_id = item.get("id")
        if inv_id and run_id:
            item["uptycsConsoleUrl"] = client.console_url(str(inv_id), str(run_id))
    return json.dumps(data, indent=2, default=str)



_INVESTIGATION_ID_PROP = {
    "investigation_id": {
        "type": "string",
        "description": "Investigation UUID — plain format, no prefix",
    }
}

_RUN_ID_PROP = {
    "run_id": {
        "type": "string",
        "description": "Run UUID — plain format, no prefix",
    }
}

_PAGINATION_PROPS = {
    "limit": {
        "type": "integer",
        "description": "Maximum number of results to return (1-50)",
    },
    "cursor": {
        "type": "string",
        "description": "Pagination cursor from a previous response's nextCursor field",
    },
}


_ALL_TOOLS: list[ToolDef] = [
    ToolDef(
        name="list_investigations",
        description=(
            "List recent Juno investigations (most recent first). "
            "Returns id, title, question, uptycsConsoleUrl per item. "
            "Supports keyword search and pagination via cursor."
        ),
        schema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Filter by keyword (matches title and question)"},
                "limit": {"type": "integer", "default": 5, "description": "Max results (1-50)"},
                "cursor": _PAGINATION_PROPS["cursor"],
            },
        },
        handler=_list_investigations,
    ),
    ToolDef(
        name="get_investigation",
        description=(
            "Get investigation metadata and run inventory (id, status, timestamps per run). "
            "Does NOT return findings, summaries, or evidence — use get_run for full run content."
        ),
        schema={
            "type": "object",
            "properties": _INVESTIGATION_ID_PROP,
            "required": ["investigation_id"],
        },
        handler=_get_investigation,
    ),
    ToolDef(
        name="create_investigation",
        description=(
            "Start a new Juno AI investigation. "
            "Returns the investigation with its first run (initially pending/running). "
            "Poll with get_run until completed or failed."
        ),
        schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "Security question to investigate, e.g. "
                        "'Are there privilege escalation attempts in the last 24 hours?'"
                    ),
                },
            },
            "required": ["question"],
        },
        handler=_create_investigation,
    ),
    ToolDef(
        name="delete_investigation",
        description=(
            "Permanently delete an investigation and all its runs. Cannot be undone."
        ),
        schema={
            "type": "object",
            "properties": _INVESTIGATION_ID_PROP,
            "required": ["investigation_id"],
        },
        handler=_delete_investigation,
    ),
    ToolDef(
        name="get_run",
        description=(
            "Get full run content: summarySections, findings (with severity, evidence, recommendations), "
            "tasks, and suggestedPrompts. Also used to poll status after create_investigation or create_follow_up."
        ),
        schema={
            "type": "object",
            "properties": {**_INVESTIGATION_ID_PROP, **_RUN_ID_PROP},
            "required": ["investigation_id", "run_id"],
        },
        handler=_get_run,
    ),
    ToolDef(
        name="create_follow_up",
        description=(
            "Ask a follow-up question on a completed run. Inherits full parent context for deeper analysis. "
            "Parent run MUST be completed or failed first — only one run per investigation can execute at a time. "
            "Calling while a run is active will fail. "
            "Returns a new run — poll with get_run until completed."
        ),
        schema={
            "type": "object",
            "properties": {
                **_INVESTIGATION_ID_PROP,
                "parent_run_id": {
                    "type": "string",
                    "description": "Completed run UUID to follow up on — plain format, no prefix",
                },
                "question": {
                    "type": "string",
                    "description": "Follow-up question building on the parent run's context",
                },
            },
            "required": ["investigation_id", "parent_run_id", "question"],
        },
        handler=_create_follow_up,
    ),
    ToolDef(
        name="publish_run",
        description="Publish a completed run to make it visible to all team members. Only when user explicitly asks.",
        schema={
            "type": "object",
            "properties": {**_INVESTIGATION_ID_PROP, **_RUN_ID_PROP},
            "required": ["investigation_id", "run_id"],
        },
        handler=_publish_run,
    ),
    ToolDef(
        name="unpublish_run",
        description="Remove a run from the team-visible published list. Only when user explicitly asks.",
        schema={
            "type": "object",
            "properties": {**_INVESTIGATION_ID_PROP, **_RUN_ID_PROP},
            "required": ["investigation_id", "run_id"],
        },
        handler=_unpublish_run,
    ),
    ToolDef(
        name="list_published_runs",
        description=(
            "List runs published and shared with the team. "
            "Returns id, investigationId, question, publishTitle, publishSummary per item. "
            "Supports keyword search and pagination."
        ),
        schema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Filter by keyword"},
                "limit": {"type": "integer", "default": 5, "description": "Max results (1-50)"},
                "cursor": _PAGINATION_PROPS["cursor"],
            },
        },
        handler=_list_published_runs,
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
) -> list[TextContent | ImageContent]:
    """Dispatch a tool call by name. Raises KeyError for unknown tools."""
    td = _HANDLERS[name]
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
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return result
    return format_markdown(name, data)


def _esc(value: Any) -> str:
    s = str(value) if value is not None else ""
    return s.replace("|", "\\|").replace("\n", " ")


def _bullet(label: str, value: Any, fallback: str = "—") -> str:
    return f"- **{label}**: {value if value else fallback}"


def _md_table(columns: list[str], rows: list[dict]) -> str:
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
    parts: list[str] = []
    if ref.get("description"):
        parts.append(ref["description"])
    tbl = _md_table(ref.get("columns", []), ref.get("rows", []))
    if tbl:
        parts.append(tbl)
    if ref.get("truncated"):
        parts.append(f"_Showing {len(ref.get('rows', []))} of {ref.get('total_rows', '?')} rows_")
    return "\n\n".join(parts)


def _md_viz(viz: dict) -> str:
    parts: list[str] = []
    title = viz.get("title", "Visualization")
    parts.append(f"**{title}**")
    if viz.get("description"):
        parts.append(viz["description"])

    mermaid_code = (viz.get("schema") or {}).get("mermaidCode", "")
    if mermaid_code:
        elk = '%%{init: {"flowchart": {"defaultRenderer": "elk"}} }%%'
        parts.append(f"```mermaid\n{elk}\n{mermaid_code}\n```")

    b64 = render_viz_b64(viz)
    if b64:
        _pending_images.append(ImageContent(type="image", data=b64, mimeType="image/png"))

    data = viz.get("data", [])
    if data:
        parts.append(_md_table(list(data[0].keys()), data))
        if viz.get("truncated"):
            parts.append(f"_Showing {len(data)} of {viz.get('total_rows', '?')} rows_")
    return "\n\n".join(parts)


def _cursor_line(data: dict) -> str:
    cursor = data.get("nextCursor", "")
    if cursor:
        return f"\n_More results available (cursor: `{cursor}`). Only fetch more if the user explicitly asks._"
    return ""


def _append_summary_sections(parts: list[str], data: dict) -> None:
    sections = data.get("summarySections", [])
    if not sections:
        return
    parts.append("")
    parts.append("## Summary")
    for sec in sections:
        parts.append("")
        parts.append(f"### {sec.get('title', 'Section')}")
        if sec.get("content"):
            parts.append(sec["content"])
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


def _fmt_investigation_list(data: dict) -> str:
    items = data.get("items", [])
    if not items:
        return "No investigations found." + _cursor_line(data)
    lines = ["# Investigations", "", "| # | ID | Title | Question |", "| --- | --- | --- | --- |"]
    for i, inv in enumerate(items, 1):
        lines.append(f"| {i} | {_esc(inv.get('id', ''))} | {_esc(inv.get('title', inv.get('question', '')))} | {_esc(inv.get('question', ''))} |")
    lines.append(_cursor_line(data))
    return "\n".join(lines)


def _fmt_investigation(data: dict) -> str:
    parts: list[str] = []
    parts.append(f"# {data.get('title', data.get('question', 'Investigation'))}")
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
            # Note: runs here are metadata-only (from get_investigation).
            # summarySections and findings are not present — use get_run for full content.
    return "\n".join(parts)


def _fmt_run(data: dict) -> str:
    parts: list[str] = [
        f"# Run: {data.get('id', '')}",
        "",
        _bullet("Status", data.get("status")),
        _bullet("Investigation", data.get("investigationId")),
        _bullet("Question", data.get("question")),
    ]
    if data.get("errorMessage"):
        parts.append(_bullet("Error", data["errorMessage"]))
    _append_summary_sections(parts, data)
    _append_suggested_prompts(parts, data)
    return "\n".join(parts)


def _fmt_published_run_list(data: dict) -> str:
    items = data.get("items", [])
    if not items:
        return "No published runs found." + _cursor_line(data)
    lines = [
        "# Published Runs", "",
        "| # | Run ID | Investigation ID | Title | Question |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, run in enumerate(items, 1):
        lines.append(
            f"| {i} | {_esc(run.get('id', ''))} | {_esc(run.get('investigationId', ''))} "
            f"| {_esc(run.get('publishTitle', run.get('question', '')))} | {_esc(run.get('question', ''))} |"
        )
    lines.append(_cursor_line(data))
    return "\n".join(lines)



def _fmt_follow_up(data: dict) -> str:
    return "\n".join([
        "# Follow-up Created",
        "",
        _bullet("Run ID", data.get("id")),
        _bullet("Investigation", data.get("investigationId")),
        _bullet("Question", data.get("question")),
        _bullet("Status", data.get("status")),
    ])


_FORMATTERS: dict[str, Any] = {
    "list_investigations": _fmt_investigation_list,
    "get_investigation": _fmt_investigation,
    "create_investigation": _fmt_investigation,
    "get_run": _fmt_run,
    "create_follow_up": _fmt_follow_up,
    "list_published_runs": _fmt_published_run_list,
    "publish_run": _fmt_run,
    "unpublish_run": _fmt_run,
}


def format_markdown(name: str, data: Any) -> str:
    """Convert data to markdown using a formatter keyed by tool name."""
    formatter = _FORMATTERS.get(name)
    if formatter is None:
        return json.dumps(data, indent=2, default=str)
    return formatter(data)
