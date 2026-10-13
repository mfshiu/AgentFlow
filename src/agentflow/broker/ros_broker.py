from .placeholder_broker import PlaceholderBroker


class RosBroker(PlaceholderBroker):
    """Reserved for a ROS backend that does not exist yet. Selecting
    `broker_type: ros` makes the agent stop at start (issue #42)."""

    backend = 'ros'
