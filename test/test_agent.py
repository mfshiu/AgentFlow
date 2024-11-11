import unittest
from src.agentflow.core.agent import Agent

import threading
import time

logger = __import__('helper').get_logger()
from agentflow.core.agent import Agent
from agentflow.core import config
from agentflow.core.config import ConfigName, EventHandler
from config0 import test_config



class AgentA(Agent):


    def __init__(self):
        # cfg = {EventHandler.ON_ACTIVATE: on_activate_a1}
        # cfg.update(test_config)
        print(f'XXX test_config: {test_config}')
        super().__init__(name='aaa', agent_config=test_config)

        self.children_count = 0
        
        
    def on_activate(self):
        logger.debug(f'AgentA on_activate')
        def test_notify():
            time.sleep(2)
            self._notify_child(list(self._children.keys())[0], '巴登諾克', ('222', 'rrr'))
            # self._notify_children('張學友 - 吻別 HQ', ('aaa', '222'))
            # self._notify_parents('大獅-修音', {'bbb': 789})
        threading.Thread(target=test_notify).start()


    def on_register_child(self, child_id, child_info:dict):
        self.children_count += 1
        print(f"self.children_count: {self.children_count}")
        with open("temp.txt", "w") as file:
            file.write(f"children_count={self.children_count}")


class AgentB(Agent):


    def __init__(self):
        super().__init__(name='aaa.bbb', agent_config=test_config)
        self.parents_count = 0
        
        
    def on_activate(self):
        logger.debug(f'AgentB on_activate')


    def on_register_parent(self, parent_id, parent_info):
        print('EEEEEEEEEE')
        self.parents_count += 1
        with open("temp1.txt", "w") as file:
            file.write(f"parents_count={self.parents_count}")



class TestAgent(unittest.TestCase):


    def setUp(self):
        self.agent_aaa = AgentA()
        self.agent_bbb = AgentB()


    def test_initialization(self):
        self.agent_aaa.start()
        self.agent_bbb.start()
        # self.assertIsNotNone(self.agent)


    def test_some_functionality(self):
        time.sleep(3)
        # result = self.agent.some_method() 
        # expected_result = 1
        with open("temp.txt", "r") as file:
            content = file.read()
        children_count = int(content.split("=")[1])        
        self.assertEqual(children_count, 1)

        with open("temp1.txt", "r") as file:
            content = file.read()
        parents_count = int(content.split("=")[1])        
        self.assertEqual(parents_count, 1)



if __name__ == '__main__':
    unittest.main()
