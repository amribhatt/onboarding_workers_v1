"""The compose file, read as data (no Docker needed): the rules the deployment depends on."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
APP = ["migrate", "worker", "verifier", "anchor", "projector"]


def _env(service: str) -> dict[str, str]:
    env = SERVICES[service].get("environment", {})
    return env if isinstance(env, dict) else dict(item.split("=", 1) for item in env)


def test_the_databases_publish_no_port() -> None:
    for name in ("postgres", "neo4j", "postgres-test", "neo4j-test"):
        assert "ports" not in SERVICES[name], name


def test_the_only_published_port_is_the_consoles_and_it_is_localhost() -> None:
    published = {n: s["ports"] for n, s in SERVICES.items() if "ports" in s}
    assert published == {"worker": ["127.0.0.1:8765:8765"]}


def test_the_backend_network_is_internal_and_the_databases_are_only_on_it() -> None:
    assert COMPOSE["networks"]["backend"]["internal"] is True
    for name in ("postgres", "neo4j"):
        assert SERVICES[name]["networks"] == ["backend"]


@pytest.mark.parametrize("name", APP)
def test_app_services_are_locked_down(name: str) -> None:
    s = SERVICES[name]
    assert s["read_only"] is True and s["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in s["security_opt"]


def test_no_secret_is_in_an_environment_block() -> None:
    for name in SERVICES:
        for key, value in _env(name).items():
            secretish = any(w in key for w in ("PASSWORD", "TOKEN", "KEY", "SECRET", "DATABASE_URL"))
            if secretish and not key.endswith("_FILE"):
                # only a reference to the .env file's non-database settings, or a throw-away test credential
                assert str(value).startswith("${") or name.endswith("test") or name == "tests" \
                    or key == "WORKER_REQUIRE_TOKEN", (name, key)
    assert {"app_database_url", "owner_database_url"} <= set(COMPOSE["secrets"])


def test_start_order_waits_for_the_database_and_the_migration() -> None:
    assert SERVICES["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    for name in ("worker", "verifier", "anchor"):
        assert SERVICES[name]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"


def test_the_worker_has_a_health_check_and_time_to_drain() -> None:
    assert "livez" in " ".join(SERVICES["worker"]["healthcheck"]["test"])
    assert SERVICES["worker"]["stop_grace_period"] == "60s"
    assert int(_env("worker")["WORKER_DRAIN_SECONDS"]) < 60


def test_the_graph_is_a_profile_and_off_by_default() -> None:
    assert SERVICES["neo4j"]["profiles"] == ["graph"] and SERVICES["projector"]["profiles"] == ["graph"]
    assert _env("worker")["WORKER_GRAPH"] == "${WORKER_GRAPH:-off}"


def test_the_image_copies_the_rule_files_and_uses_the_liveness_check() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY skills ./skills" in text and "/livez" in text and "USER worker" in text
