"""Verify MqttBroker.start credential handling with a mocked paho Client.

Target module: agentflow.broker.mqtt_broker (real code).
The `if username := options.get("username")` walrus at mqtt_broker.py:74
gates `username_pw_set`; these tests pin the exact behaviour without
relying on any real credential.
"""


def test_username_and_password_calls_username_pw_set(broker, fake_client):
    broker.start({"username": "user-under-test", "password": "pw-under-test"})
    fake_client.username_pw_set.assert_called_once_with(
        "user-under-test", "pw-under-test"
    )


def test_username_only_calls_username_pw_set_with_none_password(broker, fake_client):
    broker.start({"username": "user-under-test"})
    fake_client.username_pw_set.assert_called_once_with("user-under-test", None)


def test_no_username_does_not_call_username_pw_set(broker, fake_client):
    # Password without username must be ignored (walrus guards on username).
    broker.start({"password": "ignored-without-username"})
    fake_client.username_pw_set.assert_not_called()


def test_empty_username_does_not_call_username_pw_set(broker, fake_client):
    # Empty string is falsy → walrus branch not taken.
    broker.start({"username": "", "password": "ignored"})
    fake_client.username_pw_set.assert_not_called()


def test_no_credentials_does_not_call_username_pw_set(broker, fake_client):
    broker.start({})
    fake_client.username_pw_set.assert_not_called()


def test_username_pw_set_called_before_connect(broker, fake_client):
    call_order = []
    fake_client.username_pw_set.side_effect = lambda *a, **kw: call_order.append(
        "username_pw_set"
    )
    fake_client.connect.side_effect = lambda *a, **kw: call_order.append("connect")
    broker.start({"username": "u", "password": "p"})
    assert call_order == ["username_pw_set", "connect"]
