"""FastAPI dependencies. The container is built once in the app lifespan."""

from fastapi import Request


def get_chat_service(request: Request):
    return request.app.state.container.chat


def get_trace_service(request: Request):
    return request.app.state.container.traces


def get_system_service(request: Request):
    return request.app.state.container.core.system
