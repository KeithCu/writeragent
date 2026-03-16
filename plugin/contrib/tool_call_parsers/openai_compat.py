class Function:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments

class ChatCompletionMessageToolCall:
    def __init__(self, id, type, function):
        self.id = id
        self.type = type
        self.function = function
    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments
            }
        }
