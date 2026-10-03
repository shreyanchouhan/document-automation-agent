"""Extraction drift monitoring.

The alarm that matters in production is not "the model is wrong" - nobody has
labels at run time - it is "the model is behaving differently from the batch we
accepted". So the baseline is a distribution of per-field confidence plus the
null rate and the auto-post rate, and each batch is scored against it with the
population stability index.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from docagent.config import ALL_FIELDS, Config
from docagent.store import Store

# Bin edges sit on the decision thresholds, so a shift across a boundary is a
# shift that changes routing rather than one that only moves the histogram.
CONFIDENCE_BINS = [0.0, 0.5, 0.80, 0.92, 0.97, 1.01]
PSI_EPSILON = 1e-4
PSI_WATCH = 0.1
# PSI alone fires on a distribution that moved for the better. An alert also has
# to show more readings falling under the routing floor than the baseline had.
ADVERSE_SHARE_INCREASE = 0.05


@dataclass
class DriftAlert:
    kind: str
    severity: str
    field: str | None
    metric: float
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def histogram(values: list[float]) -> list[float]:
    """Share of observations per confidence bin."""
    counts = [0] * (len(CONFIDENCE_BINS) - 1)
    for value in values:
        for index in range(len(counts)):
            if CONFIDENCE_BINS[index] <= value < CONFIDENCE_BINS[index + 1]:
                counts[index] += 1
                break
    total = sum(counts) or 1
    return [count / total for count in counts]


def population_stability_index(baseline: list[float], current: list[float]) -> float:
    import math

    total = 0.0
    for expected, actual in zip(baseline, current):
        expected = max(expected, PSI_EPSILON)
        actual = max(actual, PSI_EPSILON)
        total += (actual - expected) * math.log(actual / expected)
    return round(total, 4)


def snapshot(store: Store, batch_ids: list[str], config: Config) -> dict:
    """Summarise one or more batches into a comparable profile."""
    fields: dict[str, dict] = {}
    for name in ALL_FIELDS:
        values: list[float] = []
        nulls = 0
        for batch_id in batch_ids:
            for row in store.connection.execute(
                "SELECT confidence, has_value FROM field_confidence WHERE batch_id = ? AND field = ?",
                (batch_id, name),
            ):
                values.append(row["confidence"])
                nulls += 0 if row["has_value"] else 1
        if not values:
            continue
        below_floor = sum(1 for value in values if value < config.field_confidence_floor)
        fields[name] = {
            "histogram": histogram(values),
            "mean_confidence": round(sum(values) / len(values), 4),
            "null_rate": round(nulls / len(values), 4),
            "below_floor_rate": round(below_floor / len(values), 4),
            "observations": len(values),
        }

    summaries = [store.batch_summary(batch_id) for batch_id in batch_ids]
    documents = sum(summary["documents"] for summary in summaries)
    auto_posted = sum(summary["auto_post"] for summary in summaries)
    return {
        "batch_ids": batch_ids,
        "documents": documents,
        "auto_post_rate": round(auto_posted / documents, 4) if documents else 0.0,
        "fields": fields,
    }


def save_baseline(profile: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2))
    return path


def load_baseline(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def compare(baseline: dict, current: dict, config: Config) -> list[DriftAlert]:
    alerts: list[DriftAlert] = []

    for name, current_field in current["fields"].items():
        baseline_field = baseline["fields"].get(name)
        if not baseline_field:
            continue
        psi = population_stability_index(baseline_field["histogram"], current_field["histogram"])
        adverse = current_field.get("below_floor_rate", 0.0) - baseline_field.get("below_floor_rate", 0.0)

        if psi >= config.drift_psi_alert and adverse >= ADVERSE_SHARE_INCREASE:
            alerts.append(
                DriftAlert(
                    kind="confidence_drift",
                    severity="critical" if psi >= 2 * config.drift_psi_alert else "warning",
                    field=name,
                    metric=psi,
                    detail=(
                        f"{name} confidence shifted down (PSI {psi:.3f}); readings under the "
                        f"{config.field_confidence_floor:.2f} floor rose from "
                        f"{baseline_field['below_floor_rate']:.0%} to {current_field['below_floor_rate']:.0%}, "
                        f"mean {baseline_field['mean_confidence']:.3f} → {current_field['mean_confidence']:.3f}"
                    ),
                )
            )
        elif psi >= config.drift_psi_alert:
            alerts.append(
                DriftAlert(
                    kind="confidence_shift",
                    severity="info",
                    field=name,
                    metric=psi,
                    detail=(
                        f"{name} distribution moved (PSI {psi:.3f}) without more low-confidence "
                        f"reads; mean {baseline_field['mean_confidence']:.3f} → "
                        f"{current_field['mean_confidence']:.3f}"
                    ),
                )
            )
        elif psi >= PSI_WATCH and adverse > 0:
            alerts.append(
                DriftAlert(
                    kind="confidence_drift",
                    severity="info",
                    field=name,
                    metric=psi,
                    detail=f"{name} confidence PSI {psi:.3f} is in the watch band",
                )
            )

        null_increase = current_field["null_rate"] - baseline_field["null_rate"]
        if null_increase >= 0.10:
            alerts.append(
                DriftAlert(
                    kind="null_rate_drift",
                    severity="warning",
                    field=name,
                    metric=round(null_increase, 4),
                    detail=(
                        f"{name} is unreadable on {current_field['null_rate']:.0%} of pages, "
                        f"up from {baseline_field['null_rate']:.0%}"
                    ),
                )
            )

    rate_drop = baseline["auto_post_rate"] - current["auto_post_rate"]
    if rate_drop >= config.auto_post_rate_alert_drop:
        alerts.append(
            DriftAlert(
                kind="auto_post_rate_drop",
                severity="critical",
                field=None,
                metric=round(rate_drop, 4),
                detail=(
                    f"auto-post rate fell from {baseline['auto_post_rate']:.0%} to "
                    f"{current['auto_post_rate']:.0%} ({rate_drop:.0%} drop); the review queue "
                    "will grow at this rate"
                ),
            )
        )
    return alerts


def monitor_batch(store: Store, batch_id: str, config: Config) -> list[DriftAlert]:
    """Score one batch against the saved baseline and persist whatever it finds."""
    baseline = load_baseline(config.baseline_path)
    if baseline is None:
        return []
    alerts = compare(baseline, snapshot(store, [batch_id], config), config)
    for alert in alerts:
        store.record_alert(batch_id, alert.kind, alert.severity, alert.detail, alert.metric)
    store.commit()
    return alerts
