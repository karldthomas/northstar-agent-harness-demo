"""AWS Lambda adapter for the FastAPI simulator application."""

from mangum import Mangum

from .main import app
from .gateway_handler import handler as gateway_handler


_http_handler = Mangum(app, lifespan="off")


def handler(event, context):
    """Serve API Gateway HTTP requests and AgentCore Lambda-target calls."""

    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None)
    if isinstance(custom, dict) and custom.get("bedrockAgentCoreToolName"):
        return gateway_handler(event, context)
    return _http_handler(event, context)
