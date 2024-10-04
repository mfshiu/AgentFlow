import signal

logger = __import__('helper').get_logger()
from agentflow.core.agent import Agent
from agentflow.core import config


def on_activate_a1(config):
    logger.verbose(f'config: {config}')
    
def on_activate_a2():
    logger.verbose(f'..')
    
    
class SubAgent(Agent):
    def __init__(self):
        agent_config = {'sub': 'Yes', config.ON_ACTIVATE: on_activate_a2}
        super().__init__(agent_config)
        
        
    def on_activate(self):
        logger.debug(f'config: {self.config}')
    
    
if __name__ == '__main__':
    logger.debug(f'***** Test Core *****')

    def signal_handler(signal, frame):
        logger.warning("System was interrupted.")
    signal.signal(signal.SIGINT, signal_handler)

    # a1 = Agent({config.ON_ACTIVATE: on_activate_a1})
    a1 = SubAgent()
    a1.start()
