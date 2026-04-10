import datetime as dt

from ampa import scheduler_types
from ampa.scheduler_types import CommandSpec, SchedulerConfig


def test_from_iso_z_terminator():
    t = scheduler_types._from_iso("2023-01-02T03:04:05Z")
    assert t is not None
    assert t.tzinfo is not None
    assert t.utcoffset() == dt.timedelta(0)


def test_from_iso_plus_offset():
    t = scheduler_types._from_iso("2023-01-02T03:04:05+00:00")
    assert t is not None
    assert t.tzinfo is not None
    assert t.utcoffset() == dt.timedelta(0)


def test_from_iso_naive_assumed_utc():
    t = scheduler_types._from_iso("2023-01-02T03:04:05")
    assert t is not None
    # Naive timestamps should be coerced to UTC
    assert t.tzinfo is not None
    assert t.utcoffset() == dt.timedelta(0)


def test_command_spec_round_trip_includes_agent_field():
    spec = CommandSpec(
        command_id="wl-audit",
        command="true",
        requires_llm=True,
        frequency_minutes=2,
        priority=1,
        metadata={"discord_label": "wl audit"},
        title="Audit",
        max_runtime_minutes=5,
        command_type="audit",
        agent="Casey",
    )
    payload = spec.to_dict()
    restored = CommandSpec.from_dict(payload)

    assert payload["agent"] == "Casey"
    assert restored.agent == "Casey"


def test_scheduler_config_from_env_adds_casey_default_mapping(monkeypatch):
    monkeypatch.setenv("AMPA_LLM_HEALTHCHECK_URL", "http://localhost:9000/health")
    monkeypatch.delenv("AMPA_DEFAULT_LLM_AGENT", raising=False)
    monkeypatch.delenv("AMPA_LLM_AGENT_ENDPOINTS", raising=False)

    config = SchedulerConfig.from_env()

    assert config.default_llm_agent == "Casey"
    assert config.llm_agent_endpoints.get("Casey") == "http://localhost:9000/health"
