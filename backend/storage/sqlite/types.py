"""存的时候统一成 UTC，取的时候也统一返回 UTC，展示给用户时再转成本地时间"""

from datetime import datetime

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator
from module.shared.common.time import BackendTime


class UTCDateTime(TypeDecorator[datetime]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> str | None:
        if value is None:
            return None
        return BackendTime.utc(value).isoformat()

    def process_result_value(self, value: str | None, dialect) -> datetime | None:
        if value is None:
            return None
        return BackendTime.utc(datetime.fromisoformat(value))
