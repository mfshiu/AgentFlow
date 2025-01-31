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


def ensure_size(text: str, max_length: int = 300) -> str:
    """
    Ensure a text size is less than or equal to the specified max length.
    If the text exceeds the max length, return a truncated version ending with '..'.

    Args:
        text (str): The input string.
        max_length (int): The maximum allowed length of the string. Default is 300.

    Returns:
        str: The original text if within the limit, otherwise a truncated version.
    """
    if text:
        if len(text) <= max_length:
            return text
        else:
            return text[:max_length - 2] + '..'
    else:
        return text
