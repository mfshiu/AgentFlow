# tests/test_mqtt_broker.py
import types
import pytest
from unittest.mock import MagicMock, call

MODULE_PATH = "yourpkg.mqtt_broker"  # ← 修改為實際模組路徑

@pytest.fixture
def module(monkeypatch):
    # 匯入被測模組
    mod = __import__(MODULE_PATH, fromlist=["*"])

    # 製作假的 paho Client 實例
    fake_client = MagicMock(name="PahoClient")
    # 讓建構子回傳 fake_client；注意 MqttBroker 在模組層已從 paho import 了 Client
    monkeypatch.setattr(mod, "Client", lambda *a, **kw: fake_client)

    # 假 notifier
    notifier = MagicMock(name="BrokerNotifier")

    return types.SimpleNamespace(mod=mod, fake_client=fake_client, notifier=notifier)

def test_start_sets_handlers_and_connects_and_loops(module):
    MqttBroker = module.mod.MqttBroker
    broker = MqttBroker(notifier=module.notifier)

    opts = {"host": "h", "port": 1883, "keepalive": 60}
    broker.start(opts)

    # callback 設定
    assert module.fake_client.on_connect == broker._on_connect
    assert module.fake_client.on_message == broker._on_message

    # 參數保存
    assert broker.host == "h"
    assert broker.port == 1883
    assert broker.keepalive == 60

    # 連線與 loop
    module.fake_client.connect.assert_called_once_with("h", 1883, 60)
    module.fake_client.loop_start.assert_called_once()

def test_start_with_username_password_calls_username_pw_set(module):
    MqttBroker = module.mod.MqttBroker
    broker = MqttBroker(notifier=module.notifier)

    opts = {"host": "h", "port": 1883, "keepalive": 60, "username": "u", "password": "p"}
    broker.start(opts)

    module.fake_client.username_pw_set.assert_called_once_with("u", "p")

def test_start_without_username_does_not_call_username_pw_set(module):
    MqttBroker = module.mod.MqttBroker
    broker = MqttBroker(notifier=module.notifier)

    opts = {"host": "h", "port": 1883, "keepalive": 60}
    broker.start(opts)

    module.fake_client.username_pw_set.assert_not_called()

def test_stop_disconnects_and_stops_loop(module):
    MqttBroker = module.mod.MqttBroker
    broker = MqttBroker(notifier=module.notifier)

    broker.stop()

    module.fake_client.disconnect.assert_called_once()
    module.fake_client.loop_stop.assert_called_once()

def test_publish_delegates(module):
    MqttBroker = module.mod.MqttBroker
    broker = MqttBroker(notifier=module.notifier)

    broker.publish("t/a", b"payload")
    module.fake_client.publish.assert_called_once_with(topic="t/a", payload=b"payload")

def test_subscribe_delegates(module):
    MqttBroker = module.mod.MqttBroker
    broker = MqttBroker(notifier=module.notifier)

    broker.subscribe("t/b", data_type=str)
    module.fake_client.subscribe.assert_called_once_with(topic="t/b")

def test_on_connect_notifies(module):
    MqttBroker = module.mod.MqttBroker
    broker = MqttBroker(notifier=module.notifier)
    # 先設定 broker.host/port/keepalive 以便 log 用（非必要）
    broker.host, broker.port, broker.keepalive = "h", 1883, 60

    # 模擬 paho 呼叫
    broker._on_connect(client=module.fake_client, userdata=None, flags={}, reasonCode=0, properties=None)

    module.notifier._on_connect.assert_called_once()

def test_on_message_forwards_topic_and_payload(module):
    MqttBroker = module.mod.MqttBroker
    broker = MqttBroker(notifier=module.notifier)

    msg = types.SimpleNamespace(topic="t/c", payload=b"X")
    broker._on_message(client=module.fake_client, db=None, message=msg)

    module.notifier._on_message.assert_called_once_with("t/c", b"X")

def test_on_message_catches_exception_and_continues(module, caplog):
    MqttBroker = module.mod.MqttBroker
    broker = MqttBroker(notifier=module.notifier)

    module.notifier._on_message.side_effect = RuntimeError("boom")
    msg = types.SimpleNamespace(topic="t/err", payload=b"oops")

    with caplog.at_level("ERROR"):
        broker._on_message(client=module.fake_client, db=None, message=msg)

    # 有記錄例外（不拋出）
    assert any("boom" in rec.message for rec in caplog.records)
