# -*- coding: utf-8 -*-
"""QYH-GD300 故障定位系统 —— 上行帧协议解析层

职责边界（设计文档 7.5「分层不可越」）：
    本模块属于协议层：纯计算，不引用任何 UI 框架、不做 IO、不起线程，
    由通信层喂字节、业务层取结果。

两套帧布局并存（契约尚未冻结）：
    LAYOUT_25   设计文档 3.3 上行帧（自述为"三方协同开发的唯一基准"）
                25 字节，多字节字段大端序，CRC 覆盖偏移 0–22
    LAYOUT_14   《方案评估报告》7.1 精简建议版（未评审）
                14 字节，CRC 覆盖偏移 0–11
    默认 LAYOUT_25，可用启动参数切到 14。

CRC 参数：CRC-16/MODBUS（poly 0xA001 反射、init 0xFFFF、无最终异或）。
    注意：CRC 字段自身的字节序文档未定义，此处按"低位在前"落地（与既有
    mock 一致）；若嵌入式组确认是大端，只改 CRC_ORDER 一个常量即可。

尚未定论、解析器不做假设的地方：
    · MSG_TYPE 取值（下表取自评估报告 7.1，设计文档未给全）
    · 25 字节帧中 TIMESTAMP / PEAK / AVG / NOISE / PULSE_CNT 约 40% 字段
      暂无数据来源，解析出来可能恒为 0——这是协议问题，不是解析器问题
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

SYNC = 0xAA55
SYNC_BYTES = b"\xAA\x55"        # 大端序下 0xAA55 的线上字节
CRC_ORDER = "little"           # CRC 字段字节序（待确认）

MSG_TYPES = {
    0x01: "周期上报",
    0x02: "报警",
    0x03: "心跳",
    0x04: "应答",
    0x05: "补传",
}


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS：poly 0xA001（反射）、init 0xFFFF、无最终异或。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


# ------------------------------------------------------------------ 布局描述
@dataclass(frozen=True)
class Field:
    """帧内一个字段：偏移 + 字节数，多字节按所属布局的字节序解析。"""
    name: str
    offset: int
    size: int
    label: str
    show: bool = True           # 是否进入结构化表格（同步头/保留字不展示）


@dataclass(frozen=True)
class Layout:
    """一套帧布局：字段表 + CRC 位置 + FLAG 位定义。

    档 1+ 起，四项常数（帧长 / CRC 偏移 / CRC 覆盖长度 / 字节序）可由界面调整，
    字段表本身仍固定——即"常数可调、字段不可编辑"。
    """
    key: str
    title: str
    frame_len: int
    crc_offset: int
    fields: tuple[Field, ...]
    flag_bits: tuple[tuple[int, str], ...]              # (位序号, 名称)
    level_bits: Optional[tuple[int, int]] = None        # (起始位, 位宽) 报警等级
    note: str = ""
    covers: Optional[int] = None                        # CRC 覆盖长度，None 表示 = crc_offset
    crc_order: str = CRC_ORDER                          # CRC 字段自身字节序
    byte_order: str = "big"                             # 多字节字段字节序

    @property
    def crc_covers(self) -> int:
        """CRC 覆盖长度（默认等于 CRC 字段偏移）。"""
        return self.crc_offset if self.covers is None else self.covers

    @property
    def columns(self) -> list[str]:
        """表格列名：接收时刻 + 展示字段 + CRC。"""
        return ["接收时刻"] + [f.label for f in self.fields if f.show] + ["CRC"]


LAYOUT_25 = Layout(
    key="25",
    title="25 字节 / 设计文档 3.3",
    frame_len=25,
    crc_offset=23,
    fields=(
        Field("SYNC", 0, 2, "同步头", show=False),
        Field("VER", 2, 1, "协议版本", show=False),
        Field("MSG_TYPE", 3, 1, "帧类型"),
        Field("DEV_ID", 4, 2, "节点地址"),
        Field("SEQ", 6, 2, "帧序号"),
        Field("TIMESTAMP", 8, 4, "节点时间戳"),
        Field("FLAG", 12, 1, "状态标志"),
        Field("PEAK", 13, 2, "峰值"),
        Field("AVG", 15, 2, "平均"),
        Field("NOISE", 17, 2, "噪声基线"),
        Field("PULSE_CNT", 19, 2, "脉冲计数"),
        Field("BATTERY", 21, 1, "电池"),
        Field("RSVD", 22, 1, "保留", show=False),
    ),
    flag_bits=((0, "报警锁存"), (3, "传感器故障"), (4, "低电")),
    level_bits=(1, 2),
    note="设计文档 3.3 上行帧（唯一基准，大端序）",
)

LAYOUT_14 = Layout(
    key="14",
    title="14 字节 / 评估报告 7.1",
    frame_len=14,
    crc_offset=12,
    fields=(
        Field("SYNC", 0, 2, "同步头", show=False),
        Field("VER", 2, 1, "协议版本", show=False),
        Field("MSG_TYPE", 3, 1, "帧类型"),
        Field("DEV_ID", 4, 2, "节点地址"),
        Field("SEQ", 6, 2, "帧序号"),
        Field("STRENGTH", 8, 2, "强度"),
        Field("BATTERY", 10, 1, "电池"),
        Field("FLAG", 11, 1, "状态标志"),
    ),
    flag_bits=((0, "报警锁存"), (1, "传感器故障"), (2, "低电")),
    level_bits=None,
    note="评估报告 7.1 精简建议版（未评审，强度位宽待确认）",
)

LAYOUTS: dict[str, Layout] = {LAYOUT_25.key: LAYOUT_25, LAYOUT_14.key: LAYOUT_14}
DEFAULT_LAYOUT = LAYOUT_25
LAYOUT_OVERRIDES = ("frame_len", "crc_offset", "covers", "crc_order", "byte_order")


def _validate_layout_params(layout: Layout) -> None:
    """校验布局常数（档 1+ 界面可改这些值，必须保证切帧/取字段不越界）。"""
    if layout.crc_offset < 2 or layout.crc_offset + 2 > layout.frame_len:
        raise ValueError(f"CRC 偏移 {layout.crc_offset} 非法：帧长 {layout.frame_len}，"
                         f"CRC 字段（2 字节）必须位于帧内")
    if layout.crc_covers > layout.crc_offset:
        raise ValueError(f"CRC 覆盖长度 {layout.crc_covers} 非法：不能超过 CRC 偏移 "
                         f"{layout.crc_offset}")
    for f in layout.fields:
        if f.offset < 0 or f.offset + f.size > layout.frame_len:
            raise ValueError(f"字段 {f.label} 越界：偏移 {f.offset} 长度 {f.size}，"
                             f"超出帧长 {layout.frame_len}")


def _as_int(label: str, value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} 必须是整数：{value!r}") from None


def build_layout(base: Layout, **overrides: Any) -> Layout:
    """基于预设布局套用常数覆盖，返回新布局。

    档 1+ 的唯一入口：字段表（fields/flag_bits）与各展示规则不变，
    只有 LAYOUT_OVERRIDES 里的五项常数可改；非法值抛 ValueError。
    """
    for k, v in overrides.items():
        if k not in LAYOUT_OVERRIDES:
            raise ValueError(f"不可调整的布局项 {k!r}（仅支持：{'/'.join(LAYOUT_OVERRIDES)}）")
    covers = overrides.get("covers", base.covers)
    layout = Layout(
        key=base.key,
        title=base.title,
        frame_len=_as_int("帧长", overrides.get("frame_len", base.frame_len)),
        crc_offset=_as_int("CRC 偏移", overrides.get("crc_offset", base.crc_offset)),
        fields=base.fields,
        flag_bits=base.flag_bits,
        level_bits=base.level_bits,
        note=base.note,
        covers=None if covers is None else _as_int("CRC 覆盖长度", covers),
        crc_order=str(overrides.get("crc_order", base.crc_order)).lower(),
        byte_order=str(overrides.get("byte_order", base.byte_order)).lower(),
    )
    if layout.frame_len < 4:
        raise ValueError(f"帧长 {layout.frame_len} 非法：至少 4 字节")
    if layout.crc_order not in ("little", "big"):
        raise ValueError(f"CRC 字节序非法：{layout.crc_order!r}（可选 little/big）")
    if layout.byte_order not in ("little", "big"):
        raise ValueError(f"字段字节序非法：{layout.byte_order!r}（可选 little/big）")
    _validate_layout_params(layout)
    return layout


def resolve_layout(spec: object) -> Layout:
    """把启动参数/配置值解析成布局；非法值抛 ValueError（由调用方报错退出）。"""
    key = str(spec).strip()
    if key in LAYOUTS:
        return LAYOUTS[key]
    raise ValueError(f"未知帧布局 {spec!r}，可选：{'/'.join(LAYOUTS)}")


# ------------------------------------------------------------------ 解析结果
def _ts_text(ts: float) -> str:
    lt = time.localtime(ts)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{int(ts % 1 * 1000):03d}"


def _flag_text(layout: Layout, flag: int) -> str:
    parts: list[str] = []
    if layout.level_bits is not None:
        shift, width = layout.level_bits
        level = (flag >> shift) & ((1 << width) - 1)
        if level:
            parts.append(f"等级{level}")
    for bit, name in layout.flag_bits:
        if (flag >> bit) & 1:
            parts.append(name)
    return "、".join(parts) if parts else "—"


@dataclass
class Frame:
    """一帧已通过 CRC 校验的上行帧。"""
    layout: str
    ts: float                      # 上位机接收时刻（节点未必有时间戳）
    crc: int
    values: dict[str, int] = field(default_factory=dict)
    cells: list[str] = field(default_factory=list)
    raw_hex: str = ""

    @property
    def dev_id(self) -> int:
        return self.values.get("DEV_ID", 0)

    @property
    def seq(self) -> int:
        return self.values.get("SEQ", 0)

    @property
    def msg_type(self) -> int:
        return self.values.get("MSG_TYPE", 0)

    def to_payload(self) -> dict[str, Any]:
        """WebSocket 推送用的 JSON 友好结构（表格单元格已在服务端排好序）。"""
        return {
            "ts": self.ts,
            "ts_text": _ts_text(self.ts),
            "dev_id": self.dev_id,
            "seq": self.seq,
            "msg_type": self.msg_type,
            "msg_name": MSG_TYPES.get(self.msg_type, f"0x{self.msg_type:02X}"),
            "crc": f"{self.crc:04X}",
            "cells": self.cells,
            "raw": self.raw_hex,
        }


# ------------------------------------------------------------------ 增量解析器
class Decoder:
    """字节流 → 结构化帧。可被跨线程读取统计（内部加锁）。

    切帧策略（与设计文档 9 章"串口粘包/断帧"用例对应）：
        1. 在缓冲里找同步头 AA 55，其前的字节计为丢弃并记一次重同步；
        2. 不足一帧则等待更多数据；
        3. 够一帧就按布局取 CRC 与计算值比对：相等则出一帧、整帧消费；
           不等则记一次 CRC 错、滑过 1 字节重新找同步头（保证不会卡死）。
    """

    def __init__(self, layout: Layout = DEFAULT_LAYOUT, keep: int = 200) -> None:
        self.layout = layout
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._frames_ok = 0
        self._crc_err = 0
        self._resync = 0
        self._bytes_in = 0
        self._bytes_dropped = 0
        self._last_ts: Optional[float] = None
        self._recent: deque[Frame] = deque(maxlen=keep)
        self._rev = 0                     # 布局版本号：换布局后自增，界面据此清表

    # -------------------------------------------------- 解析
    def feed(self, chunk: bytes, ts: Optional[float] = None) -> list[Frame]:
        """喂入一段原始字节，返回本次新解析出的帧（可能为空）。"""
        if not chunk:
            return []
        now = time.time() if ts is None else ts
        layout = self.layout
        need = layout.frame_len
        out: list[Frame] = []

        with self._lock:
            self._bytes_in += len(chunk)
            self._buf += chunk
            buf = self._buf

            while True:
                idx = buf.find(SYNC_BYTES)
                if idx < 0:
                    # 没有同步头：末字节可能是被截断的 0xAA，留 1 字节再看
                    keep_last = 1 if (buf and buf[-1] == SYNC_BYTES[0]) else 0
                    drop = len(buf) - keep_last
                    if drop > 0:
                        del buf[:drop]
                        self._bytes_dropped += drop
                        self._resync += 1
                    break
                if idx > 0:
                    del buf[:idx]
                    self._bytes_dropped += idx
                    self._resync += 1
                if len(buf) < need:
                    break

                candidate = bytes(buf[:need])
                crc_rx = int.from_bytes(
                    candidate[layout.crc_offset:layout.crc_offset + 2], layout.crc_order
                )
                if crc_rx == crc16_modbus(candidate[:layout.crc_covers]):
                    frame = self._build(candidate, now, crc_rx)
                    out.append(frame)
                    self._recent.append(frame)
                    self._frames_ok += 1
                    self._last_ts = now
                    del buf[:need]
                else:
                    # CRC 不符：这一字节不是帧起点，滑 1 字节继续找
                    self._crc_err += 1
                    self._resync += 1
                    self._bytes_dropped += 1
                    del buf[:1]
                if out and len(out) >= 64:      # 单次别做太久，剩下的下轮再切
                    break
        return out

    def _build(self, raw: bytes, ts: float, crc: int) -> Frame:
        layout = self.layout
        values: dict[str, int] = {}
        for f in layout.fields:
            values[f.name] = int.from_bytes(raw[f.offset:f.offset + f.size], layout.byte_order)

        cells = [_ts_text(ts)]
        for f in layout.fields:
            if not f.show:
                continue
            v = values[f.name]
            if f.name == "MSG_TYPE":
                cells.append(MSG_TYPES.get(v, f"0x{v:02X}"))
            elif f.name == "FLAG":
                cells.append(_flag_text(layout, v))
            elif f.name == "BATTERY":
                cells.append(f"{v * 0.1:.1f}V")
            elif f.name == "TIMESTAMP":
                cells.append(time.strftime("%H:%M:%S", time.gmtime(v)) if v else "—")
            else:
                cells.append(str(v))
        cells.append(f"{crc:04X}")

        return Frame(
            layout=layout.key,
            ts=ts,
            crc=crc,
            values=values,
            cells=cells,
            raw_hex=raw.hex(" ").upper(),
        )

    # -------------------------------------------------- 读取
    def set_layout(self, layout: Layout) -> dict[str, Any]:
        """运行中切换布局：清空缓冲与统计（旧帧按新列解释会串位），返回新统计。

        串口线程可能正在 feed，必须与 feed/_build 用同一把锁。
        """
        with self._lock:
            self.layout = layout
            self._reset_locked()
            self._rev += 1
        return self.snapshot()

    def snapshot(self, recent: int = 0) -> dict[str, Any]:
        """解析统计（+ 可选最近帧），供 /api/protocol 与界面统计条使用。"""
        layout = self.layout
        with self._lock:
            data: dict[str, Any] = {
                "type": "protocol",
                "kind": "binary",
                "layout": layout.key,
                "layout_title": layout.title,
                "layout_note": layout.note,
                "frame_len": layout.frame_len,
                "crc_offset": layout.crc_offset,
                "covers": layout.crc_covers,
                "crc_order": layout.crc_order,
                "byte_order": layout.byte_order,
                "rev": self._rev,
                "crc_label": "CRC-16/MODBUS",
                "columns": layout.columns,
                "frames_ok": self._frames_ok,
                "crc_err": self._crc_err,
                "resync": self._resync,
                "bytes_in": self._bytes_in,
                "bytes_dropped": self._bytes_dropped,
                "last_ts": self._last_ts,
                "pending": len(self._buf),
            }
            if recent > 0:
                data["recent"] = [f.to_payload() for f in list(self._recent)[-recent:]]
        return data

    def _reset_locked(self) -> None:
        """清空累计量（调用方必须已持锁）。"""
        self._buf.clear()
        self._frames_ok = 0
        self._crc_err = 0
        self._resync = 0
        self._bytes_in = 0
        self._bytes_dropped = 0
        self._last_ts = None
        self._recent.clear()

    def reset(self) -> None:
        with self._lock:
            self._reset_locked()