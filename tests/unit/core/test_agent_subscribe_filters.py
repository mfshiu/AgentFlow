"""Agent.subscribe checks an MQTT filter before registering anything
(issue #53 review).

The filter used to be checked only by the broker, after Agent.subscribe
had stored the handler and recorded the topic as subscribed in this
session; an invalid one raised but left both behind. A leftover such as
'a/#/b' then made the early-message gate (#17) treat every 'a/...'
topic as the agent's own.
"""

import pytest

from agentflow.broker import message_broker
from agentflow.core.agent import Agent
from tests.fakes.fake_broker import FakeBroker, FakeWorker


@pytest.fixture
def agent():
    agent = Agent('subscriber', {'dispatch': {'shutdown_timeout_s': 1.0}})
    agent._broker = FakeBroker(notifier=agent)
    agent._agent_worker = FakeWorker()
    yield agent
    agent.terminate()


@pytest.mark.parametrize('topic', ['robot+1/cmd', 'a/#/b', ''])
def test_an_invalid_filter_leaves_no_handler_and_no_session_topic(agent, topic):
    with pytest.raises(ValueError):
        agent.subscribe(topic, topic_handler=lambda t, p: None)
    assert agent._broker.subscribe_calls == []
    assert topic not in agent._session_topics
    assert topic not in agent._Agent__topic_handlers


@pytest.mark.parametrize('topic', ['a/+/b', '#', '$share/g/a'])
def test_check_filter_accepts_valid_filters(topic):
    assert message_broker.check_filter(topic) == topic
