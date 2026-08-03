# AgentFlow Tests

This directory holds the deterministic AgentFlow test suite.

## Scope (Phase 1)

- Pure unit tests only.
- No real MQTT / DDS / ROS / Redis connection.
- No fixed host, port, credential, or environment variable.
- No test spawns a subprocess or a thread that outlives the test.

## Layout

```
tests/
├── conftest.py                       shared fixtures (fake_client, notifier, broker)
└── unit/
    ├── test_mqtt_broker_start.py       start(), callbacks, wait=True paths
    ├── test_mqtt_broker_auth.py        username / password / walrus edge cases
    ├── test_mqtt_broker_lifecycle.py   stop, publish, subscribe delegation
    └── test_mqtt_broker_callbacks.py   _on_connect / _on_message / _on_disconnect
```

`unit_test/` (legacy) and `exe_test/` (demo scripts) are excluded from
pytest collection via `pyproject.toml` `norecursedirs`.

## Running

```bash
# From repository root:
PYTHONPATH=src python -m pytest tests/unit -v
```

`pyproject.toml` also sets `pythonpath = ["src"]`, so bare `pytest`
works too:

```bash
python -m pytest -v
```

## Requirements

- Python ≥ 3.11 (matches `setup.py` `python_requires='>=3.11'`).
- `paho-mqtt==2.1.0` (imported by `agentflow.broker.mqtt_broker` at
  module load; tests replace `Client` with a `MagicMock`).
- `pytest ≥ 7.0`.

## What is NOT tested here

- Live broker round-trips (require a running MQTT broker).
- Multi-agent parent/child flows (needs process/thread orchestration).
- Process-mode Agent lifecycle (R-06 in the audit register).
- Pickle payload security (R-01 forcing test lives in `tests/security/`
  once added in a later step).

## Markers

Registered in `pyproject.toml`:

- `quarantine` — legacy tests kept only for reference
- `integration` — needs live external services
- `security` — security-relevant tests (may include known-failing
  forcing functions)
- `manual` — manual demo scripts, not automated
