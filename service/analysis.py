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

第二批已实现：
    · 滤波：不滤波 / 滑动平均 / 中值 / 一阶低通（逐字段独立、各自维护历史）
    · 报警判据：上限 / 下限 + 去抖 + 滞回 + 自动解警
    · 报警事件：状态跳变时产出结构化事件，供业务层入库与推送界面
"""

from __future__ import annotations

import statistics
import struct
from collections import deque
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


def hist_capacity(spec: Any) -> int:
    """按滤波器类型给出需要保留的历史长度（deque 上限）。"""
    data = spec if isinstance(spec, dict) else {}
    ftype = str(data.get("type", "none")).strip().lower()
    if ftype in ("moving_avg", "median"):
        return max(1, min(999, _as_int(data.get("window"), 5)))
    if ftype == "low_pass":
        return 512                       # 一阶低通的"记忆"越长越平滑，取一个够用的上限
    return 1


def apply_filter(series: Iterable[float], spec: Any) -> Optional[float]:
    """对一串按时间升序的样本做滤波，返回最新的平滑值。

    spec = {"type": none|moving_avg|median|low_pass, "window": int, "alpha": float}
      · none       原值透传
      · moving_avg 最近 window 个样本的算术平均
      · median     最近 window 个样本的中位数（抑制脉冲噪声）
      · low_pass   一阶 IIR：y = α·x + (1-α)·y，α 越小越平滑
    无有效样本返回 None。
    """
    values = [float(v) for v in series
              if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not values:
        return None
    data = spec if isinstance(spec, dict) else {}
    ftype = str(data.get("type", "none")).strip().lower()
    if ftype == "moving_avg":
        window = values[-max(1, min(999, _as_int(data.get("window"), 5))):]
        return sum(window) / len(window)
    if ftype == "median":
        window = values[-max(1, min(999, _as_int(data.get("window"), 5))):]
        return float(statistics.median(window))
    if ftype == "low_pass":
        alpha = min(1.0, max(0.0, _as_float(data.get("alpha"), 0.2)))
        y = values[0]
        for x in values[1:]:
            y = alpha * x + (1.0 - alpha) * y
        return y
    return values[-1]


def _judge_side(cfg: Any, sub: dict, value: float, ts: float,
                is_high: bool, active: bool) -> Optional[str]:
    """判定一"侧"阈值：返回 enter / exit / hold / pending / None。

    · enter   ：满足进入条件且去抖时间已到
    · exit    ：已在报警且已越过滞回带（可自动解警）
    · hold    ：已在报警但仍在滞回带内（维持报警，防抖）
    · pending ：已触阈但去抖时间未到
    · None    ：正常
    """
    data = cfg if isinstance(cfg, dict) else {}
    if not data.get("enabled"):
        sub["pending"] = None
        return None
    limit = _as_float(data.get("limit"), 0.0)
    hyst = abs(_as_float(data.get("hysteresis"), 0.0))
    debounce = max(0.0, _as_float(data.get("debounce_s"), 0.0))
    if is_high:
        over = value >= limit                     # 进入条件
        back = value <= limit - hyst              # 退出条件（滞回带下沿）
    else:
        over = value <= limit
        back = value >= limit + hyst
    if active:
        sub["pending"] = None
        return "exit" if back else "hold"
    if not over:
        sub["pending"] = None
        return None
    if debounce <= 0:
        return "enter"
    if sub.get("pending") is None:
        sub["pending"] = ts
    return "enter" if ts - sub["pending"] >= debounce else "pending"


def judge_alarm(field: Any, value: Optional[float], state: Any, ts: float) -> dict[str, Any]:
    """上限/下限 + 去抖 + 滞回 + 自动解警的判据（有状态，需逐帧调用）。

    返回 {"level": None|"high"|"low", "changed": bool, "side": "high"|"low"|None,
          "limit": float|None, "active": bool}
      · level  当前报警级别（None 表示正常）
      · changed 本次调用是否发生状态跳变（进入或解除）
      · side   跳变发生在哪一侧（进入=触发侧，解除=原触发侧）
    """
    alarm = getattr(field, "alarm", None)
    alarm = alarm if isinstance(alarm, dict) else {}
    state = state if isinstance(state, dict) else {}
    current = state.get("level")
    if current not in ("high", "low"):
        current = None

    if value is None:                            # 本帧取不到值：维持现状，不误判
        return {"level": current, "changed": False, "side": None,
                "limit": None, "active": current is not None}

    sub_high = state.setdefault("high", {"pending": None})
    sub_low = state.setdefault("low", {"pending": None})

    # 已在报警：先看原触发侧能否解除，再考虑另一侧；正常态：上限优先
    order = [("high", True), ("low", False)] if current != "low" else [("low", False), ("high", True)]
    level, changed, side, limit = current, False, None, None
    for key, is_high in order:
        cfg = alarm.get(key)
        outcome = _judge_side(cfg, sub_high if is_high else sub_low, value, ts, is_high, current == key)
        if outcome == "hold":
            level = key
            break
        if outcome == "enter":
            level, changed, side = key, True, key
            limit = _as_float((cfg or {}).get("limit") if isinstance(cfg, dict) else 0.0, 0.0)
            break
        if outcome == "exit":
            level, changed, side = None, True, key
            limit = _as_float((cfg or {}).get("limit") if isinstance(cfg, dict) else 0.0, 0.0)
            break
        # None / pending：继续评估另一侧
    state["level"] = level
    return {"level": level, "changed": changed, "side": side,
            "limit": limit, "active": level is not None}


def alarm_event(field: Any, value: Optional[float], verdict: dict) -> dict[str, Any]:
    """把一次报警状态跳变整理成结构化事件（供入库 + 推送界面）。"""
    name = str(getattr(field, "name", "") or getattr(field, "id", ""))
    unit = str(getattr(field, "unit", "") or "")
    decimals = getattr(field, "decimals", 0)
    side = verdict.get("side")
    limit = verdict.get("limit")
    if verdict.get("active"):
        verb = "超过上限" if side == "high" else "低于下限"
        text = f"{name} {verb} {format_number(limit, decimals)}{unit}"
    else:
        verb = "上限已恢复" if side == "high" else "下限已恢复"
        text = (f"{name} {verb}（当前 "
                f"{format_number(value, decimals)}{unit}）")
    return {
        "field": str(getattr(field, "id", "")),
        "name": name,
        "unit": unit,
        "side": side,
        "active": bool(verdict.get("active")),
        "level": verdict.get("level"),
        "value": value,
        "limit": limit,
        "decimals": int(decimals) if isinstance(decimals, int) else 0,
        "text": text,
    }


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


class Analyzer:
    """字段表 → 换算结果。可在运行中热换字段表（切换方案时调用 set_fields）。

    第二批起带状态：
      · _hist    逐字段的"工程量"历史，供滤波取窗口（换字段表时清空）
      · _alarms  逐字段的报警状态（去抖计时 / 当前级别），换字段表时一并清空
    """

    def __init__(self, fields: Iterable[Any] = ()) -> None:
        self._fields: list[Any] = []
        self._hist: dict[str, deque] = {}
        self._alarms: dict[str, dict] = {}
        self.set_fields(fields)

    def set_fields(self, fields: Iterable[Any]) -> None:
        """热换字段表：历史与报警状态一并清零（旧口径的状态对新字段无意义）。"""
        self._fields = list(fields or [])
        self._hist = {}
        self._alarms = {}

    def reset(self) -> None:
        """只清历史与报警状态，保留字段表（清屏/重新计时用）。"""
        self._hist = {}
        self._alarms = {}

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

    def _history(self, field: Any) -> deque:
        """取（或按滤波类型新建）某字段的历史队列。"""
        hist = self._hist.get(field.id)
        if hist is None:
            hist = deque(maxlen=hist_capacity(getattr(field, "filter", None)))
            self._hist[field.id] = hist
        return hist

    def process(self, ts: float, values: Optional[dict] = None,
                raw: Optional[bytes] = None) -> dict[str, Any]:
        """一帧 → {字段 id: {raw, value, text, name, unit, filtered, alarm}} + 表格行 cells。

        · value：滤波后的工程量（制表 / 画曲线 / 判报警都用它）
        · raw  ：协议层取到的原始值；value_raw 为未滤波的工程量（备查）
        · alarms：本帧发生的报警状态跳变（进入/解除），供业务层入库与推送
        """
        raws = extract_raw(self._fields, values, raw)
        fields_out: dict[str, Any] = {}
        cells = [_ts_text(ts)]
        alarms: list[dict] = []
        for f in self._fields:
            rv = raws.get(f.id)
            eng = to_engineering(rv, getattr(f, "scale", None))

            fspec = getattr(f, "filter", None)
            hist = self._history(f)
            if eng is not None:
                hist.append(eng)
            ftype = str((fspec or {}).get("type", "none")).strip().lower() \
                if isinstance(fspec, dict) else "none"
            if ftype != "none" and hist:
                value = apply_filter(list(hist), fspec)
                filtered = True
            else:
                value = eng
                filtered = False

            text = format_number(value, getattr(f, "decimals", 0))
            verdict = judge_alarm(f, value, self._alarms.setdefault(f.id, {}), ts)
            if verdict["changed"]:
                alarms.append(alarm_event(f, value, verdict))
            fields_out[f.id] = {
                "raw": rv,
                "value": value,
                "value_raw": eng,
                "text": text,
                "filtered": filtered,
                "alarm": verdict["level"],
                "name": str(getattr(f, "name", "") or f.id),
                "unit": str(getattr(f, "unit", "") or ""),
            }
            if _visible(f).get("table", True):
                cells.append(text)
        return {"ts": ts, "fields": fields_out, "cells": cells, "alarms": alarms}


def _visible(field: Any) -> dict:
    vis = getattr(field, "visible", None)
    return vis if isinstance(vis, dict) else {}