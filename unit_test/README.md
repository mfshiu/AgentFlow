# Quarantined legacy tests

Nothing in this folder runs: pytest skips it (`norecursedirs` in
`pyproject.toml`), CI does not run it, and ruff excludes it. The files
do not work as they are (issue #12):

- `test_parcel.py`, `test_parents_children.py` and
  `test_parents_children_count.py` import `unit_test.config_test`,
  which is gitignored and missing, and call `Agent._subscribe` /
  `Agent._publish`, which no longer exist.
- `mqtt_broker_test.py` still has the placeholder
  `MODULE_PATH = "yourpkg.mqtt_broker"`.
- `mqtt_client_test.py` is a manual script for a live broker at
  `localhost:1884`.

What they meant to cover is tested under `tests/`:

- Parcel encoding and decoding: `tests/unit/core/test_parcel_decoding.py`
  and `tests/security/test_parcel_pickle_safety.py`.
- Parents and children: `tests/unit/core/test_holon_*.py`,
  `tests/wire/test_holon_wire.py` and `tests/wire/test_holon_terminate_wire.py`.
- MqttBroker: `tests/unit/test_mqtt_broker_*.py` and the `tests/wire/`
  tests, which run a real paho client against a loopback server.
- Agents talking across processes: `tests/wire/test_downstream_usage_wire.py`.

The files are kept for reference only; delete them once nobody needs
them.
