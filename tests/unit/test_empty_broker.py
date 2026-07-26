"""EmptyBroker inherits MessageBroker's default unsubscribe (no-op).

This test exists to satisfy RFC-001 acceptance criterion #6: the
default no-op unsubscribe must be exercised by at least one test on a
concrete broker path (EmptyBroker) so that a future accidental change
to the default is caught.
"""

from unittest.mock import MagicMock

from agentflow.broker.empty_broker import EmptyBroker


def test_empty_broker_unsubscribe_returns_none_by_default():
    broker = EmptyBroker(notifier=MagicMock())
    assert broker.unsubscribe("some/topic") is None


def test_empty_broker_unsubscribe_does_not_raise_on_unknown_topic():
    broker = EmptyBroker(notifier=MagicMock())
    broker.unsubscribe("never/subscribed")  # must not raise


def test_empty_broker_unsubscribe_does_not_call_notifier():
    notifier = MagicMock()
    broker = EmptyBroker(notifier=notifier)
    broker.unsubscribe("t")
    notifier._on_connect.assert_not_called()
    notifier._on_message.assert_not_called()
