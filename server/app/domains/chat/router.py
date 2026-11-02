from fastapi import APIRouter, Depends

from app.domains.chat.schemas import ChatRequest, ChatResponse
from app.domains.chat.service import ChatService
from app.web import get_chat_service

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def post_chat(
    request: ChatRequest, chat: ChatService = Depends(get_chat_service)
) -> ChatResponse:
    return await chat.handle_message(request)
