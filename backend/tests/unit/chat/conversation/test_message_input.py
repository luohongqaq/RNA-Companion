"""单元测试：发送一条用户消息，测试输入是否合理"""
from datetime import datetime, timedelta, timezone

import pytest

from module.chat.conversation.core.domain.contracts import SaveMessageInput


@pytest.fixture
def make_message():
    """默认创建合法输入，测试时可以覆盖指定字段。"""
    def factory(**changes):
        values = {
            "request_id": "request_001",
            "conversation_id": "conversation_001",
            "character_id": "character_deepseek",
            "content": "你好",
            "client_created_at": None,
        }
        values.update(changes)
        return SaveMessageInput(**values)

    return factory


def test_valid_input_passes_validation(make_message):
    command = make_message()

    command.validate()


# 四个必填字段，都不允许为空、全空白或非字符串。
@pytest.mark.parametrize(
    "field",
    ["request_id", "conversation_id", "character_id", "content"],
)
@pytest.mark.parametrize(
    "value",
    ["", "   ", "\n\t", None, 123, False],
)
def test_invalid_required_field_is_rejected(make_message, field, value):
    command = make_message(**{field: value})

    with pytest.raises(ValueError, match=f"{field} is required"):
        command.validate()


# 正文最短为一个非空白字符，最长为 100000 个字符。
@pytest.mark.parametrize("length", [1, 100_000])
def test_content_within_length_limit_is_allowed(make_message, length):
    command = make_message(content="啊" * length)

    command.validate()


def test_content_is_too_long_is_rejected(make_message):
    command = make_message(content="啊" * 100_001)

    with pytest.raises(
        ValueError,
        match="content cannot exceed 100000 characters",
    ):
        command.validate()


def test_client_created_at_can_be_none(make_message):
    command = make_message(client_created_at=None)

    command.validate()


# 即使字符串看起来是合法时间，业务对象这里也要求 datetime 类型。
@pytest.mark.parametrize(
    "value",
    ["2026-10-26T12:00:00+08:00", "", 123, False],
)
def test_non_datetime_client_created_at_is_rejected(make_message, value):
    command = make_message(client_created_at=value)

    with pytest.raises(
        ValueError,
        match="client_created_at must be a datetime",
    ):
        command.validate()


def test_client_created_at_without_timezone_is_rejected(make_message):
    command = make_message(
        client_created_at=datetime(2026, 10, 26, 12, 0),
    )

    with pytest.raises(
        ValueError,
        match="client_created_at must include a timezone",
    ):
        command.validate()


# UTC 和其他带时区的时间都应该允许。
@pytest.mark.parametrize(
    "tz",
    [
        timezone.utc,
        timezone(timedelta(hours=8)),
        timezone(timedelta(hours=-5)),
    ],
)
def test_client_created_at_with_timezone_is_allowed(make_message, tz):
    command = make_message(
        client_created_at=datetime(2026, 10, 26, 12, 0, tzinfo=tz),
    )

    command.validate()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-sv"]))
