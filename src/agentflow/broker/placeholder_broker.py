from .message_broker import MessageBroker


class PlaceholderBroker(MessageBroker):
    """A broker type that is reserved but not implemented (issue #42).

    These brokers used to log and drop every message while the agent
    reported the broker as started, so a misconfigured agent looked
    healthy and publish_sync only timed out. They now refuse to start,
    publish or subscribe: an agent that selects one stops at once, with
    an error naming the backend. Only the MQTT broker carries messages.
    """

    backend = 'placeholder'

    def _not_implemented(self) -> NotImplementedError:
        return NotImplementedError(
            f"The '{self.backend}' broker is not implemented; "
            f"use broker_type 'mqtt' (see issue #42).")


    ###################################
    # Implementation of MessageBroker #
    ###################################


    def start(self, options:dict):
        raise self._not_implemented()


    def stop(self):
        # Agent deactivation calls stop() even after start() failed.
        return None


    def publish(self, topic:str, payload, *, qos: int = 0, retain: bool = False):
        raise self._not_implemented()


    def subscribe(self, topic:str, data_type, *, qos: int = 0):
        raise self._not_implemented()
