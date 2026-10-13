from enum import Enum

# RFC-012: public re-export of the publish result contract exception
# so callers can `from agentflow.broker import MqttPublishError`
# without depending on the mqtt_broker module path.
from .mqtt_broker import MqttBackpressureError, MqttPublishError, MqttPublishReason
from .message_broker import PublishBackpressureError
from .placeholder_broker import BrokerNotAvailableError


class BrokerType(Enum):
    """Only MQTT carries messages. Empty is a no-op for tests; Redis
    and ROS are reserved names whose brokers refuse to start (#42)."""
    Redis = "redis"
    MQTT = "mqtt"
    ROS = "ros"
    Empty = "empty"


__all__ = [
    "BrokerNotAvailableError",
    "BrokerType",
    "MqttBackpressureError",
    "MqttPublishError",
    "MqttPublishReason",
    "PublishBackpressureError",
]
