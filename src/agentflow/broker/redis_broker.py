from .placeholder_broker import PlaceholderBroker


class RedisBroker(PlaceholderBroker):
    """Reserved for a Redis backend that does not exist yet. Selecting
    `broker_type: redis` makes the agent fail to activate (issue #42)."""

    backend = 'redis'
