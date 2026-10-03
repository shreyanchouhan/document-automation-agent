from docagent import drift
from docagent.drift import histogram, population_stability_index


def test_psi_is_zero_for_an_identical_distribution():
    distribution = histogram([0.99] * 30 + [0.6] * 10)
    assert population_stability_index(distribution, distribution) == 0.0


def test_psi_grows_as_the_distribution_moves():
    baseline = histogram([0.99] * 40)
    mild = histogram([0.99] * 35 + [0.5] * 5)
    severe = histogram([0.99] * 10 + [0.5] * 30)
    assert 0 < population_stability_index(baseline, mild) < population_stability_index(baseline, severe)


def test_a_favourable_shift_is_reported_but_not_alerted(config, processed_pipeline):
    baseline = {
        "auto_post_rate": 0.6,
        "fields": {
            "total_amount": {
                "histogram": histogram([0.85] * 20 + [0.99] * 20),
                "mean_confidence": 0.92,
                "null_rate": 0.0,
                "below_floor_rate": 0.05,
            }
        },
    }
    current = {
        "auto_post_rate": 0.62,
        "fields": {
            "total_amount": {
                "histogram": histogram([0.99] * 40),
                "mean_confidence": 0.99,
                "null_rate": 0.0,
                "below_floor_rate": 0.0,
            }
        },
    }
    alerts = drift.compare(baseline, current, config)
    assert all(alert.severity == "info" for alert in alerts)


def test_an_adverse_shift_alerts(config):
    baseline = {
        "auto_post_rate": 0.6,
        "fields": {
            "total_amount": {
                "histogram": histogram([0.99] * 40),
                "mean_confidence": 0.99,
                "null_rate": 0.0,
                "below_floor_rate": 0.0,
            }
        },
    }
    current = {
        "auto_post_rate": 0.58,
        "fields": {
            "total_amount": {
                "histogram": histogram([0.99] * 15 + [0.45] * 25),
                "mean_confidence": 0.65,
                "null_rate": 0.0,
                "below_floor_rate": 0.62,
            }
        },
    }
    alerts = drift.compare(baseline, current, config)
    assert any(alert.kind == "confidence_drift" and alert.severity != "info" for alert in alerts)


def test_an_auto_post_rate_collapse_alerts(config):
    baseline = {"auto_post_rate": 0.70, "fields": {}}
    current = {"auto_post_rate": 0.30, "fields": {}}
    alerts = drift.compare(baseline, current, config)
    assert [alert.kind for alert in alerts] == ["auto_post_rate_drop"]
    assert alerts[0].severity == "critical"


def test_monitoring_without_a_baseline_is_a_no_op(processed_pipeline, config):
    config.baseline_path.unlink(missing_ok=True)
    assert drift.monitor_batch(processed_pipeline.store, "batch-01", config) == []


def test_snapshot_and_baseline_round_trip(processed_pipeline, config):
    profile = drift.snapshot(processed_pipeline.store, ["batch-01"], config)
    assert profile["documents"] > 0
    assert "total_amount" in profile["fields"]
    assert set(profile["fields"]["total_amount"]) >= {
        "histogram", "mean_confidence", "null_rate", "below_floor_rate"
    }
    drift.save_baseline(profile, config.baseline_path)
    assert drift.load_baseline(config.baseline_path) == profile
