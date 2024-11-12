import sys
import os
sys.path.append(os.path.abspath(".."))  # Adjust path if necessary

import unittest
import time

from agentflow.core.agent import Agent
from agentflow.core.config import EventHandler
from unit_test.config_test import config_test

from logging import Logger
from AgentFlow import log_helper
logger:Logger = log_helper.get_logger()



class TestAgent(unittest.TestCase):
    children_counts = {}
    parents_counts = {}
    
    
    
    class ValidationAgent(Agent):
        def __init__(self):
            super().__init__(name='validation', agent_config=config_test)
            
            
        def on_connected(self):
            self._subscribe('children_count')
            self._subscribe('parents_count')
            
            
        # def on_message(self, topic: str, data):
        #     logger.debug(self.M(f"topic: {topic}, data: {data}"))
            
        #     if 'children_count' == topic:
        #         TestAgent.children_count = int(data)
        #     else:
        #         TestAgent.parents_count = int(data)

                
        def on_register_child(self, child_id, child_info:dict):
            logger.debug(f"self: {self}, child_id: {child_id}, child_info: {child_info}")
            if (parent_id := child_info['parent_id']) in TestAgent.children_counts:
                TestAgent.children_counts[parent_id] += 1
            else:
                TestAgent.children_counts[parent_id] = 1
            logger.debug(f"TestAgent.children_counts: {TestAgent.children_counts}")

                
        def on_register_parent(self, parent_id, parent_info:dict):
            logger.debug(f"self: {self}, parent_id: {parent_id}, parent_info: {parent_info}")
            if (child_id := parent_info['child_id']) in TestAgent.parents_counts:
                TestAgent.parents_counts[child_id] += 1
            else:
                TestAgent.parents_counts[child_id] = 1
            logger.debug(f"TestAgent.parents_counts: {TestAgent.parents_counts}")
        
        

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)        
        self.validation_agent = TestAgent.ValidationAgent()


    def setUp(self):
        self.validation_agent.start_thread()
        
        cfg = config_test.copy()
        cfg[EventHandler.ON_REGISTER_CHILD] = self.validation_agent.on_register_child
        cfg[EventHandler.ON_REGISTER_PARENT] = self.validation_agent.on_register_parent
        
        self.aaa = Agent(name='aaa', agent_config=cfg)
        self.aaa.start()
        
        self.aaa_bbb = Agent(name='aaa.bbb', agent_config=cfg)
        self.aaa_bbb.start()
        self.aaa_ccc = Agent(name='aaa.ccc', agent_config=cfg)
        self.aaa_ccc.start()

        self.aaa_bbb_111 = Agent(name='aaa.bbb.111', agent_config=cfg)
        self.aaa_bbb_111.start()
        self.aaa_bbb_222 = Agent(name='aaa.bbb.222', agent_config=cfg)
        self.aaa_bbb_222.start()


    def _do_test_1(self):
        self.assertEqual(TestAgent.parents_counts.get(self.aaa.agent_id, 0), 0)
        self.assertEqual(TestAgent.children_counts[self.aaa.agent_id], 2)
        
        self.assertEqual(TestAgent.parents_counts[self.aaa_bbb.agent_id], 1)
        self.assertEqual(TestAgent.children_counts.get(self.aaa_bbb.agent_id, 0), 2)
        
        self.assertEqual(TestAgent.parents_counts[self.aaa_ccc.agent_id], 1)
        self.assertEqual(TestAgent.children_counts.get(self.aaa_ccc.agent_id, 0), 0)
        
        self.assertEqual(TestAgent.parents_counts[self.aaa_bbb_111.agent_id], 1)
        self.assertEqual(TestAgent.children_counts.get(self.aaa_bbb_111.agent_id, 0), 0)
        
        self.assertEqual(TestAgent.parents_counts[self.aaa_bbb_222.agent_id], 1)
        self.assertEqual(TestAgent.children_counts.get(self.aaa_bbb_222.agent_id, 0), 0)


    def test_1(self):
        time.sleep(2)
        
        try:
            self._do_test_1()
        except Exception as ex:
            logger.exception(ex)
            self.assertTrue(False)
    
    
    def tearDown(self):
        self.aaa.terminate()            
        self.validation_agent.terminate()



if __name__ == '__main__':
    unittest.main()
