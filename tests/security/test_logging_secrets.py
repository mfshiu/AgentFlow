"""Configuration and publish diagnostics must not expose planted secrets."""

import copy
import logging
import threading
from unittest.mock import MagicMock

import pytest

from agentflow.broker.empty_broker import EmptyBroker
from agentflow.broker.placeholder_broker import BrokerNotAvailableError
from agentflow.broker.redis_broker import RedisBroker
from agentflow.broker.ros_broker import RosBroker
from agentflow.core.agent import Agent


class ActivatedAgent(Agent):
    def __init__(self, config: dict) -> None:
        self.activated = threading.Event()
        super().__init__("logging-security", config)

    def on_activate(self, config: dict) -> None:
        self.activated.set()


@pytest.mark.security
def test_real_thread_lifecycle_keeps_config_credentials_and_payload_out_of_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    config = {
        "broker": {
            "broker_name": "fixture",
            "fixture": {"broker_type": "empty", "password": "PW-PLANTED"},
        },
        "llm": {"api_key": "KEY-PLANTED"},
        "services": [{"Authorization": "AUTH-PLANTED"}],
    }
    original = copy.deepcopy(config)
    agent = ActivatedAgent(config)
    try:
        agent.start_thread()
        assert agent.activated.wait(3), "real EmptyBroker activation must finish"
        agent.publish("orders", {"card": "PAYLOAD-PLANTED"})
        assert agent.config["broker"] == original["broker"]
        assert agent.config["llm"] == original["llm"]
    finally:
        agent.terminate()
        if agent._agent_worker is not None:
            assert not agent._agent_worker.work_thread.is_alive()
    assert config == original
    for secret in ("PW-PLANTED", "KEY-PLANTED", "AUTH-PLANTED", "PAYLOAD-PLANTED"):
        assert secret not in caplog.text
    assert "logging-security" in caplog.text
    assert "orders" in caplog.text


@pytest.mark.security
@pytest.mark.parametrize("broker_type", [EmptyBroker, RedisBroker, RosBroker])
@pytest.mark.parametrize(
    "payload",
    [b"PAYLOAD-PLANTED" * 1000, "PAYLOAD-PLANTED", {"card": "PAYLOAD-PLANTED"}],
    ids=["large-bytes", "string", "mapping"],
)
def test_backend_publish_logs_only_metadata(
    broker_type: type[EmptyBroker | RedisBroker | RosBroker],
    payload: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    broker = broker_type(MagicMock())
    if broker_type in (RedisBroker, RosBroker):
        with pytest.raises(BrokerNotAvailableError) as raised:
            broker.publish("orders", payload)
        assert "PAYLOAD-PLANTED" not in str(raised.value)
        assert "PAYLOAD-PLANTED" not in caplog.text
        assert not caplog.records
        return

    broker.publish("orders", payload)
    assert "PAYLOAD-PLANTED" not in caplog.text
    records = [record for record in caplog.records if "orders" in record.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert len(records[0].getMessage()) < 150


def test_empty_broker_options_are_redacted_without_mutating_input(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    options = {"host": "fixture", "nested": ({"access-token": "TOKEN-PLANTED"},)}
    original = copy.deepcopy(options)
    EmptyBroker(MagicMock()).start(options)
    assert "TOKEN-PLANTED" not in caplog.text
    assert "fixture" in caplog.text
    assert options == original


def test_nested_redaction_preserves_safe_values_aliases_and_cycles() -> None:
    from agentflow.logging import redact_config

    shared = {"API-Key": "KEY-PLANTED", "retries": 3}
    config = {"first": [shared], "second": (shared,), "enabled": True}
    config["cycle"] = config
    assert redact_config(config) == {
        "first": [{"API-Key": "***", "retries": 3}],
        "second": ({"API-Key": "***", "retries": 3},),
        "enabled": True,
        "cycle": "<recursive>",
    }
    assert shared["API-Key"] == "KEY-PLANTED"


@pytest.mark.parametrize(
    "name",
    [
        "PASSWORD",
        "passwd",
        "client_secret",
        "access-token",
        "apiKey",
        "private_key",
        "credentials",
        "Authorization",
        "AWS_ACCESS_KEY_ID",
    ],
)
def test_secret_names_are_case_and_separator_insensitive(name: str) -> None:
    from agentflow.logging import redact_config

    assert redact_config({name: {"nested": "SECRET-PLANTED"}}) == {name: "***"}


def test_opaque_objects_and_deep_config_do_not_run_repr_or_recurse_forever(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agentflow.logging import redact_config

    class Opaque:
        def __repr__(self) -> str:
            raise AssertionError("diagnostics must not evaluate application repr")

        def __len__(self) -> int:
            raise AssertionError("diagnostics must not evaluate application len")

    config: object = Opaque()
    for _ in range(100):
        config = [config]
    assert "<max-depth>" in str(redact_config(config))
    assert redact_config({"value": Opaque()}) == {"value": "<object>"}
    caplog.set_level(logging.DEBUG)
    EmptyBroker(MagicMock()).publish("orders", Opaque())
    assert "payload_length: None" in caplog.text
