import signal
import threading
import time

logger = __import__('helper').get_logger()
from agentflow.core.agent import Agent
from agentflow.core import config
from agentflow.core.config import ConfigName


def on_activate_a1(config):
    logger.verbose(f'config: {config}')
    
    
def on_activate_a2():
    logger.verbose(f'..')
    
    
test_config = {
    'version': '2',
    "broker_type": "MQTT",
    'username': 'eric',
    'password': 'eric123',
    'host': 'localhost',
    'port': 1884,
    "keepalive": 60,
    # ConfigName.START_METHOD: 'thread'
}
    
    
class AgentA(Agent):
    def __init__(self):
        cfg = {ConfigName.ON_ACTIVATE: on_activate_a1}
        cfg.update(test_config)
        super().__init__(name='aaa', agent_config=test_config)
        
        
    def on_activate(self):
        def test_notify():
            time.sleep(2)
            # self._notify_children('張學友 - 吻別 HQ', ('aaa', '222'), target_children=list(self._children.keys())[1], target_child_name='aaa.bbb')
            # self._notify_children('張學友 - 吻別 HQ', ('aaa', '222'))
            # self._notify_parents('大獅-修音', {'bbb': 789})
            self._notify_child(list(self._children.keys())[0], '毛豆', ('333', '2fff22'))
        threading.Thread(target=test_notify).start()
        
        
    def on_children(self, topic, info):
        # data = data.decode('utf-8', 'ignore')
        logger.debug(f'topic: {topic}, info: {info}')
    
    

class AgentB(Agent):
    def __init__(self):
        super().__init__(name='aaa.bbb', agent_config=test_config)
    
    

class AgentC(Agent):
    def __init__(self):
        super().__init__(name='aaa.bbb.ccc', agent_config=test_config)
        
        
    def on_parents(self, topic, info):
        # data = data.decode('utf-8', 'ignore')
        logger.debug(f'topic: {topic}, info: {info}')
    
    
    
if __name__ == '__main__':
    logger.debug(f'***** Test Core *****')

    def signal_handler(signal, frame):
        logger.warning("System was interrupted.")
        a1.terminate()
    signal.signal(signal.SIGINT, signal_handler)
        
    # a1 = Agent({config.ON_ACTIVATE: on_activate_a1})
    a1:Agent = AgentA()
    a1.start()
    
    # def stop():
    #     time.sleep(10)
    #     a1.terminate()
    # threading.Thread(target=stop).start()
    
    for _ in range(2):
        # bb = Agent(name='aaa.bbb', agent_config=test_config)
        bb = AgentB()
        bb.start()
        # logger.debug(bb.M("qwert"))
        # bb.on_activate_xx()
        
    # c1 = AgentC()
    # c1.start()

    try:
        while a1.is_active():
            time.sleep(1)  # Simulate long-running process
    except KeyboardInterrupt:
        logger.info("Caught KeyboardInterrupt, exiting.")
