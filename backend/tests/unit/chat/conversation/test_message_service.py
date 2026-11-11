"""MessageService 单元测试：运行真实服务，用 Fake 仓储替代数据库。"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from module.shared.context.types.visual import Expression, Pose, VisualState
from module.chat.conversation.core.processing.messages import MessageService
from module.chat.conversation.core.domain.contracts import AcceptedReply, SaveMessageInput, SaveMessageResult
from module.shared.common.errors.base import InvalidStateError, NotFoundError


class FakeChatRepository:
    """记录调用、提供固定结果；不执行任何数据库操作。"""

    def __init__(self):
        self.commands = []
        self.read_job_ids = []
        self.save_error = None
        self.read_error = None
        self.accepted_reply = None
        self.saved_result = SaveMessageResult(
            request_id="request_001",
            message_id="message_001",
            turn_id="turn_001",
            input_version=1,
            job_id="job_001",
        )

    async def accept_message(self, command):
        self.commands.append(command)
        if self.save_error is not None:
            raise self.save_error
        return self.saved_result

    async def read_accepted_reply(self, job_id):
        self.read_job_ids.append(job_id)
        if self.read_error is not None:
            raise self.read_error
        return self.accepted_reply


@pytest.fixture
def repository():
    # 每个测试获得一个新的 Fake，调用记录不会在测试之间共享。
    return FakeChatRepository()


@pytest.fixture
def command():
    return SaveMessageInput(
        request_id="request_001",
        conversation_id="conversation_001",
        character_id="character_test",
        content="你好",
    )


@pytest.mark.asyncio
async def test_invalid_input_does_not_call_repository_or_notify(repository, command):
    # 无效输入不能调用仓储或发送通知
    notifications = []
    service = MessageService(
        repository, notify_generation=lambda: notifications.append("notify"),
    )

    # validate 的详细边界已有独立测试；这里只验证服务转换异常并停止后续操作。
    with pytest.raises(InvalidStateError, match="content is required") as caught:
        await service.accept_message(replace(command, content=""))

    assert isinstance(caught.value.__cause__, ValueError)
    assert repository.commands == []
    assert notifications == []


@pytest.mark.asyncio
async def test_success_passes_original_command_and_returns_result_dict(repository, command):
    # 成功执行了T1业务后返回result回执
    service = MessageService(repository)

    result = await service.accept_message(command)

    assert len(repository.commands) == 1
    assert repository.commands[0] is command
    # 显式列出接口结果，避免用和实现相同的 asdict 再算一遍预期。
    assert result == {
        "request_id": "request_001",
        "message_id": "message_001",
        "turn_id": "turn_001",
        "input_version": 1,
        "job_id": "job_001",
        "save_state": "committed",
        "generation_state": "queued",
    }


@pytest.mark.asyncio
async def test_generation_is_notified_only_after_repository_finishes(command):
    # 仓储还没完成时，不能提前通知
    started = asyncio.Event()
    allow_finish = asyncio.Event()
    events = []

    class WaitingRepository(FakeChatRepository):
        async def accept_message(self, command):
            started.set()
            # 暂停仓储，模拟保存操作尚未结束；不靠 sleep 猜执行顺序。
            await allow_finish.wait()
            result = await super().accept_message(command)
            events.append("save_finished")
            return result

    service = MessageService(
        WaitingRepository(), notify_generation=lambda: events.append("notify"),
    )
    task = asyncio.create_task(service.accept_message(command))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert events == []
        assert not task.done()
        allow_finish.set()
        await asyncio.wait_for(task, timeout=2)
        assert events == ["save_finished", "notify"]
    finally:
        # 即使断言失败，也清理暂停中的任务。
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_repository_failure_propagates_without_notifying(repository, command):
    # 仓储失败时，异常上抛给调用者，但不发送通知
    error = RuntimeError("模拟保存失败")
    repository.save_error = error
    notifications = []
    service = MessageService(
        repository, notify_generation=lambda: notifications.append("notify"),
    )

    with pytest.raises(RuntimeError) as caught:
        await service.accept_message(command)

    assert caught.value is error
    assert repository.commands == [command]
    assert notifications == []


@pytest.mark.asyncio
async def test_notification_failure_still_returns_saved_result(repository, command):
    # 仓储成功，但通知失败时，仍然返回保存结果
    notifications = []

    def failing_notify():
        notifications.append("notify")
        raise RuntimeError("模拟通知失败")

    service = MessageService(repository, notify_generation=failing_notify)

    # 这里不应该抛异常：仓储已经返回成功，通知失败不能变成发送失败。
    result = await service.accept_message(command)

    assert notifications == ["notify"]
    assert repository.commands == [command]
    assert result["save_state"] == "committed"
    assert result["message_id"] == "message_001"


@pytest.mark.parametrize("job_id", ["", "   ", "\n\t", None, 123])
@pytest.mark.asyncio
async def test_invalid_reply_job_id_does_not_call_repository(repository, job_id):
    # 无效的job_id不能调用仓储
    service = MessageService(repository)

    with pytest.raises(InvalidStateError, match="job_id不能为空"):
        await service.reply_message(job_id)

    assert repository.read_job_ids == []


@pytest.mark.asyncio
async def test_reply_returns_none_when_repository_has_no_accepted_reply(repository):
    # 仓储没有对应记录时，返回None
    service = MessageService(repository)

    result = await service.reply_message("job_001")

    assert result is None
    assert repository.read_job_ids == ["job_001"]


@pytest.mark.parametrize("has_visual", [True, False])
@pytest.mark.asyncio
async def test_reply_returns_accepted_content_and_identifiers(repository, has_visual):
    # 仓储有对应记录时，返回组装结果
    created_at = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    visual = VisualState(Pose("pose_001", "站立"), Expression("expression_001", "微笑"))
    repository.accepted_reply = AcceptedReply(
        conversation_id="conversation_001",
        turn_id="turn_001",
        message_id="reply_001",
        content="你好呀！",
        visual=visual if has_visual else None,
        created_at=created_at,
    )
    service = MessageService(repository)

    result = await service.reply_message("job_001")

    assert result == repository.accepted_reply
    assert repository.read_job_ids == ["job_001"]


@pytest.mark.asyncio
async def test_reply_repository_error_propagates(repository):
    error = NotFoundError("生成任务不存在")
    repository.read_error = error
    service = MessageService(repository)

    with pytest.raises(NotFoundError) as caught:
        await service.reply_message("missing_job")

    assert caught.value is error
    assert repository.read_job_ids == ["missing_job"]
