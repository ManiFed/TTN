#!/usr/bin/env python3
"""
Metric aggregation for digital-twin runs.

Two layers:

  summarize_run(result)          one scheduler × one world → summary dict
  aggregate(summaries)           the same scheduler across seed replicates →
                                 mean / sd / min / max per metric

Metric-fairness note: `realized_value` uses CHORUS's cell-value currency (it
is the only network-wide definition of "science captured" the project has),
but every headline comparison also carries scheduler-agnostic counts —
accepted measurements, distinct targets, alert latency, transit coverage,
wasted minutes — so a reader can judge CHORUS without trusting its own
objective function.
"""

import math
import statistics
from typing import Optional


def _gini(values: list) -> float:
    """Load inequality across nodes (0 = perfectly even, →1 = one node does
    everything).  Computed over nodes with any planned work potential."""
    vals = sorted(max(0.0, float(v)) for v in values)
    n = len(vals)
    total = sum(vals)
    if n == 0 or total <= 0:
        return 0.0
    cum = 0.0
    for i, v in enumerate(vals, start=1):
        cum += i * v
    return (2.0 * cum) / (n * total) - (n + 1.0) / n


def summarize_run(result: dict) -> dict:
    """Collapse one run's per-night records into scalar metrics."""
    nights = result["nights"]
    n = max(1, len(nights))

    def per_night(key):
        return sum(rec.get(key, 0) or 0 for rec in nights) / n

    accepted = sum(rec["n_accepted"] for rec in nights)
    planned = sum(rec["n_planned"] for rec in nights)
    executed = sum(rec["n_executed"] for rec in nights)
    planned_min = sum(rec["planned_minutes"] for rec in nights)
    wasted_min = sum(rec["wasted_minutes"] for rec in nights)
    value = sum(rec["realized_value"] for rec in nights)
    schedulable = sum(rec["schedulable_value"] for rec in nights)
    exp_deliv = sum(rec["expected_deliveries"] for rec in nights)
    transit_events = sum(rec["transit_events"] for rec in nights)
    transit_covered = sum(rec["transit_covered"] for rec in nights)

    by_class: dict = {}
    for rec in nights:
        for cls, cnt in rec.get("accepted_by_class", {}).items():
            by_class[cls] = by_class.get(cls, 0) + cnt

    alerts = result.get("alerts", [])
    latencies = [a["latency_h"] for a in alerts if a["latency_h"] is not None]
    n_alerts = len(alerts)

    return {
        "scenario": result["scenario"],
        "scheduler": result["scheduler"],
        "seed": result["seed"],
        "n_nodes": result["n_nodes"],
        "n_nights": len(nights),
        # volume
        "planned_per_night": round(planned / n, 2),
        "executed_per_night": round(executed / n, 2),
        "accepted_per_night": round(accepted / n, 2),
        "aavso_per_month": round(30.0 * accepted / n, 1),
        # efficiency
        "acceptance_rate": round(accepted / planned, 4) if planned else 0.0,
        "expected_deliveries_per_night": round(exp_deliv / n, 2),
        "calibration_ratio": (round(executed / exp_deliv, 3)
                              if exp_deliv > 0 else None),
        "wasted_minutes_per_night": round(wasted_min / n, 1),
        "wasted_time_frac": (round(wasted_min / planned_min, 4)
                             if planned_min else 0.0),
        # science
        "realized_value_per_night": round(value / n, 3),
        "value_capture_frac": (round(value / schedulable, 4)
                               if schedulable else 0.0),
        "distinct_targets_per_night": round(per_night("distinct_targets"), 2),
        "redundancy_rate": (round(planned / max(1e-9, sum(
            rec["distinct_targets"] for rec in nights)), 3)
            if nights else 0.0),
        "accepted_by_class": by_class,
        # time-critical
        "n_alerts": n_alerts,
        "alerts_responded": len(latencies),
        "alert_response_frac": (round(len(latencies) / n_alerts, 3)
                                if n_alerts else None),
        "alert_latency_median_h": (round(statistics.median(latencies), 1)
                                   if latencies else None),
        # transits
        "transit_events": transit_events,
        "transit_covered": transit_covered,
        "transit_coverage_frac": (round(transit_covered / transit_events, 3)
                                  if transit_events else None),
        "transit_mean_coverage": round(per_night("transit_mean_coverage"), 3),
        # network shape
        "utc_hours_covered_per_night": round(per_night("utc_hours_covered"), 2),
        "node_load_gini": round(_gini(list(result["node_accepted"].values())), 3),
        "mean_clear_frac": round(per_night("mean_clear_frac"), 3),
    }


_AGG_SKIP = {"scenario", "scheduler", "seed", "accepted_by_class"}


def aggregate(summaries: list) -> dict:
    """Mean ± sd (and min/max) across seed replicates of one scheduler."""
    if not summaries:
        return {}
    out = {
        "scenario": summaries[0]["scenario"],
        "scheduler": summaries[0]["scheduler"],
        "n_replicates": len(summaries),
        "seeds": [s["seed"] for s in summaries],
    }
    keys = [k for k in summaries[0] if k not in _AGG_SKIP]
    for k in keys:
        vals = [s[k] for s in summaries if isinstance(s.get(k), (int, float))]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        out[k] = {"mean": round(mean, 3), "sd": round(sd, 3),
                  "min": round(min(vals), 3), "max": round(max(vals), 3)}
    # class counts: mean per class
    classes: dict = {}
    for s in summaries:
        for cls, cnt in s.get("accepted_by_class", {}).items():
            classes.setdefault(cls, []).append(cnt)
    out["accepted_by_class_mean"] = {
        cls: round(sum(v) / len(summaries), 1) for cls, v in classes.items()}
    return out


def comparison_table(aggregates: list, metrics: Optional[list] = None) -> str:
    """Markdown table: schedulers as rows, key metrics as columns
    (mean ± sd across replicates)."""
    metrics = metrics or [
        "accepted_per_night", "realized_value_per_night",
        "value_capture_frac", "wasted_minutes_per_night",
        "alert_latency_median_h", "transit_coverage_frac",
        "utc_hours_covered_per_night", "node_load_gini", "redundancy_rate",
    ]
    header = "| scheduler | " + " | ".join(metrics) + " |"
    sep = "|" + "---|" * (len(metrics) + 1)
    rows = []
    for agg in aggregates:
        cells = [agg["scheduler"]]
        for m in metrics:
            v = agg.get(m)
            if isinstance(v, dict):
                cells.append(f"{v['mean']:g} ± {v['sd']:g}")
            elif v is None:
                cells.append("—")
            else:
                cells.append(f"{v}")
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([header, sep] + rows)
