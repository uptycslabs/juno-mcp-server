"""ECharts server-side rendering via Node.js subprocess.

Converts Juno visualization payloads into ECharts options, renders them
to SVG via the bundled ``echart_render.js`` script, and converts to PNG
using ``cairosvg``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("juno_mcp.charts")

_RENDER_JS = str(Path(__file__).parent / "echart_render.js")

# Uptycs brand-aligned color palette
_PALETTE = [
    "#6366f1",  # indigo
    "#8b5cf6",  # violet
    "#a78bfa",  # light violet
    "#818cf8",  # periwinkle
    "#6ee7b7",  # mint
    "#34d399",  # emerald
    "#fbbf24",  # amber
    "#f87171",  # red
    "#60a5fa",  # blue
    "#c084fc",  # purple
]


def render_png(option: dict, *, width: int = 600, height: int = 400) -> bytes | None:
    """Render an ECharts option dict to PNG bytes via Node.js SSR.

    Returns ``None`` if rendering fails (Node.js missing, etc.).
    """
    try:
        env = {**os.environ, "NODE_PATH": "/app/node_modules"}
        proc = subprocess.run(
            ["node", _RENDER_JS, str(width), str(height)],
            input=json.dumps(option),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        if proc.returncode != 0:
            logger.warning("ECharts render failed: %s", proc.stderr.strip())
            return None
        svg = proc.stdout
        if not svg:
            return None

        import cairosvg
        return cairosvg.svg2png(bytestring=svg.encode("utf-8"))
    except FileNotFoundError:
        logger.warning("Node.js not found — chart rendering disabled")
        return None
    except Exception:
        logger.warning("Chart rendering error", exc_info=True)
        return None


def render_png_b64(option: dict, **kwargs: Any) -> str | None:
    """Render and return base64-encoded PNG, or ``None`` on failure."""
    data = render_png(option, **kwargs)
    if data is None:
        return None
    return base64.standard_b64encode(data).decode("ascii")


# ------------------------------------------------------------------
# Viz → ECharts option builders
# ------------------------------------------------------------------

def viz_to_echart(viz: dict) -> dict | None:
    """Convert a Juno visualization payload to an ECharts option dict.

    Returns ``None`` if the visualization cannot be converted.
    """
    intent = viz.get("intent", "")
    data = viz.get("data", [])
    if not data:
        return None

    schema = viz.get("schema") or {}
    title = viz.get("title", "")

    if intent in ("show_trend_over_time", "highlight_anomalies"):
        return _line_chart(title, data, schema)
    if intent in ("show_proportion", "show_composition"):
        return _pie_chart(title, data, schema)
    if intent == "show_heatmap":
        return _heatmap_chart(title, data, schema)
    if intent == "show_event_timeline":
        return _timeline_chart(title, data, schema)
    return _bar_chart(title, data, schema)


def _detect_fields(
    first: dict, schema: dict,
) -> tuple[str | None, list[str]]:
    """Detect category and value fields from schema or first data row."""
    cat_field = (
        schema.get("categoryField")
        or schema.get("category_field")
        or schema.get("labelField")
        or schema.get("label_field")
    )
    val_fields = (
        schema.get("valueFields")
        or schema.get("value_fields")
        or []
    )
    if isinstance(val_fields, str):
        val_fields = [val_fields]

    if not cat_field:
        for k, v in first.items():
            if isinstance(v, str):
                cat_field = k
                break
    if not val_fields:
        val_fields = [
            k for k, v in first.items()
            if isinstance(v, (int, float)) and k != cat_field
        ]
    return cat_field, val_fields


def _base_option(title: str) -> dict:
    return {
        "backgroundColor": "#1a1a2e",
        "title": {
            "text": title,
            "left": "center",
            "textStyle": {"color": "#e0e0e0", "fontSize": 14},
        },
        "color": _PALETTE,
        "tooltip": {"trigger": "axis"},
        "grid": {
            "left": "8%", "right": "5%",
            "top": "18%", "bottom": "15%",
        },
    }


def _bar_chart(title: str, data: list[dict], schema: dict) -> dict | None:
    first = data[0]
    cat_field, val_fields = _detect_fields(first, schema)
    if not cat_field or not val_fields:
        return None

    categories = [str(r.get(cat_field, "")) for r in data]
    option = _base_option(title)
    option["xAxis"] = {
        "type": "category",
        "data": categories,
        "axisLabel": {
            "color": "#aaa",
            "rotate": 30 if max(len(c) for c in categories) > 10 else 0,
            "fontSize": 11,
        },
        "axisLine": {"lineStyle": {"color": "#444"}},
    }
    option["yAxis"] = {
        "type": "value",
        "axisLabel": {"color": "#aaa", "fontSize": 11},
        "splitLine": {"lineStyle": {"color": "#2a2a4a"}},
    }
    option["series"] = []
    for vf in val_fields:
        option["series"].append({
            "name": vf,
            "type": "bar",
            "data": [r.get(vf, 0) for r in data],
            "barMaxWidth": 40,
            "itemStyle": {"borderRadius": [4, 4, 0, 0]},
        })
    if len(val_fields) > 1:
        option["legend"] = {
            "data": val_fields,
            "textStyle": {"color": "#ccc"},
            "top": "5%",
        }
    return option


def _line_chart(title: str, data: list[dict], schema: dict) -> dict | None:
    time_field = schema.get("timeField") or schema.get("time_field")
    val_fields = schema.get("valueFields") or schema.get("value_fields") or []
    if isinstance(val_fields, str):
        val_fields = [val_fields]

    first = data[0]
    if not time_field:
        for k, v in first.items():
            if isinstance(v, str) and ("T" in str(v) or "-" in str(v)):
                time_field = k
                break
    if not val_fields:
        val_fields = [
            k for k, v in first.items()
            if isinstance(v, (int, float)) and k != time_field
        ]
    if not time_field or not val_fields:
        return None

    categories = [str(r.get(time_field, "")) for r in data]
    option = _base_option(title)
    option["xAxis"] = {
        "type": "category",
        "data": categories,
        "axisLabel": {"color": "#aaa", "fontSize": 11},
        "axisLine": {"lineStyle": {"color": "#444"}},
    }
    option["yAxis"] = {
        "type": "value",
        "axisLabel": {"color": "#aaa", "fontSize": 11},
        "splitLine": {"lineStyle": {"color": "#2a2a4a"}},
    }
    option["series"] = []
    for vf in val_fields:
        option["series"].append({
            "name": vf,
            "type": "line",
            "data": [r.get(vf, 0) for r in data],
            "smooth": True,
            "areaStyle": {"opacity": 0.15},
        })
    if len(val_fields) > 1:
        option["legend"] = {
            "data": val_fields,
            "textStyle": {"color": "#ccc"},
            "top": "5%",
        }
    return option


def _pie_chart(title: str, data: list[dict], schema: dict) -> dict | None:
    first = data[0]
    cat_field, val_fields = _detect_fields(first, schema)
    if not cat_field or not val_fields:
        return None

    option = _base_option(title)
    option.pop("grid", None)
    option["tooltip"] = {"trigger": "item"}
    option["series"] = [{
        "type": "pie",
        "radius": ["35%", "65%"],
        "center": ["50%", "55%"],
        "data": [
            {"name": str(r.get(cat_field, "")), "value": r.get(val_fields[0], 0)}
            for r in data
        ],
        "label": {"color": "#ccc", "fontSize": 11},
        "itemStyle": {"borderRadius": 6, "borderColor": "#1a1a2e", "borderWidth": 2},
    }]
    return option


def _heatmap_chart(title: str, data: list[dict], schema: dict) -> dict | None:
    x_field = schema.get("xAxisField") or schema.get("x_axis_field")
    y_field = schema.get("yAxisField") or schema.get("y_axis_field")
    val_fields = schema.get("valueFields") or schema.get("value_fields") or []
    if isinstance(val_fields, str):
        val_fields = [val_fields]

    if not x_field or not y_field or not val_fields:
        return None

    x_cats = sorted(set(str(r.get(x_field, "")) for r in data))
    y_cats = sorted(set(str(r.get(y_field, "")) for r in data))
    x_idx = {v: i for i, v in enumerate(x_cats)}
    y_idx = {v: i for i, v in enumerate(y_cats)}

    heat_data = []
    max_val = 0
    for r in data:
        xi = x_idx.get(str(r.get(x_field, "")))
        yi = y_idx.get(str(r.get(y_field, "")))
        val = r.get(val_fields[0], 0)
        if xi is not None and yi is not None:
            heat_data.append([xi, yi, val])
            max_val = max(max_val, val)

    option = _base_option(title)
    option["tooltip"] = {"position": "top"}
    option["grid"] = {
        "left": "15%", "right": "12%",
        "top": "18%", "bottom": "15%",
    }
    option["xAxis"] = {
        "type": "category",
        "data": x_cats,
        "axisLabel": {"color": "#aaa", "fontSize": 11, "rotate": 30},
        "splitArea": {"show": True},
    }
    option["yAxis"] = {
        "type": "category",
        "data": y_cats,
        "axisLabel": {"color": "#aaa", "fontSize": 11},
        "splitArea": {"show": True},
    }
    option["visualMap"] = {
        "min": 0,
        "max": max_val or 1,
        "calculable": True,
        "orient": "vertical",
        "right": "2%",
        "top": "center",
        "inRange": {"color": ["#1a1a2e", "#6366f1", "#f87171"]},
        "textStyle": {"color": "#aaa"},
    }
    option["series"] = [{
        "type": "heatmap",
        "data": heat_data,
        "label": {"show": True, "color": "#e0e0e0", "fontSize": 10},
        "itemStyle": {"borderWidth": 2, "borderColor": "#1a1a2e"},
    }]
    return option


def _timeline_chart(title: str, data: list[dict], schema: dict) -> dict | None:
    time_field = schema.get("timeField") or schema.get("time_field")
    label_field = schema.get("labelField") or schema.get("label_field")

    if not time_field or not label_field:
        # Try to auto-detect
        first = data[0]
        for k, v in first.items():
            if not time_field and isinstance(v, str) and ("T" in v or ":" in v):
                time_field = k
            elif not label_field and isinstance(v, str) and "T" not in v:
                label_field = k
    if not time_field or not label_field:
        return None

    labels = [str(r.get(label_field, "")) for r in data]
    # Shorten labels for display
    short_labels = [l[:50] + "..." if len(l) > 50 else l for l in labels]
    times = [str(r.get(time_field, "")) for r in data]
    # Use index as value (uniform bar lengths for timeline)
    values = list(range(1, len(data) + 1))

    option = _base_option(title)
    option["tooltip"] = {
        "trigger": "axis",
        "axisPointer": {"type": "shadow"},
        "formatter": None,  # will show label + time
    }
    option["grid"] = {
        "left": "35%", "right": "5%",
        "top": "18%", "bottom": "10%",
    }
    option["xAxis"] = {
        "type": "value",
        "show": False,
    }
    option["yAxis"] = {
        "type": "category",
        "data": short_labels,
        "inverse": True,
        "axisLabel": {"color": "#ccc", "fontSize": 10, "width": 200,
                      "overflow": "truncate"},
        "axisLine": {"lineStyle": {"color": "#444"}},
    }
    option["series"] = [{
        "type": "bar",
        "data": values,
        "barMaxWidth": 20,
        "itemStyle": {"borderRadius": [0, 4, 4, 0]},
        "label": {
            "show": True,
            "position": "right",
            "formatter": None,
            "color": "#aaa",
            "fontSize": 9,
        },
    }]
    # Encode times as custom data for tooltip
    for i, t in enumerate(times):
        option["series"][0]["data"][i] = {
            "value": values[i],
            "name": t,
        }
    return option
