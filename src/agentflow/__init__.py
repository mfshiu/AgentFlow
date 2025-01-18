import logging
import os


LOGGING_LEVEL_VERBOSE = int(logging.DEBUG / 2)
logging.addLevelName(LOGGING_LEVEL_VERBOSE, "VERBOSE")

def verbose(self, message, *args, **kwargs):
    if self.isEnabledFor(LOGGING_LEVEL_VERBOSE):
        self._log(LOGGING_LEVEL_VERBOSE, message, args, **kwargs, stacklevel=2)

logging.Logger.verbose = verbose


LOGGER_NAME = os.getenv("LOGGER_NAME", "agentflow")
__logger:logging.Logger = logging.getLogger(LOGGER_NAME)

def get_logger() -> logging.Logger:
    return __logger
