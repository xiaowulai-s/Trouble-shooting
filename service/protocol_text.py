# -*- coding: utf-8 -*-
"""QYH-GD300 故障定位系统 —— 上行文本行解析层（ASCII 协议）

背景：
    设备固件改为把数码管 / 滤波后的电信号值直接通过串口发出来，线上是
    「ASCII 文本行」，不是设计文档 3.3 的 25 字节定长帧。本模块是文本链路的
    协议层，与 protocol.py（二进制链路）并列，同样遵守 7.5「分层不可越」：
    纯计算、不做 IO、不起线程，由通信层喂字节、业务层取结果。

当前固件约定的形态（设备已实测确认）：
    23150\\r\\n              # 一行一个裸数字：数码管数值 = 谐振频率 Hz

解析器刻意做成「键值对超集」，固件往后改形态，上位机不用动：
    · 裸数字行（无 '='）自动归到频率字段 F —— 即固件当前的输出形态
    · 一行内可含多个键值对，逗号 / 分号分隔：A=1,B=2
    · 键名不认识也能用：首次出现即自动成为新列，列顺序 = 首次出现顺序
    · 预留 *XX 异或校验钩子（VERIFY_XOR_CHECKSUM=False 时只剥离不校验）

输出结构与 protocol.Decoder 对齐（snapshot 的 JSON 键名一律相同：
frames_ok / crc_err / resync / bytes_in / bytes_dropped / pending / columns /
rev / layout / layout_title …），另加 kind="text" 供界面区分文案。
这样界面与 API 不必为两条链路写两套代码。
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from service.protocol import _ts_text

TEXT_KEY = "text"
MAX_LINE = 512                 # 单行字节上限：超长视为噪声，防缓冲无限增长
MAX_KEYS = 16                  # 列数上限：一"行"冒出几十个键几乎一定是噪声
MAX_PAIRS = 16                 # 单行键值对上限，超过按无效行处理
KEEP_FRAMES = 200
DEFAULT_KEY = "F"              # 裸数字行归属的键（设备当前唯一上报量：频率 Hz）
VERIFY_XOR_CHECKSUM = False    # 预留钩子：固件若加 *XX 异或校验，改这里为 True

# 已知键的展示列名（未列出的键直接用键名）
KEY_LABELS = {"F": "F(Hz)"}

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,15}$")
_NUMBER_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]{2}$")


# ------------------------------------------------------------------ 伪布局
@dataclass(frozen=True)
class TextLayout:
    """文本链路的伪布局：只为让 layout_config / 界面用同一套字段名读取。

    二进制布局里的帧长、CRC 偏移等常数对文本行没有意义，统一给 0 / "-"，
    界面在 text 模式下不显示这些项。
    """

    key: str = TEXT_KEY
    title: str = "ASCII 文本行（数值 Hz）"
    note: str = ("设备按文本行上报数值：裸数字（如 23150）或 KEY=VALUE（如 F=23150）；"
                 "未知字段自动成列，改固件无需改上位机")
    frame_len: int = 0
    crc_offset: int = 0
    crc_covers: int = 0
    crc_order: str = "-"
    byte_order: str = "-"


TEXT_LAYOUT = TextLayout()


# ------------------------------------------------------------------ 解析结果
@dataclass
class TextFrame:
    """一行已成功解析的文本。"""

    layout: str = TEXT_KEY
    ts: float = 0.0                       # 上位机接收时刻
    values: dict[str, Any] = field(default_factory=dict)
    cells: list[str] = field(default_factory=list)
    raw_text: str = ""

    def to_payload(self) -> dict[str, Any]:
        """WebSocket 推送用的 JSON 友好结构（与 protocol.Frame.to_payload 对齐）。"""
        return {
            "ts": self.ts,
            "ts_text": _ts_text(self.ts),
            "cells": self.cells,
            "values": self.values,
            "raw": self.raw_text,
        }


def _cell_text(value: Any) -> str:
    """数值转表格文本：整数不带小数点，浮点最多 3 位小数。"""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _parse_value(text: str) -> Any:
    """把 VALUE 文本转成 int/float/str；非数字保留原文（便于后续加状态字）。"""
    if _NUMBER_RE.match(text):
        if "." in text:
            return float(text)
        return int(text)
    return text


def _split_pairs(text: str) -> list[str]:
    """按逗号 / 分号切键值对；不含 '=' 的片段再按空白切（容忍空格分隔）。"""
    out: list[str] = []
    for chunk in re.split(r"[;,]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            out.append(chunk)
        else:
            out.extend(chunk.split())
    return out


def _strip_checksum(text: str) -> str:
    """剥离尾部 *XX 校验字段（钩子）；开启校验时校验不过返回空串。"""
    star = text.rfind("*")
    if star <= 0:
        return text
    tail = text[star + 1:]
    if not _HEX_RE.match(tail):
        return text
    body = text[:star].strip()
    if not VERIFY_XOR_CHECKSUM:
        return body
    acc = 0
    for ch in body.encode("utf-8", "replace"):
        acc ^= ch
    return body if acc == int(tail, 16) else ""


# ------------------------------------------------------------------ 增量解析器
class TextLineDecoder:
    """字节流 → 结构化文本行。可被跨线程读取统计（内部加锁）。

    切行策略：
        1. 缓冲里按 '\\n' 切行，剥掉行尾 '\\r' 与空白；
        2. 行内解析 KEY=VALUE，动态维护列（键首次出现顺序）；
        3. 解析不出任何字段的行记一次"无效行"，字节计入丢弃量；
        4. 长时间收不到 '\\n'（超长行）时丢弃最旧字节，避免缓冲无限增长。
    """

    def __init__(self, keep: int = KEEP_FRAMES) -> None:
        self.layout = TEXT_LAYOUT
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._keys: list[str] = []
        self._frames_ok = 0
        self._bad_lines = 0
        self._bytes_in = 0
        self._bytes_dropped = 0
        self._last_ts: Optional[float] = None
        self._recent: deque[TextFrame] = deque(maxlen=keep)
        self._rev = 0                      # 链路版本号：清空后自增，界面据此清表

    # -------------------------------------------------- 解析
    def feed(self, chunk: bytes, ts: Optional[float] = None) -> list[TextFrame]:
        """喂入一段原始字节，返回本次新解析出的行（可能为空）。"""
        if not chunk:
            return []
        now = time.time() if ts is None else ts
        out: list[TextFrame] = []

        with self._lock:
            self._bytes_in += len(chunk)
            self._buf += chunk

            while True:
                idx = self._buf.find(b"\n")
                if idx < 0:
                    if len(self._buf) > MAX_LINE:      # 超长行：丢最旧的字节
                        drop = len(self._buf) - MAX_LINE
                        del self._buf[:drop]
                        self._bytes_dropped += drop
                        self._bad_lines += 1
                    break

                raw = bytes(self._buf[:idx])
                del self._buf[:idx + 1]
                frame = self._parse(raw, now)
                if frame is None:
                    self._bad_lines += 1
                    self._bytes_dropped += len(raw) + 1
                else:
                    out.append(frame)
                    self._recent.append(frame)
                    self._frames_ok += 1
                    self._last_ts = now
                if len(out) >= 64:                     # 单次别做太久，剩下的下轮再切
                    break
        return out

    def _parse(self, raw: bytes, ts: float) -> Optional[TextFrame]:
        """解析一行；解析不出任何字段返回 None（调用方按无效行计数）。"""
        text = _strip_checksum(raw.decode("utf-8", "replace").strip())
        if not text:
            return None

        tokens = _split_pairs(text)
        if not tokens or len(tokens) > MAX_PAIRS:
            return None

        values: dict[str, Any] = {}
        for token in tokens:
            if "=" in token:
                key, _, value = token.partition("=")
                key, value = key.strip(), value.strip()
            elif _NUMBER_RE.match(token) and DEFAULT_KEY not in values:
                key, value = DEFAULT_KEY, token        # 裸数字行：按 F 处理
            else:
                continue
            if not _KEY_RE.match(key) or not value or len(value) > 32:
                continue
            if len(self._keys) >= MAX_KEYS and key not in self._keys:
                continue                               # 列已满：忽略新键，不污染表格
            if key not in self._keys:
                self._keys.append(key)                 # 键首次出现 → 自动成新列
            values[key] = _parse_value(value)

        if not values:
            return None

        cells = [_ts_text(ts)]
        for key in self._keys:
            cells.append(_cell_text(values[key]) if key in values else "—")

        return TextFrame(ts=ts, values=values, cells=cells, raw_text=text)

    # -------------------------------------------------- 读取
    def columns(self) -> list[str]:
        """当前表格列名（接收时刻 + 各字段），随键首次出现动态增长。"""
        with self._lock:
            return ["接收时刻"] + [KEY_LABELS.get(k, k) for k in self._keys]

    def snapshot(self, recent: int = 0) -> dict[str, Any]:
        """解析统计（+ 可选最近行），供 /api/protocol 与界面统计条使用。

        JSON 键名与 protocol.Decoder.snapshot 保持一致；text 链路没有 CRC 与
        重同步概念，"crc_err" 承载"无效行"计数，"resync" 恒为 0。
        """
        with self._lock:
            data: dict[str, Any] = {
                "type": "protocol",
                "kind": "text",
                "layout": TEXT_KEY,
                "layout_title": self.layout.title,
                "layout_note": self.layout.note,
                "frame_len": 0,
                "crc_offset": 0,
                "covers": 0,
                "crc_order": "-",
                "byte_order": "-",
                "rev": self._rev,
                "crc_label": "无校验（预留 *XX 钩子）",
                "columns": ["接收时刻"] + [KEY_LABELS.get(k, k) for k in self._keys],
                "frames_ok": self._frames_ok,
                "crc_err": self._bad_lines,
                "resync": 0,
                "bytes_in": self._bytes_in,
                "bytes_dropped": self._bytes_dropped,
                "last_ts": self._last_ts,
                "pending": len(self._buf),
            }
            if recent > 0:
                data["recent"] = [f.to_payload() for f in list(self._recent)[-recent:]]
        return data

    def reset(self) -> None:
        """清空缓冲、列与统计（清屏用）。"""
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        """清空累计量（调用方必须已持锁）。"""
        self._buf.clear()
        self._keys.clear()
        self._frames_ok = 0
        self._bad_lines = 0
        self._bytes_in = 0
        self._bytes_dropped = 0
        self._last_ts = None
        self._recent.clear()
        self._rev += 1