import signal
import threading
import time

logger = __import__('helper').get_logger()
from agentflow.core.agent import Agent
from agentflow.core import config
from agentflow.core.config import ConfigName, EventHandler


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
    ConfigName.CONCURRENCY_TYPE: 'thread'
}
    
    
class AgentParent(Agent):
    def __init__(self):
        cfg = {EventHandler.ON_ACTIVATE: on_activate_a1}
        cfg.update(test_config)
        super().__init__(name='aaa', agent_config=cfg)
        
        
    def on_children_message(self, topic, info):
        # data = data.decode('utf-8', 'ignore')
        logger.debug(f'topic: {topic}, info: {info}')
    
    

class AgentChild(Agent):
    def __init__(self):
        super().__init__(name='aaa.bbb', agent_config=test_config)
        
        
    def on_activate(self):
        def test_notify():
            time.sleep(2)
            self._notify_children('張學友 - 吻別 HQ', ('aaa', '222'))
            self._notify_parents('大獅-修音', {'bbb': 789})
        
        threading.Thread(target=test_notify).start()
    
    
    
if __name__ == '__main__':
    logger.debug(f'***** Test Multi-Parents Multi-Children Specify 1 Parent *****')
    
    parents = []        

    def signal_handler(signal, frame):
        logger.warning("System was interrupted.")
        for a in parents:
            a.terminate()
    signal.signal(signal.SIGINT, signal_handler)

    for _ in range(2):
        a = AgentParent()
        a.start()
        parents.append(a)
        AgentChild().start()

    time.sleep(1)
    while parents[0].is_active():
        print('.', end='')
        time.sleep(1)
