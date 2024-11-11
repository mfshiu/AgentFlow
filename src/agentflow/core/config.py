from enum import Enum, auto



class LowercaseStrEnum(Enum):
    def _generate_next_value_(name, start, count, last_values):
        return name
    


class ConfigName(LowercaseStrEnum):
    CONCURRENCY_TYPE = auto()



class EventHandler(Enum):
    ON_ACTIVATE = auto()
    ON_PARENTS = auto()
    ON_CHILDREN = auto()



default_config = {
    ConfigName.CONCURRENCY_TYPE: ConfigName.CONCURRENCY_TYPE,
}
