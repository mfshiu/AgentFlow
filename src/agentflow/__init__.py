import logging
import os


LOGGER_NAME = os.getenv("LOGGER_NAME", "agentflow")
__logger:logging.Logger = logging.getLogger(LOGGER_NAME)


def get_logger():
    return __logger
