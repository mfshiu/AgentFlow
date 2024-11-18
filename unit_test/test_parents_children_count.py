import sys
import os
sys.path.append(os.path.abspath(".."))  # Adjust path if necessary

import unittest
from src.agentflow.core.agent import Agent

import threading
import time

from agentflow.core.agent import Agent
from agentflow.core import config
from agentflow.core.config import EventHandler
from unit_test.config_test import config_test

from AgentFlow import log_helper
logger = log_helper.get_logger()



class AgentA(Agent):


    def __init__(self):
        super().__init__(name='aaa', agent_config=config_test)
        self.children_count = 0


    def on_register_child(self, child_id, child_info:dict):
        self.children_count += 1
        self._publish('children_count', self.children_count)



class AgentB(Agent):


    def __init__(self):
        super().__init__(name='aaa.bbb', agent_config=config_test)
        self.parents_count = 0


    def on_register_parent(self, parent_id, parent_info):
        self.parents_count += 1
        self._publish('parents_count', self.parents_count)



class TestAgent(unittest.TestCase):

    children_count = 0
    parents_count = 0
    
    
    
    class ValidationAgent(Agent):


        def __init__(self):
            super().__init__(name='validation', agent_config=config_test)
            
            
        def on_connected(self):
            self._subscribe('children_count')
            self._subscribe('parents_count')
            
            
        def on_message(self, topic: str, data):
            logger.debug(self.M(f"topic: {topic}, data: {data}"))
            
            if 'children_count' == topic:
                TestAgent.children_count = int(data)
            else:
                TestAgent.parents_count = int(data)



    def setUp(self):
        self.validation_agent = TestAgent.ValidationAgent()
        self.validation_agent.start_thread()
        
        self.agent_aaa = AgentA()
        self.agent_bbb = AgentB()
        self.agent_bbb1 = AgentB()
        self.agent_aaa.start()
        self.agent_bbb.start()
        self.agent_bbb1.start()


    def test_1(self):
        time.sleep(1)
        logger.info(f"children_count: {TestAgent.children_count}")
        logger.info(f"parents_count: {TestAgent.parents_count}")
        self.assertEqual(TestAgent.children_count, 2)
        self.assertEqual(TestAgent.parents_count, 1)


    def tearDown(self):
        self.agent_aaa.terminate()
        self.validation_agent.terminate()



if __name__ == '__main__':
    unittest.main()
