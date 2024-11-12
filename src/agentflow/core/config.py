from enum import Enum, auto
    

class StrEnum(Enum):
    def _generate_next_value_(name, start, count, last_values):
        # print(name.lower())
        return name
        # return str(name).lower()



class EventHandler(StrEnum):
    ON_ACTIVATE = auto()
    ON_CHILDREN_MESSAGE = auto()
    ON_PARENTS_MESSAGE = auto()
    ON_REGISTER_CHILD = auto()
    ON_REGISTER_PARENT = auto()



# Config Names
CONCURRENCY_TYPE = 'CONCURRENCY_TYPE'


default_config = {
    CONCURRENCY_TYPE: 'process',
}
