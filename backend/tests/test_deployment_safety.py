from pathlib import Path

import yaml
from sqlalchemy.schema import CreateIndex
from sqlalchemy.dialects import postgresql

from app.db.models import Incident

ROOT = Path(__file__).resolve().parents[2]


def test_alertmanager_uses_bearer_token_from_kubernetes_secret() -> None:
    manifest = yaml.safe_load(
        (ROOT / "deploy" / "prometheus" / "incidentpilot-alertmanager-config.yaml")
        .read_text()
    )
    webhook = manifest["spec"]["receivers"][0]["webhookConfigs"][0]
    authorization = webhook["httpConfig"]["authorization"]

    assert webhook["url"].endswith("/api/v1/webhooks/alertmanager")
    assert authorization["type"] == "Bearer"
    assert authorization["credentials"] == {
        "name": "incidentpilot-alertmanager-webhook-token",
        "key": "token",
    }


def test_crashloop_alert_uses_restart_metric_available_in_prometheus() -> None:
    manifest = yaml.safe_load(
        (ROOT / "deploy" / "prometheus" / "crashloop-rule.yaml").read_text()
    )
    rule = manifest["spec"]["groups"][0]["rules"][0]

    assert "kube_pod_container_status_restarts_total" in rule["expr"]
    assert "namespace=\"dev\"" in rule["expr"]
    assert ">= 3" in rule["expr"]


def test_alertmanager_allows_team_routes_across_namespaces() -> None:
    values = yaml.safe_load(
        (ROOT / "deploy" / "prometheus" / "values.yaml").read_text()
    )
    strategy = values["alertmanager"]["alertmanagerSpec"][
        "alertmanagerConfigMatcherStrategy"
    ]

    assert strategy == {"type": "None"}


def test_database_dedup_index_is_partial_and_unique() -> None:
    index = next(
        index
        for index in Incident.__table__.indexes
        if index.name == "uq_incidents_open_fingerprint"
    )
    sql = str(
        CreateIndex(index).compile(dialect=postgresql.dialect())
    ).lower()

    assert index.unique
    assert "unique index" in sql
    assert "where state <> 'resolved'" in sql
