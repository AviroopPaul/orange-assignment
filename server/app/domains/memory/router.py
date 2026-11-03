import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.identifiers import IDENTIFIER_PATTERN
from app.domains.chat.service import ChatService
from app.domains.memory.schemas import MemoryAction, MemoryCorrection, MemoryList, MemoryRead
from app.domains.memory.service import MemoryNotActive, MemoryNotFound, MemoryService
from app.web import get_chat_service, get_memory_service

router = APIRouter(tags=["memory"])


def _not_available(error: Exception) -> HTTPException:
    if isinstance(error, MemoryNotFound):
        return HTTPException(status_code=404, detail="memory not found")
    return HTTPException(status_code=409, detail="this memory is no longer current")


@router.get("/memories", response_model=MemoryList)
async def list_memories(
    user_id: str = Query(pattern=IDENTIFIER_PATTERN),
    memory: MemoryService = Depends(get_memory_service),
) -> MemoryList:
    return MemoryList(memories=await memory.list_all(user_id))


@router.post("/memories/{memory_id}/confirm", response_model=MemoryRead)
async def confirm_memory(
    memory_id: uuid.UUID,
    body: MemoryAction,
    memory: MemoryService = Depends(get_memory_service),
    chat: ChatService = Depends(get_chat_service),
) -> MemoryRead:
    try:
        result = await memory.confirm_by_id(body.user_id, memory_id)
    except (MemoryNotFound, MemoryNotActive) as error:
        raise _not_available(error) from error
    await chat.settle_memory_changes(body.user_id)
    return result


@router.put("/memories/{memory_id}", response_model=MemoryRead)
async def correct_memory(
    memory_id: uuid.UUID,
    body: MemoryCorrection,
    memory: MemoryService = Depends(get_memory_service),
    chat: ChatService = Depends(get_chat_service),
) -> MemoryRead:
    try:
        result = await memory.correct_by_id(body.user_id, memory_id, body.value)
    except (MemoryNotFound, MemoryNotActive) as error:
        raise _not_available(error) from error
    await chat.settle_memory_changes(body.user_id)
    return result


@router.delete("/memories/{memory_id}", status_code=204)
async def forget_memory(
    memory_id: uuid.UUID,
    user_id: str = Query(pattern=IDENTIFIER_PATTERN),
    memory: MemoryService = Depends(get_memory_service),
    chat: ChatService = Depends(get_chat_service),
) -> None:
    try:
        await memory.forget_by_id(user_id, memory_id)
    except (MemoryNotFound, MemoryNotActive) as error:
        raise _not_available(error) from error
    await chat.settle_memory_changes(user_id)
