"""Public fixture distribution does not offer local Vertex canary execution."""


class VertexCanaryRefused(RuntimeError):
    def public_terminal_error(self):
        return {"error": "provider_operation_terminal", "retryable": False}


class VertexCanaryOperation:
    pass


def operation_from_env():
    return None
