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


def ensure_size(string: str, max_length: int = 300) -> str:
    """
    Ensure a string size is less than or equal to the specified max length.
    If the string exceeds the max length, return a truncated version ending with '..'.

    Args:
        string (str): The input string.
        max_length (int): The maximum allowed length of the string. Default is 300.

    Returns:
        str: The original string if within the limit, otherwise a truncated version.
    """
    if len(string) <= max_length:
        return string
    else:
        return string[:max_length - 2] + '..'
