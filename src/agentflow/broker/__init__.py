from enum import Enum

# RFC-012: public re-export of the publish result contract exception
# so callers can `from agentflow.broker import MqttPublishError`
# without depending on the mqtt_broker module path.
from .mqtt_broker import MqttBackpressureError, MqttPublishError, MqttPublishReason
from .message_broker import PublishBackpressureError


class BrokerType(Enum):
    Redis = "redis"
    MQTT = "mqtt"
    ROS = "ros"
    Empty = "empty"


__all__ = [
    "BrokerType",
    "MqttBackpressureError",
    "MqttPublishError",
    "MqttPublishReason",
    "PublishBackpressureError",
]
