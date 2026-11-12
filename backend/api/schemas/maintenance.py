"""维护接口的 HTTP 契约。"""

from datetime import date

from pydantic import BaseModel, ConfigDict


class RetentionRequest(BaseModel):
    """按业务日期执行一次降池；不传则用后端当天。"""
    model_config = ConfigDict(extra="forbid")

    target_date: date | None = None


class RetentionResponse(BaseModel):
    cooled: int
    target_date: date
