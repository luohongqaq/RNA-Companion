"""统一时间戳和后端业务日期的转换。"""
from datetime import date, datetime, time, timezone, tzinfo


class BackendTime:
    @staticmethod
    def utc(value: datetime) -> datetime:
        """将带时区的时间戳转换为 UTC。"""
        if value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @staticmethod
    def local(value: datetime | date, *, zone: tzinfo | None = None) -> datetime:
        """按后端系统时区换算时间戳，或取得指定日期的当地零点。"""
        if isinstance(value, datetime):
            return BackendTime.utc(value).astimezone(zone)
        midnight = datetime.combine(value, time.min)
        return midnight.astimezone() if zone is None else midnight.replace(tzinfo=zone)
