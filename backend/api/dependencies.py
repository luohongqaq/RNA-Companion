"""从当前应用容器取出服务，供 FastAPI 路由注入。"""

from __future__ import annotations

from fastapi import Request

from api.container import ApplicationContainer
from module.chat.conversation.core.processing.messages import MessageService
from module.chat.conversation.core.processing.events import ChatEventService
from module.chat.conversation.core.processing.conversations import ConversationService
from module.character.core.processing.service import CharacterService
from module.model_settings.core.processing.service import ModelSettingsService
from module.user_edit.memory_edit.core.processing.edit_service import MemoryEditService
from module.user_edit.memory_edit.core.processing.query_service import MemoryQueryService
from module.chat.reply.core.processing.preparation import GenerationPreparationService
from module.chat.reply.core.processing.service import GenerationReplyService
from module.chat.reply.core.domain.errors.reply import ReplyModelUnavailable
from module.chat.reply.core.processing.query import GenerationQueryService


def get_container(request: Request) -> ApplicationContainer:
    # 从应用状态里拿容器：lifespan启动时放进去的，全进程只有一份
    return request.app.state.container


def get_message_service(request: Request) -> MessageService:
    # 给路由提供消息服务：每个请求来时从容器上取一次引用
    return get_container(request).message_service


def get_conversation_service(request: Request) -> ConversationService:
    return get_container(request).conversation_service


def get_character_service(request: Request) -> CharacterService:
    return get_container(request).character_service


def get_model_settings_service(request: Request) -> ModelSettingsService:
    return get_container(request).model_settings_service


def get_memory_query_service(request: Request) -> MemoryQueryService:
    service = get_container(request).memory_query_service
    if service is None:
        raise RuntimeError("记忆查询服务未初始化")
    return service


def get_memory_edit_service(request: Request) -> MemoryEditService:
    service = get_container(request).memory_edit_service
    if service is None:
        raise RuntimeError("记忆编辑服务未初始化")
    return service


def get_chat_event_service(request: Request) -> ChatEventService:
    return get_container(request).chat_event_service


def get_generation_preparation_service(request: Request) -> GenerationPreparationService:
    # 给路由提供回复生成的备料服务（当前没有路由在用，留给以后直接生成回复的接口）
    service = get_container(request).generation_preparation_service
    if service is None:
        raise RuntimeError("回复备料服务未初始化")
    return service


def get_generation_reply_service(request: Request) -> GenerationReplyService:
    # 给路由提供回复模型服务；功能没启用时抛业务错误，由统一异常处理转成HTTP响应
    service = get_container(request).reply_service
    if service is None:
        raise ReplyModelUnavailable("回复模型尚未启用，请配置RNA_REPLY_ENABLED")
    return service


def get_generation_query_service(request: Request) -> GenerationQueryService:
    # 给路由提供生成进度查询服务
    return get_container(request).generation_query_service
