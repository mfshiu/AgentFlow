from enum import Enum


class BrokerType(Enum):
    Redis = "redis"
    MQTT = "mqtt"
    ROS = "ros"
    Empty = "empty"
    