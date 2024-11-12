from typing import Callable
from logging import Logger
import multiprocessing
import queue
import threading

# import agentflow.core.agent as aa


logger:Logger = __import__('agentflow').get_logger()



# Define the Strategy interface
class Worker:
    def __init__(self, initiator_agent):
        self.initiator_agent = initiator_agent
    
    
    def is_working(self):
        if hasattr(self, 'terminate_event'):
            return not self.terminate_event.is_set()
        else:
            return False


    def send_data(self, data):
        pass

    
    def start(self):
        pass
    
    
    def stop(self):
        pass



# Concrete strategy for using processes
class ProcessWorker(Worker):
    def __init__(self, initiator_agent):
        super().__init__(initiator_agent)
        

    def start(self):
        logger.debug(self.initiator_agent.M(f"self.initiator_agent: {self.initiator_agent}"))
        self.work_queue = multiprocessing.Queue()
        self.terminate_event = multiprocessing.Event()
        
        cfg = self.initiator_agent.config
        cfg['work_queue'] = self.work_queue
        cfg['terminate_event'] = self.terminate_event
        self.process = multiprocessing.Process(target=self.initiator_agent._activate, args=(cfg,))
        self.process.start()


    def send_data(self, data):
        self.work_queue.put(data)


    def stop(self):
        logger.debug(self.initiator_agent.M("Stopping.."))
        self.send_data('terminate')
        self.process.join()  # Wait for the process to finish
        logger.debug(self.initiator_agent.M("Stopped."))



# Concrete strategy for using threads
class ThreadWorker(Worker):
    def __init__(self, initiator_agent):
        super().__init__(initiator_agent)


    def start(self):
        logger.debug("Thread worker")
        self.work_queue = queue.Queue()
        self.terminate_event = threading.Event()
        
        cfg = self.initiator_agent.config
        cfg['work_queue'] = self.work_queue
        cfg['terminate_event'] = self.terminate_event
        self.thread = threading.Thread(target=self.initiator_agent._activate, args=(cfg,))
        self.thread.start()


    def send_data(self, data):
        logger.debug(self.initiator_agent.M(f"data: {data}"))
        self.work_queue.put(data)


    def stop(self):
        logger.debug(self.initiator_agent.M("Stopping.."))
        self.send_data('terminate')
        self.thread.join()  # Wait for the process to finish
        logger.debug(self.initiator_agent.M("Stopped."))
