"""进程内后台运行的通用发布/订阅基建（工作流与 API 测试执行共用，D-021）。

模式：事件缓冲（晚接入的订阅者经 subscribe() 重放）+ 订阅队列列表 + None 结束哨兵。
调用方：register() 注册运行（同 key 已有运行直接复用，调用方退化为订阅者）；
执行协程用 publish()/publish_key() 发事件、finish()/finish_key() 收尾
（done 标记 + 唤醒全部订阅者 + 注销）；HTTP 层 subscribe() 接入队列后用 forward_events() 转为 SSE。
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi.responses import StreamingResponse

# SSE 心跳间隔：两次事件之间可能长时间没有数据（一个节点里可能串行跑几十次 LLM 调用，
# 期间只在节点结束时发一条事件），而空闲的 HTTP 长连接会被 nginx（proxy_read_timeout
# 默认 60s）、云负载均衡与企业代理掐断——表现就是前端弹「订阅连接中断」而后台一切正常。
# 定期发一条 SSE 注释行（以 ":" 开头，客户端一律忽略）让连接上始终有数据流动（D-045）。
SSE_HEARTBEAT_SECONDS = 15

SSE_COMMENT = ": keep-alive\n\n"

# SSE 响应头：禁缓存 + 关掉 nginx 的响应缓冲（不关的话事件会被攒着不发，
# 长连接看起来像卡住不动）
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


@dataclass
class RunHandle:
    """一次后台运行的运行态：事件缓冲（供订阅者重放）+ 订阅队列列表"""

    events: list = field(default_factory=list)
    subscribers: list = field(default_factory=list)
    done: bool = False
    task: Any = None


def sse_event(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def sse_response(hub: "RunHub", handle: RunHandle, queue: asyncio.Queue) -> StreamingResponse:
    """把运行订阅包成 SSE 响应：media type 与禁缓冲响应头只在这里定义一次"""
    return StreamingResponse(
        hub.forward_events(handle, queue),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


class RunHub:
    """按 key 管理进程内运行：同 key 重复注册直接复用既有运行（调用方退化为订阅者）"""

    def __init__(self) -> None:
        self._runs: dict[str, RunHandle] = {}
        # 串行化「查状态 → 抢占 → 注册」临界区，避免并发 start 的检查竞态
        self.lock = asyncio.Lock()

    def register(self, key: str, spawn: Callable[[], Any]) -> RunHandle:
        """注册并启动后台任务；同 key 已有运行时直接返回该运行（spawn 不再执行）"""
        existing = self._runs.get(key)
        if existing is not None:
            return existing
        handle = RunHandle()
        self._runs[key] = handle  # 先注册再 spawn：执行协程内可立即 get() 到自身
        handle.task = spawn()
        return handle

    def get(self, key: str) -> RunHandle | None:
        return self._runs.get(key)

    def publish(self, handle: RunHandle, event: dict) -> None:
        handle.events.append(event)
        for queue in handle.subscribers:
            queue.put_nowait(event)

    def publish_key(self, key: str, event: dict) -> None:
        """按 key 发布（执行协程未持有 handle 时使用）；运行已收尾则为无操作"""
        handle = self._runs.get(key)
        if handle is not None:
            self.publish(handle, event)

    def subscribe(self, handle: RunHandle) -> asyncio.Queue:
        """注册订阅队列并重放已有事件（无 await 的同步段，无并发竞态）"""
        queue: asyncio.Queue = asyncio.Queue()
        for event in handle.events:
            queue.put_nowait(event)
        handle.subscribers.append(queue)
        return queue

    def unsubscribe(self, handle: RunHandle, queue: asyncio.Queue) -> None:
        if queue in handle.subscribers:
            handle.subscribers.remove(queue)

    def finish(self, key: str, handle: RunHandle) -> None:
        """收尾：标记完成、唤醒全部订阅者（None 哨兵）、注销运行"""
        handle.done = True
        for queue in handle.subscribers:
            queue.put_nowait(None)
        self._runs.pop(key, None)

    def finish_key(self, key: str) -> None:
        handle = self._runs.get(key)
        if handle is not None:
            self.finish(key, handle)

    async def forward_events(self, handle: RunHandle, queue: asyncio.Queue):
        """SSE 订阅者：转发后台运行的事件；客户端断开只影响订阅本身，执行不受影响。

        队列必须在端点内（register 之后、返回响应之前）同步接入，
        保证运行先于订阅完成时的事件也能经 handle.events 重放。
        空闲超过 SSE_HEARTBEAT_SECONDS 就发一条注释行保活（见该常量的说明）。
        """
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=SSE_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # 取消 queue.get() 不会丢事件：值仍在队列里并会唤醒下一个等待者
                    yield SSE_COMMENT
                    continue
                if event is None:
                    break
                yield sse_event(event)
        finally:
            self.unsubscribe(handle, queue)
        yield "data: [DONE]\n\n"
