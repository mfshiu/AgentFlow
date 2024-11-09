import threading
import multiprocessing
import queue
import time

# Define the Strategy interface
class WorkerStrategy:
    def start_worker(self):
        pass

    def send_data(self, data):
        pass

    def stop_worker(self):
        pass

# Concrete strategy for using threads
class ThreadWorker(WorkerStrategy):
    def __init__(self):
        self.stop_event = threading.Event()  # Event to stop the thread
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._worker)

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                message = self.queue.get(timeout=1)  # Get messages with timeout to check stop_event
                print(f"Thread worker received: {message}")
            except queue.Empty:
                continue  # Continue if there's no message
        print("Thread worker stopping gracefully.")

    def start_worker(self):
        print("Thread worker started.")
        self.thread.start()

    def send_data(self, data):
        self.queue.put(data)

    def stop_worker(self):
        self.stop_event.set()  # Signal the thread to stop
        self.thread.join()  # Wait for the thread to finish
        print("Thread worker has stopped.")

# Concrete strategy for using processes
class ProcessWorker(WorkerStrategy):
    def __init__(self):
        self.stop_event = multiprocessing.Event()  # Event to stop the process
        self.queue = multiprocessing.Queue()
        self.process = multiprocessing.Process(target=self._worker)

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                message = self.queue.get(timeout=1)  # Get messages with timeout to check stop_event
                print(f"Process worker received: {message}")
            except queue.Empty:
                continue  # Continue if there's no message
        print("Process worker stopping gracefully.")

    def start_worker(self):
        print("Process worker started.")
        self.process.start()

    def send_data(self, data):
        self.queue.put(data)

    def stop_worker(self):
        self.stop_event.set()  # Signal the process to stop
        self.process.join()  # Wait for the process to finish
        print("Process worker has stopped.")

# The context class which uses a strategy
class WorkerContext:
    def __init__(self, worker_type="thread"):
        if worker_type == "thread":
            self.worker_strategy = ThreadWorker()
        elif worker_type == "process":
            self.worker_strategy = ProcessWorker()
        else:
            raise ValueError("Invalid worker type. Choose 'thread' or 'process'.")

    def start_worker(self):
        self.worker_strategy.start_worker()

    def send_data(self, data):
        self.worker_strategy.send_data(data)

    def stop_worker(self):
        self.worker_strategy.stop_worker()

# Example usage
if __name__ == "__main__":
    # Choose between 'thread' and 'process'
    # worker_type = "thread"
    worker_type = "process"

    # Create the context and pass the desired strategy
    worker_context = WorkerContext(worker_type)
    
    # Start the worker
    worker_context.start_worker()

    # Send some data
    worker_context.send_data("Hello Worker!")
    time.sleep(2)
    worker_context.send_data("Another message")
    time.sleep(2)

    # Stop the worker
    worker_context.stop_worker()

    print("Main process finished.")
