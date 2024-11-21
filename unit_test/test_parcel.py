import sys
import os
sys.path.append(os.path.abspath(".."))  # Adjust path if necessary

import random
import threading
import time
import unittest

from agentflow.core.agent import Agent
from agentflow.core.config import EventHandler
from agentflow.core.parcel import Parcel
from unit_test.config_test import config_test

from logging import Logger
logger:Logger = __import__('AgentFlow').get_logger()



class TestAgent(unittest.TestCase):
    binary_data = {}
    text_data = {}
    image_data = {}
    
    class ValidationAgent(Agent):
        def __init__(self):
            super().__init__(name='main', agent_config=config_test)


        def on_connected(self):
            self._subscribe('binary_payload')
            self._subscribe('text_payload')


        def on_message(self, topic:str, data):
            logger.debug(self.M(f"topic: {topic}, len(data): {len(data)}"))
            
            if 'binary_payload' == topic:
                TestAgent.binary_data = data
            elif 'text_payload' == topic:
                TestAgent.text_data = data


    def setUp(self):
        self.validation_agent = TestAgent.ValidationAgent()
        self.validation_agent.start_thread()
        
        # setup 1        
        def send_binary():
            def do_send():
                time.sleep(random.uniform(0.5, 1.5))
                self.binary_agent._publish('binary_payload', bytes([72, 101, 108, 108, 111]))
            threading.Thread(target=do_send).start()
        
        cfg = config_test.copy()
        cfg[EventHandler.ON_CONNECTED] = send_binary
        self.binary_agent = Agent(name='main.binary', agent_config=cfg)
        self.binary_agent.start()
                
        # setup 2
        def send_text():
            def do_send():
                time.sleep(random.uniform(0.5, 1.5))
                self.binary_agent._publish('text_payload', '早安, Anita.')
            threading.Thread(target=do_send).start()

        cfg = config_test.copy()
        cfg[EventHandler.ON_CONNECTED] = send_text
        self.text_agent = Agent(name='main.text', agent_config=cfg)
        self.text_agent.start()


    def _do_test_binary(self):
        logger.info(f'TestAgent.binary_data: {TestAgent.binary_data}')
        content = TestAgent.binary_data
        self.assertTrue(isinstance(content, bytes))
        self.assertEqual(content, bytes([72, 101, 108, 108, 111]))


    def _do_test_text(self):
        logger.info(f'TestAgent.text_data: {TestAgent.text_data}')
        content = TestAgent.text_data
        self.assertTrue(isinstance(content, str))
        self.assertEqual(content, '早安, Anita.')


    def test_1(self):
        time.sleep(3)

        try:
            self._do_test_binary()
            self._do_test_text()
        except Exception as ex:
            logger.exception(ex)
            self.assertTrue(False)


    def tearDown(self):
        self.validation_agent.terminate()
        self.text_agent.terminate()
        self.binary_agent.terminate()



if __name__ == '__main__':
    unittest.main()
