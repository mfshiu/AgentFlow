# from types import MethodType
import inspect


def print_value():
    print(999)
    
def print_value1(value):
    print(value)
    
class MyClass:
    def __init__(self):
        self.value = 777

    def set_print_value(self, print_func):
        # self.print_value = MethodType(print_func, self)
        self.print_value = print_func
        
    def print_value(self):
        print(self.value)


# Creating an instance of MyClass
obj = MyClass()
obj.set_print_value(print_value)

sig = inspect.signature(obj.print_value)
if n := len(sig.parameters) == 0:
    obj.print_value()
elif isinstance(sig.parameters.get('self'), MyClass):
    obj.print_value(obj)
else:
    obj.print_value(666)