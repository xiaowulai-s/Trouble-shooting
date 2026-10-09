# -*- coding: utf-8 -*-
"""QYH-GD300 上位机 v0.1 —— WebSocket 推送层

线程安全设计：
    串口线程（SerialHub）在非事件循环线程里调用 broadcast()，
    内部用 loop.call_soon_threadsafe 把消息转交给事件循环；
    每个客户端一个独立队列，队列满时丢弃最旧消息（慢客户端不拖垮内存）。
"""

from __future__ import annotations

import asyncio
from typing import Optional, Set


class WsHub:
    QUEUE_SIZE = 256

    def __init__(self) -> None:
        self._clients: Set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._latest: Optional[dict] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def register(self) -> asyncio.Queue:
        """在事件循环线程中调用，返回该客户端的专属队列。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self._clients.add(queue)
        if self._latest is not None:
            queue.put_nowait(self._latest)      # 新客户端立刻拿到当前状态
        return queue

    def unregister(self, queue: asyncio.Queue) -> None:
        self._clients.discard(queue)

    def broadcast(self, message: dict) -> None:
        """任意线程可调用；状态类消息会被缓存给后接入的客户端。"""
        if message.get("type") == "status":
            self._latest = message
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, message)
        except RuntimeError:
            pass

    def _fanout(self, message: dict) -> None:
        for queue in list(self._clients):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()          # 丢最旧，保最新
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    pass