from abc import ABC, abstractmethod
from typing import Any

from agent import Agent


class Logistic(ABC):
    def __init__(self, service_agent:Agent):
        self.service_agent = service_agent


    @abstractmethod
    def publish(self, topic, data):
        pass


    @abstractmethod
    def subscribe(self, topic):
        pass
