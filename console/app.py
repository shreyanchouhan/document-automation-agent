"""Human review console: the other half of human-in-the-loop routing.

    streamlit run console/app.py

Left, the page as it was scanned. Right, what the agent read, what it checked,
and why it stopped. A reviewer corrects a field or approves as read, and the
decision goes back into the ledger and into the handling-time numbers.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docagent import drift  # noqa: E402
from docagent.config import ALL_FIELDS, CRITICAL_FIELDS, load_config  # noqa: E402
from docagent.pipeline import Pipeline  # noqa: E402

# Categorical slot 1 and the blue sequential ramp from the validated palette.
COLORS = {
    "light": {
        "series": "#2a78d6", "ink": "#0b0b0b", "muted": "#898781", "grid": "#e1e0d9",
        "good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b",
        "ramp": ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#104281"],
    },
    "dark": {
        "series": "#3987e5", "ink": "#ffffff", "muted": "#898781", "grid": "#2c2c2a",
        "good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b",
        "ramp": ["#0d366b", "#184f95", "#256abf", "#3987e5", "#86b6ef", "#cde2fb"],
    },
}
MONEY_FIELDS = {"subtotal", "tax_amount", "total_amount"}


def palette() -> dict:
    try:
        theme_type = st.context.theme.type
    except Exception:
        theme_type = st.get_option("theme.base") or "light"
    return COLORS.get(theme_type, COLORS["light"])


def style(chart: alt.Chart, colors: dict) -> alt.Chart:
    return (
        chart.configure_view(strokeWidth=0)
        .configure_axis(
            labelColor=colors["muted"], titleColor=colors["muted"],
            domainColor=colors["grid"], tickColor=colors["grid"], gridColor=colors["grid"],
            labelFontSize=11, titleFontSize=11,
        )
        .configure_legend(labelColor=colors["muted"], titleColor=colors["muted"])
        .properties(background="transparent")
    )


@st.cache_resource
def get_pipeline() -> Pipeline:
    return Pipeline(load_config())


def confidence_mark(confidence: float, colors: dict) -> str:
    if confidence >= 0.92:
        color, label = colors["good"], "high"
    elif confidence >= 0.80:
        color, label = colors["warning"], "low"
    else:
        color, label = colors["critical"], "very low"
    return (
        f"<span style='color:{color};font-weight:600'>{confidence:.2f}</span> "
        f"<span style='color:{colors['muted']};font-size:0.8rem'>{label}</span>"
    )


def render_review(pipeline: Pipeline, document_id: str, colors: dict) -> None:
    store = pipeline.store
    document = store.document_row(document_id)
    extraction = store.extraction_payload(document_id)
    validation = store.validation_payload(document_id)
    decision = store.decision_row(document_id)

    st.subheader(document_id)
    st.caption(f"batch {document['batch_id']} · {extraction['backend']} / {extraction['model']}")

    page_column, form_column = st.columns([1, 1])

    with page_column:
        image_path = Path(document["image_path"])
        if image_path.exists():
            st.image(str(image_path), caption="scanned page", width="stretch")
        else:
            st.warning(f"page image missing at {image_path}")

    with form_column:
        st.markdown("**Why this stopped**")
        import json as _json

        for reason in _json.loads(decision["reasons"]):
            st.markdown(f"- {reason}")

        st.markdown("**Extracted fields**")
        corrections: dict = {}
        with st.form(f"review-{document_id}"):
            for name in ALL_FIELDS:
                field = extraction["fields"][name]
                label = f"{name}{' *' if name in CRITICAL_FIELDS else ''}"
                left, right = st.columns([3, 1])
                with left:
                    current = "" if field["value"] is None else str(field["value"])
                    entered = st.text_input(label, value=current, key=f"{document_id}-{name}")
                with right:
                    st.markdown("&nbsp;", unsafe_allow_html=True)
                    st.markdown(confidence_mark(field["confidence"], colors), unsafe_allow_html=True)
                if entered != current:
                    corrections[name] = float(entered) if name in MONEY_FIELDS and entered else entered

            reviewer = st.text_input("Reviewer", value="ap.reviewer")
            seconds = st.number_input(
                "Seconds spent", min_value=0.0, value=float(pipeline.config.review_handling_seconds)
            )
            approve, correct, reject = st.columns(3)
            approved = approve.form_submit_button("Approve as read", width="stretch")
            corrected = correct.form_submit_button("Save corrections", width="stretch")
            rejected = reject.form_submit_button("Reject", width="stretch")

        if approved or corrected or rejected:
            resolution = "rejected" if rejected else ("corrected" if corrections else "approved")
            pipeline.apply_review(
                document_id=document_id,
                resolution=resolution,
                reviewer=reviewer,
                corrections=corrections if resolution == "corrected" else {},
                seconds_spent=seconds,
            )
            st.success(f"{document_id} {resolution} by {reviewer}")
            time.sleep(0.4)
            st.rerun()

    st.markdown("**Business rules**")
    rows = [
        {
            "rule": f"{outcome['rule_id']} {outcome['title']}",
            "severity": outcome["severity"],
            "status": outcome["status"],
            "detail": outcome["detail"],
        }
        for outcome in validation["outcomes"]
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def render_operations(pipeline: Pipeline, colors: dict) -> None:
    store = pipeline.store
    batch_ids = store.batch_ids()
    if not batch_ids:
        st.info("Nothing processed yet. Run `python -m docagent.cli run`.")
        return

    summaries = [store.batch_summary(batch_id) for batch_id in batch_ids]
    frame = pd.DataFrame(summaries)

    columns = st.columns(4)
    overall = store.batch_summary()
    effort = store.review_effort()
    columns[0].metric("Documents", overall["documents"])
    columns[1].metric("Auto-post rate", f"{overall['auto_post_rate']:.0%}")
    columns[2].metric("Open reviews", len(store.open_reviews(limit=10000)))
    columns[3].metric("Reviewed", f"{effort['resolved']} docs")

    st.markdown("**Auto-post rate by batch**")
    bars = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=4, size=26, color=colors["series"])
        .encode(
            x=alt.X("batch_id:N", title=None),
            y=alt.Y("auto_post_rate:Q", title="auto-post rate", axis=alt.Axis(format="%")),
            tooltip=["batch_id", "documents", "auto_post", "needs_review", "rejected"],
        )
    )
    labels = bars.mark_text(dy=-8, fontSize=11, color=colors["ink"]).encode(
        text=alt.Text("auto_post_rate:Q", format=".0%")
    )
    st.altair_chart(style(bars + labels, colors), width="stretch")

    st.markdown("**Mean field confidence by batch**")
    heat_rows = []
    for batch_id in batch_ids:
        profile = drift.snapshot(store, [batch_id], pipeline.config)
        for name, field in profile["fields"].items():
            heat_rows.append(
                {"batch_id": batch_id, "field": name, "mean_confidence": field["mean_confidence"]}
            )
    heatmap = (
        alt.Chart(pd.DataFrame(heat_rows))
        .mark_rect(stroke="white", strokeWidth=2)
        .encode(
            x=alt.X("batch_id:N", title=None),
            y=alt.Y("field:N", title=None, sort=list(ALL_FIELDS)),
            color=alt.Color(
                "mean_confidence:Q",
                title="mean confidence",
                scale=alt.Scale(range=colors["ramp"], domain=[0.6, 1.0], clamp=True),
            ),
            tooltip=["batch_id", "field", alt.Tooltip("mean_confidence:Q", format=".3f")],
        )
        .properties(height=320)
    )
    st.altair_chart(style(heatmap, colors), width="stretch")

    st.markdown("**Drift alerts**")
    alerts = store.alerts(limit=100)
    if not alerts:
        st.caption("No alerts. Save a baseline with `docagent baseline`, then process a later batch.")
    else:
        alert_frame = pd.DataFrame(alerts)[["batch_id", "kind", "severity", "metric", "detail"]]
        st.dataframe(alert_frame, width="stretch", hide_index=True)

    with st.expander("Batch table"):
        st.dataframe(frame, width="stretch", hide_index=True)


def main() -> None:
    st.set_page_config(page_title="Document review console", page_icon="📄", layout="wide")
    config = load_config()
    colors = palette()

    st.title("Document Automation Agent — review console")
    st.caption(
        f"backend `{config.backend}` · confidence floor {config.field_confidence_floor:.2f} "
        f"(critical {config.critical_confidence_floor:.2f}) · auto-approval limit {config.manual_approval_amount:,.0f}"
    )

    if not (config.corpus_directory / "manifest.jsonl").exists():
        st.error(f"No corpus at {config.corpus_directory}. Run `python data/generate_corpus.py`.")
        return

    pipeline = get_pipeline()
    queue_tab, operations_tab = st.tabs(["Review queue", "Operations"])

    with queue_tab:
        reviews = pipeline.store.open_reviews(limit=200)
        if not reviews:
            st.success("The review queue is empty.")
        else:
            st.caption(f"{len(reviews)} open, weakest critical field first")
            options = {
                f"{review['document_id']} · crit {review['minimum_critical_confidence']:.2f} · {review['reasons'][0][:70]}":
                review["document_id"]
                for review in reviews
            }
            chosen = st.selectbox("Queue", list(options), label_visibility="collapsed")
            render_review(pipeline, options[chosen], colors)

    with operations_tab:
        render_operations(pipeline, colors)


if __name__ == "__main__":
    main()
