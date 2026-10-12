# -*- coding: utf-8 -*-
"""QYH-GD300 上位机 —— 长期归档（帧原始记录，可选开关，默认关闭）

定位：
    会话缓冲「退出即清」是硬约束；现场长跑需要一份可选的持久落盘。
    归档只存**帧原始记录**（用户确认口径）：接收时刻 + 链路类型 + 整帧 HEX
    （文本链路为整行文本），不存分析结果——协议口径/换算规则日后修正时，
    历史数据仍可用新方案重新解析。

文件组织：
    app_root()/archives/监听_YYYYMMDD.jsonl，按天分割；每行一个 JSON 对象：
        {"ts": 1697040000.123, "kind": "lora", "hex": "AA 55 ..."}
        文本链路行：{"ts": ..., "kind": "text", "text": "..."}
    总大小超过 max_mb 时从最旧文件开始删除（轮转；文件名即日期序）。

线程模型：append() 只被通信线程（hub 回调）调用；开关与文件列表来自 API
    线程，内部加锁。归档失败（磁盘不可写等）静默降级为不落盘，绝不影响监听主流程。
"""

from __future__ import annotations

import json
import pathlib
import re
import threading
import time
from typing import Any

from service.settings import app_root, read_settings, update_settings

ARCHIVE_KEY = "archive"                      # settings.json 中的配置键
DIR_NAME = "archives"
FILE_RE = re.compile(r"^监听_(\d{8})\.jsonl$")
DEFAULTS: dict[str, Any] = {"enabled": False, "max_mb": 200}
_ROTATE_CHECK_EVERY = 500                    # 每 N 次写入检查一次总大小


def read_archive_cfg() -> dict[str, Any]:
    """读取归档配置（损坏/缺省项回退默认值，读取路径不抛异常）。"""
    raw = read_settings().get(ARCHIVE_KEY)
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        cfg["enabled"] = bool(raw.get("enabled", False))
        try:
            cfg["max_mb"] = max(1, int(raw.get("max_mb", DEFAULTS["max_mb"])))
        except (TypeError, ValueError):
            pass
    return cfg


def save_archive_cfg(patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入归档配置（只改传入键，其余配置原样保留），返回归并后的配置。"""
    merged = read_archive_cfg()
    if "enabled" in patch:
        merged["enabled"] = bool(patch["enabled"])
    if "max_mb" in patch:
        try:
            merged["max_mb"] = max(1, int(patch["max_mb"]))
        except (TypeError, ValueError) as e:
            raise ValueError("max_mb 必须是正整数") from e
    update_settings({ARCHIVE_KEY: merged})
    return merged


class ArchiveStore:
    """按天分割的 JSONL 帧原始记录归档。"""

    def __init__(self, enabled: bool = False) -> None:
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._fh = None
        self._day = ""
        self._writes = 0

    # -------------------------------------------------- 开关
    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = bool(value)
            if not self._enabled:
                self._close_quiet()          # 关闭即收笔；重新开启时按天重开

    # -------------------------------------------------- 路径与列表
    @staticmethod
    def directory() -> pathlib.Path:
        return app_root() / DIR_NAME

    def list_files(self) -> list[dict[str, Any]]:
        """归档文件列表（按名字排序 == 按日期从旧到新）。"""
        out: list[dict[str, Any]] = []
        try:
            files = sorted(self.directory().glob("监听_*.jsonl"))
        except OSError:
            return out
        for p in files:
            try:
                st = p.stat()
                out.append({"name": p.name, "size": st.st_size,
                            "mtime": int(st.st_mtime)})
            except OSError:
                continue
        return out

    # -------------------------------------------------- 写入（通信线程）
    def append(self, records: list[dict[str, Any]]) -> None:
        """批量追加帧原始记录；未启用或无记录直接返回。"""
        if not records or not self._enabled:
            return
        stamp = time.strftime("%Y%m%d")
        with self._lock:
            if not self._enabled:            # 双检：等锁期间可能被关闭
                return
            try:
                if self._fh is None or stamp != self._day:
                    self._reopen(stamp)
                for rec in records:
                    self._fh.write(json.dumps(rec, ensure_ascii=False,
                                              separators=(",", ":")) + "\n")
                self._writes += 1
                self._fh.flush()
                if self._writes % _ROTATE_CHECK_EVERY == 0:
                    self._rotate()
            except OSError:
                self._close_quiet()          # 静默降级，下批再试

    def _reopen(self, stamp: str) -> None:
        self._close_quiet()
        directory = self.directory()
        directory.mkdir(parents=True, exist_ok=True)
        self._fh = open(directory / f"监听_{stamp}.jsonl", "a", encoding="utf-8")
        self._day = stamp
        self._rotate()                       # 打开时也查一次总大小

    def _rotate(self) -> None:
        """总大小超 max_mb 时从最旧文件开始删除。"""
        max_bytes = read_archive_cfg()["max_mb"] * 1024 * 1024
        files = self.list_files()
        total = sum(f["size"] for f in files)
        for f in files:
            if total <= max_bytes:
                break
            try:
                (self.directory() / f["name"]).unlink()
                total -= f["size"]
            except OSError:
                pass

    def _close_quiet(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        self._day = ""

    def close(self) -> None:
        with self._lock:
            self._close_quiet()
