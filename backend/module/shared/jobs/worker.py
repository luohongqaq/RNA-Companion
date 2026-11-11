"""单协程任务执行器的公共生命周期、通知与轮询调度。"""
import asyncio
from abc import ABC, abstractmethod
import logging
from math import isfinite

logger = logging.getLogger(__name__)


class Worker(ABC):
    def __init__(self, *, name: str, poll_interval: float = 1.0) -> None:
        if not isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("扫描间隔必须为正数")
        self._name, self._poll_interval = name, poll_interval
        self._wake = asyncio.Event()
        self._lifecycle = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """旧协程结束后才允许恢复；恢复失败直接交给应用启动流程。"""
        async with self._lifecycle:
            if self._task is not None and not self._task.done():
                return
            if self._task is not None:
                try:
                    self._task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    logger.error("上次%s协程已异常结束（%s）", self._name, type(error).__name__)
            await self.recover()
            self._task = asyncio.create_task(self.run(), name=self._name)
            logger.info("后台执行器已启动：%s", self._name,
                        extra={"component": "background", "stage": self._name, "event": "worker_started"})

    def notify(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        async with self._lifecycle:
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    # 消费已结束协程的异常，使关闭流程仍能释放其余资源。
                    logger.error("%s协程已异常结束（%s）", self._name, type(error).__name__)
                logger.info("后台执行器已停止：%s", self._name,
                            extra={"component": "background", "stage": self._name, "event": "worker_stopped"})

    async def run(self) -> None:
        while True:
            # 扫描期间到达的通知保留到本轮等待；持久化任务仍由轮询兜底。
            self._wake.clear()
            try:
                if await self.process_next():
                    continue
            except NotImplementedError:
                # 开发阶段明确暴露未完成入口，不反复扫描占位任务。
                raise
            except Exception as error:
                logger.error("%s本轮未完成（%s）", self._name, type(error).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass

    @abstractmethod
    async def recover(self) -> None:
        """只在旧协程已结束、下一次调度开始前恢复本类任务。"""
        ...

    @abstractmethod
    async def process_next(self) -> bool:
        """串行推进一次调度，返回是否有进展；任务异常由workflow处理。"""
        ...
