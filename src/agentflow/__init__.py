import logging


LOGGER_NAME = "agentflow"
__logger = logging.getLogger(LOGGER_NAME)


def initialize_logger(name: str):
    global LOGGER_NAME, __logger
    if not isinstance(name, str) or len(name) == 0:
        raise ValueError("LOGGER_NAME must be a non-empty string")
    
    LOGGER_NAME = name
    __logger = logging.getLogger(LOGGER_NAME)  # 更新 logger
    return __logger


def get_logger():
    return __logger
