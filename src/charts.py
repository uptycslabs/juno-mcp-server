"""Chart rendering via matplotlib.

Converts Juno visualization payloads directly to base64-encoded PNG
images using matplotlib. No external binaries required.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

try:
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _HAS_MPL = True
except ImportError:
    np = None  # type: ignore[assignment]
    _HAS_MPL = False

logger = logging.getLogger("juno_mcp.charts")

_PALETTE = [
    "#6366f1", "#8b5cf6", "#a78bfa", "#818cf8", "#6ee7b7",
    "#34d399", "#fbbf24", "#f87171", "#60a5fa", "#c084fc",
]

_BG = "#1a1a2e"
_TEXT = "#e0e0e0"
_GRID = "#2a2a4a"
_AXIS = "#aaa"


def render_viz_b64(viz: dict) -> str | None:
    """Render a Juno visualization payload to a base64-encoded PNG.

    Returns ``None`` if rendering fails or matplotlib is not installed.
    """
    if not _HAS_MPL:
        logger.warning("matplotlib not installed — chart rendering disabled")
        return None

    intent = viz.get("intent", "")
    data = viz.get("data", [])
    if not data:
        return None

    schema = viz.get("schema") or {}
    title = viz.get("title", "")

    builders = {
        "show_trend_over_time": _line_chart,
        "highlight_anomalies": _anomaly_chart,
        "show_proportion": _pie_chart,
        "show_composition": _pie_chart,
        "show_heatmap": _heatmap_chart,
        "show_event_timeline": _timeline_chart,
    }
    builder = builders.get(intent, _bar_chart)
    return builder(title, data, schema)

def _new_fig(
    figsize: tuple[float, float] = (10, 6),
) -> tuple[Any, Any]:
    """Create a dark-themed figure + axes pair."""
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    ax.tick_params(colors=_AXIS)
    ax.xaxis.label.set_color(_AXIS)
    ax.yaxis.label.set_color(_AXIS)
    ax.title.set_color(_TEXT)
    for spine in ax.spines.values():
        spine.set_color(_GRID)
    return fig, ax


def _save_b64(fig: Any) -> str:
    """Save figure as PNG, close it, and return the base64 string."""
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", dpi=150, bbox_inches="tight",
        facecolor=fig.get_facecolor(),
    )
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")
    plt.close(fig)
    return b64


def _finalize(fig: Any, ax: Any, title: str, default: str) -> str:
    """Apply standard title/grid/formatter and save to base64 PNG."""
    ax.set_title(title or default, fontsize=13, pad=12)
    ax.grid(True, alpha=0.15, color=_GRID, axis="y")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(_fmt_tick))
    return _save_b64(fig)


def _add_legend(ax: Any) -> None:
    ax.legend(
        facecolor=_BG, edgecolor=_GRID,
        labelcolor=_TEXT, fontsize=9,
    )


def _set_cat_labels(
    ax: Any, labels: list[str], *, axis: str = "x",
) -> None:
    """Set category tick labels on the given axis."""
    rotation = 30 if max((len(lb) for lb in labels), default=0) > 10 else 0
    if axis == "x":
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(
            labels, rotation=rotation, ha="right",
            color=_AXIS, fontsize=9,
        )
    else:
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels, color=_AXIS, fontsize=9)


def _fmt_tick(x: float, _pos: object) -> str:
    """Human-friendly tick formatter (1K, 1.5M, etc.)."""
    abs_x = abs(x)
    if abs_x >= 1_000_000:
        v = x / 1_000_000
        return f"{v:.1f}".rstrip("0").rstrip(".") + "M"
    if abs_x >= 1_000:
        v = x / 1_000
        return f"{v:.1f}".rstrip("0").rstrip(".") + "K"
    if float(x).is_integer():
        return str(int(x))
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _detect_fields(
    first: dict, schema: dict, *, time: bool = False,
) -> tuple[str | None, list[str]]:
    """Detect category (or time) field and value fields.

    When *time* is True, looks for ``timeField`` instead of
    ``categoryField`` and auto-detects date-like strings.
    """
    if time:
        cat = schema.get("timeField") or schema.get("time_field")
    else:
        cat = (
            schema.get("categoryField")
            or schema.get("category_field")
            or schema.get("labelField")
            or schema.get("label_field")
        )

    vals = schema.get("valueFields") or schema.get("value_fields") or []
    if isinstance(vals, str):
        vals = [vals]

    if not cat:
        for k, v in first.items():
            if isinstance(v, str):
                if time and ("T" in v or "-" in v):
                    cat = k
                    break
                elif not time:
                    cat = k
                    break
    if not vals:
        vals = [
            k for k, v in first.items()
            if isinstance(v, (int, float)) and k != cat
        ]
    return cat, vals


def _bar_chart(title: str, data: list[dict], schema: dict) -> str | None:
    cat, vals = _detect_fields(data[0], schema)
    if not cat or not vals:
        return None

    categories = [str(r.get(cat, "")) for r in data]
    fig, ax = _new_fig()

    if len(vals) == 1:
        numbers = [r.get(vals[0], 0) for r in data]
        if len(numbers) > 7:
            ax.barh(range(len(numbers)), numbers, color=_PALETTE[0], height=0.6)
            _set_cat_labels(ax, categories, axis="y")
            ax.set_xlabel(vals[0].replace("_", " ").title(), fontsize=10)
        else:
            ax.bar(range(len(numbers)), numbers, color=_PALETTE[0], width=0.6)
            _set_cat_labels(ax, categories)
            ax.set_ylabel(vals[0].replace("_", " ").title(), fontsize=10)
    else:
        x = np.arange(len(categories))
        w = 0.8 / len(vals)
        for i, vf in enumerate(vals):
            ax.bar(
                x + w * i, [r.get(vf, 0) for r in data], w,
                color=_PALETTE[i % len(_PALETTE)],
                label=vf.replace("_", " ").title(),
            )
        ax.set_xticks(x + w * (len(vals) - 1) / 2)
        _set_cat_labels(ax, categories)
        _add_legend(ax)

    return _finalize(fig, ax, title, "Distribution")


def _line_chart(title: str, data: list[dict], schema: dict) -> str | None:
    time_f, vals = _detect_fields(data[0], schema, time=True)
    if not time_f or not vals:
        return None

    labels = [str(r.get(time_f, "")) for r in data]
    fig, ax = _new_fig()

    for i, vf in enumerate(vals):
        v = [r.get(vf, 0) for r in data]
        c = _PALETTE[i % len(_PALETTE)]
        ax.plot(range(len(v)), v, marker="o", linewidth=2, color=c,
                label=vf.replace("_", " ").title())
        ax.fill_between(range(len(v)), v, alpha=0.10, color=c)

    _set_cat_labels(ax, labels)
    ax.set_ylabel(vals[0].replace("_", " ").title(), fontsize=10)
    if len(vals) > 1:
        _add_legend(ax)
    return _finalize(fig, ax, title, "Trend Over Time")


def _anomaly_chart(title: str, data: list[dict], schema: dict) -> str | None:
    time_f, vals = _detect_fields(data[0], schema, time=True)
    if not time_f or not vals:
        return None

    vf = vals[0]
    labels = [str(r.get(time_f, "")) for r in data]
    values = [r.get(vf, 0) for r in data]
    fig, ax = _new_fig()

    ax.plot(range(len(values)), values, color=_PALETTE[0],
            linewidth=2, alpha=0.8, label="Baseline")

    if len(values) > 2:
        arr = np.array(values, dtype=float)
        mean, std = np.mean(arr), np.std(arr)
        if std > 0:
            idx = [i for i, v in enumerate(values) if abs(v - mean) > 1.5 * std]
            if idx:
                ax.scatter(idx, [values[i] for i in idx], color=_PALETTE[7],
                           s=120, zorder=5, label="Anomaly")

    _set_cat_labels(ax, labels)
    ax.set_ylabel(vf.replace("_", " ").title(), fontsize=10)
    _add_legend(ax)
    return _finalize(fig, ax, title, "Anomaly Detection")


def _pie_chart(title: str, data: list[dict], schema: dict) -> str | None:
    cat, vals = _detect_fields(data[0], schema)
    if not cat or not vals:
        return None

    labels = [str(r.get(cat, "")) for r in data]
    values = [r.get(vals[0], 0) for r in data]
    colors = [_PALETTE[i % len(_PALETTE)] for i in range(len(data))]

    fig, ax = _new_fig()
    _, texts, autotexts = ax.pie(
        values, labels=labels, colors=colors, autopct="%1.1f%%",
        startangle=140, pctdistance=0.8,
        wedgeprops={"edgecolor": _BG, "linewidth": 2},
    )
    for t in texts:
        t.set_color(_TEXT)
        t.set_fontsize(10)
    for t in autotexts:
        t.set_color(_TEXT)
        t.set_fontsize(9)

    ax.set_title(title or "Composition", fontsize=13, pad=12)
    return _save_b64(fig)


def _heatmap_chart(title: str, data: list[dict], schema: dict) -> str | None:
    x_f = schema.get("xAxisField") or schema.get("x_axis_field")
    y_f = schema.get("yAxisField") or schema.get("y_axis_field")
    vals = schema.get("valueFields") or schema.get("value_fields") or []
    if isinstance(vals, str):
        vals = [vals]
    if not x_f or not y_f or not vals:
        return None

    x_cats = sorted({str(r.get(x_f, "")) for r in data})
    y_cats = sorted({str(r.get(y_f, "")) for r in data})
    xi = {v: i for i, v in enumerate(x_cats)}
    yi = {v: i for i, v in enumerate(y_cats)}

    matrix = np.zeros((len(y_cats), len(x_cats)))
    for r in data:
        x = xi.get(str(r.get(x_f, "")))
        y = yi.get(str(r.get(y_f, "")))
        if x is not None and y is not None:
            matrix[y, x] = r.get(vals[0], 0)

    fw = min(max(10, len(x_cats) * 0.5 + 3), 20)
    fh = min(max(6, len(y_cats) * 0.6 + 2), 16)
    fig, ax = _new_fig(figsize=(fw, fh))

    im = ax.imshow(matrix, cmap="YlOrRd", aspect="auto", interpolation="nearest")
    cbar = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.ax.tick_params(labelsize=8, colors=_AXIS)

    _set_cat_labels(ax, x_cats)
    _set_cat_labels(ax, y_cats, axis="y")

    cell_pts = min(fw * 72 / max(len(x_cats), 1), fh * 72 / max(len(y_cats), 1))
    if cell_pts > 25:
        mx = np.nanmax(matrix) if matrix.size else 1
        fs = max(6, min(10, cell_pts / 4))
        for i in range(len(y_cats)):
            for j in range(len(x_cats)):
                v = matrix[i, j]
                tc = "white" if v > (mx / 2) else "black"
                if v >= 1e6:
                    t = f"{v / 1e6:.1f}M"
                elif v >= 1e3:
                    t = f"{v / 1e3:.0f}K"
                else:
                    t = str(int(v))
                ax.text(j, i, t, ha="center", va="center",
                        color=tc, fontsize=fs, fontweight="bold")

    ax.set_xlabel(x_f.replace("_", " ").title(), fontsize=11)
    ax.set_ylabel(y_f.replace("_", " ").title(), fontsize=11)
    ax.set_title(title or "Heatmap", fontsize=13, pad=15)
    ax.grid(False)
    return _save_b64(fig)


def _timeline_chart(title: str, data: list[dict], schema: dict) -> str | None:
    time_f = schema.get("timeField") or schema.get("time_field")
    label_f = schema.get("labelField") or schema.get("label_field")

    if not time_f or not label_f:
        first = data[0]
        for k, v in first.items():
            if not time_f and isinstance(v, str) and ("T" in v or ":" in v):
                time_f = k
            elif not label_f and isinstance(v, str) and "T" not in v:
                label_f = k
    if not time_f or not label_f:
        return None

    labels = [str(r.get(label_f, "")) for r in data]
    short = [lb[:50] + "..." if len(lb) > 50 else lb for lb in labels]
    times = [str(r.get(time_f, "")) for r in data]
    values = list(range(1, len(data) + 1))

    fig, ax = _new_fig(figsize=(10, max(4, len(data) * 0.5)))
    colors = [_PALETTE[i % len(_PALETTE)] for i in range(len(data))]
    ax.barh(range(len(values)), values, color=colors, height=0.6)

    _set_cat_labels(ax, short, axis="y")
    ax.invert_yaxis()
    for i, t in enumerate(times):
        ax.text(values[i] + 0.1, i, t, va="center", color=_AXIS, fontsize=8)

    ax.xaxis.set_visible(False)
    ax.set_title(title or "Event Timeline", fontsize=13, pad=12)
    ax.grid(True, alpha=0.08, axis="x", color=_GRID)
    return _save_b64(fig)
