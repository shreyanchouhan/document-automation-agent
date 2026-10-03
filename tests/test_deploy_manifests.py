"""The manifests are part of the deliverable, so they are linted like code."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

K8S_DIRECTORY = Path(__file__).resolve().parent.parent / "deploy" / "k8s"


def documents():
    for path in sorted(K8S_DIRECTORY.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text()):
            if document:
                yield path.name, document


def test_every_manifest_parses_and_names_a_kind():
    found = list(documents())
    assert found
    for name, document in found:
        assert document.get("apiVersion"), name
        assert document.get("kind"), name
        if document["kind"] != "Kustomization":  # kustomize files carry no metadata.name
            assert document.get("metadata", {}).get("name"), name


def test_the_expected_objects_are_present():
    kinds = {document["kind"] for _, document in documents()}
    assert {"Deployment", "Service", "ConfigMap", "CronJob", "HorizontalPodAutoscaler"} <= kinds


def test_the_api_deployment_has_all_three_probes_and_runs_unprivileged():
    deployment = next(d for _, d in documents() if d["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert {"startupProbe", "readinessProbe", "livenessProbe"} <= set(container)
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert deployment["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True


def test_resources_are_bounded_on_every_container():
    for name, document in documents():
        if document["kind"] == "Deployment":
            containers = document["spec"]["template"]["spec"]["containers"]
        elif document["kind"] == "CronJob":
            containers = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
        else:
            continue
        for container in containers:
            assert "requests" in container["resources"], name
            assert "limits" in container["resources"], name


def test_the_cronjob_fails_the_run_on_critical_drift():
    cronjob = next(d for _, d in documents() if d["kind"] == "CronJob")
    args = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--fail-on-critical-drift" in args
    assert cronjob["spec"]["concurrencyPolicy"] == "Forbid"


def test_the_api_and_the_cronjob_run_the_same_image():
    images = {
        container["image"]
        for _, document in documents()
        for container in (
            document["spec"]["template"]["spec"]["containers"]
            if document["kind"] == "Deployment"
            else document["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
            if document["kind"] == "CronJob"
            else []
        )
    }
    assert len(images) == 1
