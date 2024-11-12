import sys
import os
sys.path.append(os.path.abspath(".."))  # Adjust path if necessary

import signal
import threading
import time

from AgentFlow import log_helper
logger = log_helper.get_logger()

from agentflow.core.agent import Agent
from agentflow.core import config
from agentflow.core.config import ConfigName, EventHandler

from config_test import config_test

    
if __name__ == '__main__':
    logger.debug(f'***** Test *****')

    def signal_handler(signal, frame):
        logger.warning("System was interrupted.")
        a.terminate()
    signal.signal(signal.SIGINT, signal_handler)

    a = Agent('ccc', config_test)
    a.start_process()    

    time.sleep(1)
    while a.is_active():
        print('.', end='')
        time.sleep(1)
