"""
Matplotlib weekly report chart generators (Section 6.8).

Renders to in-memory PNG bytes (`io.BytesIO`) with accessible colors, readable fonts,
and labels in the user's language ('uz' or 'en'):
1. `render_weekly_stacked_chart`: Stacked bar chart of hours per day (Mon-Sun) by activity.
2. `render_weekly_targets_chart`: Horizontal bar chart of weekly actual hours vs weekly targets.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Accessible colorblind-safe palette (Okabe-Ito inspired)
PALETTE = [
    "#2563EB",  # Blue
    "#10B981",  # Emerald
    "#F59E0B",  # Amber
    "#EC4899",  # Pink
    "#8B5CF6",  # Purple
    "#06B6D4",  # Cyan
    "#F97316",  # Orange
    "#64748B",  # Slate
]

DAY_LABELS = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "uz": ["Du", "Se", "Chor", "Pay", "Ju", "Shan", "Yak"],
}


def render_weekly_stacked_chart(
    days: Sequence[date],
    activity_daily_minutes: dict[str, list[float]],
    lang: str = "uz",
) -> bytes:
    """
    Render chart (1): Stacked bar of hours per day by activity.
    `activity_daily_minutes` maps `activity_name -> [mon_mins, tue_mins, ..., sun_mins]`.
    Returns PNG bytes in memory.
    """
    lang_key = "uz" if lang == "uz" else "en"
    labels = DAY_LABELS[lang_key][: len(days)]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=140)
    bottoms = [0.0] * len(days)

    if not activity_daily_minutes:
        ax.bar(labels, bottoms, color="#CBD5E1")
    else:
        for idx, (act_name, mins_list) in enumerate(activity_daily_minutes.items()):
            hours_list = [round(m / 60.0, 2) for m in mins_list]
            color = PALETTE[idx % len(PALETTE)]
            ax.bar(labels, hours_list, bottom=bottoms, label=act_name, color=color, width=0.55)
            bottoms = [b + h for b, h in zip(bottoms, hours_list)]

    title = (
        "Kunlar bo'yicha sarflangan soatlar"
        if lang_key == "uz"
        else "Hours per Day by Activity"
    )
    ylabel = "Soat" if lang_key == "uz" else "Hours"

    ax.set_title(title, fontsize=12, fontweight="bold", pad=12)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.grid(axis="y", linestyle="--", alpha=0.35)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    if activity_daily_minutes:
        ax.legend(loc="upper right", frameon=True, fontsize=8)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def render_weekly_targets_chart(
    items: Sequence[tuple[str, float, float]],
    lang: str = "uz",
) -> bytes:
    """
    Render chart (2): Horizontal bar of weekly totals vs weekly targets.
    `items` is a list of `(activity_name, actual_minutes, target_minutes)`.
    Returns PNG bytes in memory.
    """
    lang_key = "uz" if lang == "uz" else "en"
    fig, ax = plt.subplots(figsize=(8, max(3.8, 0.7 * len(items) + 1.5)), dpi=140)

    if not items:
        names = ["—"]
        actual_h = [0.0]
        target_h = [0.0]
    else:
        names = [it[0] for it in items]
        actual_h = [round(it[1] / 60.0, 2) for it in items]
        target_h = [round(it[2] / 60.0, 2) for it in items]

    y_pos = list(range(len(names)))
    bar_h = 0.36

    lbl_actual = "Haqiqiy (soat)" if lang_key == "uz" else "Actual (h)"
    lbl_target = "Maqsad (soat)" if lang_key == "uz" else "Target (h)"

    ax.barh(
        [y + bar_h / 2 for y in y_pos],
        actual_h,
        height=bar_h,
        label=lbl_actual,
        color="#2563EB",
    )
    ax.barh(
        [y - bar_h / 2 for y in y_pos],
        target_h,
        height=bar_h,
        label=lbl_target,
        color="#94A3B8",
    )

    ax.set_yticks(y_pos)
    ax.set_yticklabels(names, fontsize=9)
    title = (
        "Haftalik natija va haftalik maqsad"
        if lang_key == "uz"
        else "Weekly Totals vs. Weekly Targets"
    )
    xlabel = "Soat" if lang_key == "uz" else "Hours"
    ax.set_title(title, fontsize=12, fontweight="bold", pad=12)
    ax.set_xlabel(xlabel, fontsize=10)
    ax.grid(axis="x", linestyle="--", alpha=0.35)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="lower right", frameon=True, fontsize=8)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
