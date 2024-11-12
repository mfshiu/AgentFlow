import sys
import os
sys.path.append(os.path.abspath(".."))  # Adjust path if necessary

import threading
import time
from config_test import config_test

from agentflow.core.agent import Agent


# logger = __import__('log_helper').get_logger()
from AgentFlow import log_helper
logger = log_helper.get_logger()
logger.debug('AGi 儲存無限靈感，手機擴充神器 – TF138 三合一')



class AgentA(Agent):


    def __init__(self):
        # cfg = {EventHandler.ON_ACTIVATE: on_activate_a1}
        # cfg.update(test_config)
        print(f'XXX test_config: {config_test}')
        super().__init__(name='aaa', agent_config=config_test)

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



if __name__ == '__main__':
    agent_aaa = AgentA()
    agent_aaa.start()
    time.sleep(3)
    agent_aaa.terminate()
