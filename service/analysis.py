# -*- coding: utf-8 -*-
"""QYH-GD300 故障定位系统 —— 数据分析层（纯计算，无 IO、无框架依赖）

职责（设计文档 7.5「分层不可越」）：
    把协议层交出来的"原始值"按用户自定义字段表换算成可展示的工程量，
    为表格 / 曲线 / 报警提供统一的数据形态。本模块不读文件、不起线程，
    由业务层（main.py）在解析出帧之后调用。

第一批已实现：
    · 按字段表从一帧里取值：文本链路取键（key），二进制链路按偏移取值（offset）
    · 线性换算 y = k·x + b
    · 按小数位格式化

第二批（此处已预留签名，暂以"原值透传"占位）：
    · 滤波：不滤波 / 滑动平均 / 中值 / 一阶低通
    · 报警判据：上限 / 下限 + 去抖 + 滞回
"""

from __future__ import annotations

import struct
from typing import Any, Iterable, Optional

from service.protocol import _ts_text

# 二进制原始类型 → (字节数, struct 格式符)
RAW_TYPES: dict[str, tuple[int, str]] = {
    "u8": (1, "B"), "i8": (1, "b"),
    "u16": (2, "H"), "i16": (2, "h"),
    "u32": (4, "I"), "i32": (4, "i"),
    "f32": (4, "f"),
}
DEFAULT_RAW_TYPE = "u16"
FILTER_TYPES: tuple[str, ...] = ("none", "moving_avg", "median", "low_pass")

COL_TS = "接收时刻"


def raw_size(raw_type: object) -> int:
    """原始类型的字节数（未知类型按 u16 处理）。"""
    entry = RAW_TYPES.get(str(raw_type).lower())
    return entry[0] if entry else RAW_TYPES[DEFAULT_RAW_TYPE][0]


def decode_raw(raw: bytes, offset: int, raw_type: object = DEFAULT_RAW_TYPE,
               byte_order: object = "big") -> Optional[float | int]:
    """从帧字节里按偏移/类型/字节序取出一个数值；越界或类型非法返回 None。"""
    entry = RAW_TYPES.get(str(raw_type).lower())
    if entry is None:
        return None
    size, fmt = entry
    if not isinstance(raw, (bytes, bytearray)):
        return None
    off = int(offset)
    if off < 0 or off + size > len(raw):
        return None
    prefix = "<" if str(byte_order).lower() == "little" else ">"
    try:
        return struct.unpack(prefix + fmt, bytes(raw[off:off + size]))[0]
    except struct.error:
        return None


def extract_raw(fields: Iterable[Any], values: Optional[dict] = None,
                raw: Optional[bytes] = None) -> dict[str, Any]:
    """按字段表从一帧里取出各字段的原始值。

    values：文本链路解析出的键值对（键 → 值）
    raw   ：二进制链路的整帧字节（按 source.offset 取值）
    """
    values = values if isinstance(values, dict) else {}
    out: dict[str, Any] = {}
    for f in fields:
        src = getattr(f, "source", None)
        src = src if isinstance(src, dict) else {}
        if str(src.get("type", "key")).lower() == "offset":
            out[f.id] = decode_raw(raw, src.get("offset", 0),
                                   src.get("raw_type", DEFAULT_RAW_TYPE),
                                   src.get("byte_order", "big"))
        else:
            out[f.id] = values.get(str(src.get("key", "")))
    return out


def to_engineering(value: Any, scale: Any) -> Optional[float]:
    """线性换算 y = k·x + b；非数值返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    s = scale if isinstance(scale, dict) else {}
    try:
        k = float(s.get("k", 1.0))
        b = float(s.get("b", 0.0))
    except (TypeError, ValueError):
        k, b = 1.0, 0.0
    return k * float(value) + b


def format_number(value: Optional[float], decimals: object = 0) -> str:
    """按小数位格式化；空值给占位符 '—'。"""
    if value is None:
        return "—"
    try:
        digits = int(decimals)
    except (TypeError, ValueError):
        digits = 0
    digits = max(0, min(6, digits))
    return f"{value:.0f}" if digits == 0 else f"{value:.{digits}f}"


def column_label(field: Any) -> str:
    """表格/曲线列名：显示名 + 可选单位。"""
    name = str(getattr(field, "name", "") or getattr(field, "id", ""))
    unit = str(getattr(field, "unit", "") or "")
    return f"{name}({unit})" if unit else name


def apply_filter(series: Iterable[float], spec: Any) -> Optional[float]:
    """第二批：对一串样本做滤波，返回最新平滑值（当前为原值透传占位）。"""
    values = [v for v in series if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return values[-1] if values else None


def judge_alarm(field: Any, value: Optional[float], state: Any = None) -> dict[str, Any]:
    """第二批：上限/下限 + 去抖 + 滞回判据（当前恒返回"未报警"，仅占位）。"""
    return {"level": None, "changed": False}


class Analyzer:
    """字段表 → 换算结果。可在运行中热换字段表（切换方案时调用 set_fields）。"""

    def __init__(self, fields: Iterable[Any] = ()) -> None:
        self._fields: list[Any] = []
        self.set_fields(fields)

    def set_fields(self, fields: Iterable[Any]) -> None:
        self._fields = list(fields or [])

    @property
    def fields(self) -> list[Any]:
        return list(self._fields)

    def columns(self) -> list[str]:
        """表格列名：接收时刻 + 进表的字段。"""
        cols = [COL_TS]
        for f in self._fields:
            if _visible(f).get("table", True):
                cols.append(column_label(f))
        return cols

    def process(self, ts: float, values: Optional[dict] = None,
                raw: Optional[bytes] = None) -> dict[str, Any]:
        """一帧 → {字段 id: {raw, value, text, name, unit}} + 表格行 cells。"""
        raws = extract_raw(self._fields, values, raw)
        fields_out: dict[str, Any] = {}
        cells = [_ts_text(ts)]
        for f in self._fields:
            rv = raws.get(f.id)
            value = to_engineering(rv, getattr(f, "scale", None))
            text = format_number(value, getattr(f, "decimals", 0))
            fields_out[f.id] = {
                "raw": rv,
                "value": value,
                "text": text,
                "name": str(getattr(f, "name", "") or f.id),
                "unit": str(getattr(f, "unit", "") or ""),
            }
            if _visible(f).get("table", True):
                cells.append(text)
        return {"ts": ts, "fields": fields_out, "cells": cells}


def _visible(field: Any) -> dict:
    vis = getattr(field, "visible", None)
    return vis if isinstance(vis, dict) else {}