import sys
import os
sys.path.append(os.path.abspath(".."))  # Adjust path if necessary

import unittest
import time

from agentflow.core.agent import Agent
from agentflow.core.config import EventHandler
from unit_test.config_test import config_test

from logging import Logger
logger:Logger = __import__('AgentFlow').get_logger()



class TestAgent(unittest.TestCase):
    children = {}
    parents = {}
    
    
    
    class ValidationAgent(Agent):
        def __init__(self):
            super().__init__(name='validation', agent_config=config_test)
            
            
        def on_connected(self):
            self._subscribe('children_count')
            self._subscribe('parents_count')

                
        def on_register_child(self, child_id, child_info:dict):
            logger.debug(f"self: {self}, child_id: {child_id}, child_info: {child_info}")
            if (parent_id := child_info['parent_id']) in TestAgent.children:
                TestAgent.children[parent_id].append(child_id)
            else:
                TestAgent.children[parent_id] = [child_id]
            logger.debug(f"TestAgent.children_counts: {TestAgent.children}")

                
        def on_register_parent(self, parent_id, parent_info:dict):
            logger.debug(f"self: {self}, parent_id: {parent_id}, parent_info: {parent_info}")
            if (child_id := parent_info['child_id']) in TestAgent.parents:
                TestAgent.parents[child_id].append(parent_id)
            else:
                TestAgent.parents[child_id] = [parent_id]
            logger.debug(f"TestAgent.parents_counts: {TestAgent.parents}")
        
        

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
        
        self.aaa_bbb = Agent(name='bbb.aaa', agent_config=cfg)
        self.aaa_bbb.start()
        self.aaa_ccc = Agent(name='ccc.aaa', agent_config=cfg)
        self.aaa_ccc.start()

        self.aaa_bbb_111 = Agent(name='111.bbb.aaa', agent_config=cfg)
        self.aaa_bbb_111.start()
        self.aaa_bbb_222 = Agent(name='222.bbb.aaa', agent_config=cfg)
        self.aaa_bbb_222.start()


    def _do_test_1(self):
        parents = TestAgent.parents.get(self.aaa.agent_id, [])
        children = TestAgent.children[self.aaa.agent_id]
        self.assertEqual(len(parents), 0)
        self.assertEqual(len(children), 2)
        self.assertTrue(self.aaa_bbb.agent_id in children)
        self.assertTrue(self.aaa_ccc.agent_id in children)
        
        parents = TestAgent.parents[self.aaa_bbb.agent_id]
        children = TestAgent.children.get(self.aaa_bbb.agent_id, [])
        self.assertEqual(len(parents), 1)
        self.assertEqual(len(children), 2)
        self.assertTrue(self.aaa.agent_id in parents)
        self.assertTrue(self.aaa_bbb_111.agent_id in children)
        self.assertTrue(self.aaa_bbb_222.agent_id in children)
        
        parents = TestAgent.parents[self.aaa_ccc.agent_id]
        children = TestAgent.children.get(self.aaa_ccc.agent_id, [])
        self.assertEqual(len(parents), 1)
        self.assertEqual(len(children), 0)
        self.assertTrue(self.aaa.agent_id in parents)
        
        parents = TestAgent.parents[self.aaa_bbb_111.agent_id]
        children = TestAgent.children.get(self.aaa_bbb_111.agent_id, [])
        self.assertEqual(len(parents), 1)
        self.assertEqual(len(children), 0)
        self.assertTrue(self.aaa_bbb.agent_id in parents)
        
        parents = TestAgent.parents[self.aaa_bbb_222.agent_id]
        children = TestAgent.children.get(self.aaa_bbb_222.agent_id, [])
        self.assertEqual(len(parents), 1)
        self.assertEqual(len(children), 0)
        self.assertTrue(self.aaa_bbb.agent_id in parents)


    def test_1(self):
        time.sleep(3)

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
