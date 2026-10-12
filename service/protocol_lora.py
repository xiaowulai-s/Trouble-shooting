# -*- coding: utf-8 -*-
"""QYH-GD300 上位机 —— LoRa 主从通用帧解析层（只读监听）

依据 docs/LoRa主机从机通信协议说明书.md v1.0（2026-10-10）：
    通用帧 = AA 55 | 目标地址(2B) | 源地址(2B) | Seq(1B) | Cmd(1B) | Len(1B)
             | 载荷(NB) | CRC16(2B)
    · 固定开销 11B，整帧 = 11 + Len（Len 0~255 → 整帧上限 266B）；
    · 多字节字段小端；CRC-16/MODBUS（poly 0xA001 反射、init 0xFFFF），
      覆盖「自目标地址起至载荷末」（不含帧头）——说明书【新增定义·待确认】口径，
      嵌入式组若确认不同，只需改 CRC_START_OFFSET / CRC_ORDER 两个常量。

定位（需求方确认）：上位机是总线上的**只读监听者**，主机轮询与从机应答/上报
    都会出现在线上，本层一律列出并标注方向；不向总线写任何字节。
    载荷结构说明书标注【待定义】，本层不解释载荷，仅以 HEX 展示；
    0xA0 告警上报本批仅在表格展示（暂不做报警联动）。

分层边界与 protocol.py / protocol_text.py 一致：纯计算、不做 IO、不起线程，
由通信层喂字节、业务层取结果；输出结构与 Decoder 对齐（snapshot 的 JSON 键名
相同），另加 kind="lora" 供界面区分文案。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from service.protocol import SYNC_BYTES, crc16_modbus, _ts_text

LORA_KEY = "lora"

FIXED_OVERHEAD = 11          # 帧头2 + 目标2 + 源2 + Seq1 + Cmd1 + Len1 + CRC2
HEADER_LEN = 9               # 目标2 + 源2 + Seq1 + Cmd1 + Len1（不含帧头）
LEN_OFFSET = 8               # Len 字节在「帧头之后」的偏移（2+2+1+1）
MAX_PAYLOAD = 255
MAX_FRAME = FIXED_OVERHEAD + MAX_PAYLOAD
CRC_START_OFFSET = 2         # CRC 覆盖起点：目标地址（即跳过 2B 帧头）
CRC_ORDER = "little"         # CRC 字段自身小端（低字节在前）【待确认口径】
BYTE_ORDER = "little"        # 多字节字段小端
PAYLOAD_SHOW = 12            # 载荷 HEX 最多展示的字节数，超出以 … 截断
KEEP_FRAMES = 200

# 地址与命令区间（说明书第 2/4 章）
MASTER_ADDR = 0x0000
BROADCAST_ADDR = 0xFFFF
CMD_RESP_MIN, CMD_RESP_MAX = 0x81, 0x87      # 从机应答区间
CMD_REPORT_MIN, CMD_REPORT_MAX = 0xA0, 0xA1  # 从机主动上报区间

# 命令码 → 展示名（说明书第 4 章修正后）
LORA_CMDS: dict[int, str] = {
    0x01: "读数据", 0x02: "读参数", 0x03: "写参数", 0x04: "控制命令",
    0x05: "心跳", 0x06: "时间同步(预留)", 0x07: "广播",
    0x81: "读数据应答", 0x82: "读参数应答", 0x83: "写参数应答",
    0x84: "控制命令应答", 0x85: "心跳应答", 0x86: "时间同步应答(预留)",
    0x87: "广播分槽确认",
    0xA0: "告警上报", 0xA1: "事件上报", 0xB0: "告警确认", 0xB1: "事件确认",
}


def cmd_name(cmd: int) -> str:
    return LORA_CMDS.get(cmd, f"0x{cmd:02X}")


def build_frame(dst: int, src: int, seq: int, cmd: int,
                payload: bytes = b"") -> bytes:
    """按说明书组一帧（小端 + MODBUS CRC16，覆盖目标地址→载荷末）。

    供 mock 模拟源与自测脚本使用；监听链路本身不发送任何字节。
    """
    if not 0 <= dst <= 0xFFFF or not 0 <= src <= 0xFFFF:
        raise ValueError("地址必须是 2 字节（0~0xFFFF）")
    if not 0 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError(f"载荷长度越界：{len(payload)}（上限 {MAX_PAYLOAD}）")
    body = bytearray()
    body += int(dst).to_bytes(2, BYTE_ORDER)
    body += int(src).to_bytes(2, BYTE_ORDER)
    body.append(int(seq) & 0xFF)
    body.append(int(cmd) & 0xFF)
    body.append(len(payload))
    body += payload
    crc = crc16_modbus(bytes(body))
    return SYNC_BYTES + bytes(body) + crc.to_bytes(2, CRC_ORDER)


# ------------------------------------------------------------------ 伪布局
@dataclass(frozen=True)
class LoraLayout:
    """LoRa 链路的伪布局：变长帧没有帧长/CRC 偏移等常数，统一给 0，
    让 layout_config / 界面用同一套字段名读取（同 TextLayout 做法）。"""

    key: str = LORA_KEY
    title: str = "LoRa 主从通用帧（AA55 / 变长）"
    note: str = ("AA 55｜目标2｜源2｜Seq1｜Cmd1｜Len1｜载荷N｜CRC16(MODBUS,小端)，"
                 "固定开销 11B，多字节小端；只读监听，主从双向帧均列出")
    frame_len: int = 0
    crc_offset: int = 0
    crc_covers: int = 0
    crc_order: str = CRC_ORDER
    byte_order: str = BYTE_ORDER


LORA_LAYOUT = LoraLayout()


# ------------------------------------------------------------------ 解析结果
def _direction(dst: int, src: int) -> str:
    """按地址判方向（监听视角）：广播 / 主机→从机 / 从机→主机。"""
    if dst == 0xFFFF:
        return "广播"
    if src == 0x0000:
        return "主机→从机"
    if dst == 0x0000:
        return "从机→主机"
    return "—"


def _payload_text(payload: bytes) -> str:
    if not payload:
        return "—"
    hex_full = payload.hex(" ").upper()
    if len(payload) <= PAYLOAD_SHOW:
        return hex_full
    shown = payload[:PAYLOAD_SHOW].hex(" ").upper()
    return f"{shown} …({len(payload)}B)"


@dataclass
class LoraFrame:
    """一帧已通过 CRC 校验的 LoRa 通用帧。"""

    layout: str = LORA_KEY
    ts: float = 0.0                       # 上位机接收时刻
    dst: int = 0
    src: int = 0
    seq: int = 0
    cmd: int = 0
    plen: int = 0
    payload_hex: str = ""
    crc: int = 0
    values: dict[str, Any] = field(default_factory=dict)
    cells: list[str] = field(default_factory=list)
    raw_hex: str = ""

    @property
    def direction(self) -> str:
        return _direction(self.dst, self.src)

    def to_payload(self) -> dict[str, Any]:
        """WebSocket 推送用的 JSON 友好结构（与 Frame.to_payload 对齐）。"""
        return {
            "ts": self.ts,
            "ts_text": _ts_text(self.ts),
            "dst": self.dst,
            "src": self.src,
            "seq": self.seq,
            "cmd": self.cmd,
            "cmd_name": cmd_name(self.cmd),
            "crc": f"{self.crc:04X}",
            "cells": self.cells,
            "values": self.values,
            "raw": self.raw_hex,
        }


# ------------------------------------------------------------------ 增量解析器
class LoraDecoder:
    """字节流 → 结构化 LoRa 帧。可被跨线程读取统计（内部加锁）。

    切帧策略（变长帧，与 protocol.Decoder 的定长策略对应）：
        1. 在缓冲里找同步头 AA 55，其前的字节计为丢弃并记一次重同步；
        2. 不足 11B 帧头则等待；够 11B 后按 Len 算整帧长（11+Len），不足再等；
        3. CRC 覆盖「目标地址→载荷末」比对：相等则出一帧、整帧消费；
           不等则记一次 CRC 错、滑过 1 字节重新找同步头（不会卡死）。
    """

    def __init__(self, keep: int = KEEP_FRAMES) -> None:
        self.layout = LORA_LAYOUT
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._frames_ok = 0
        self._crc_err = 0
        self._resync = 0
        self._bytes_in = 0
        self._bytes_dropped = 0
        self._last_ts: Optional[float] = None
        self._recent: deque[LoraFrame] = deque(maxlen=keep)
        self._rev = 0                      # 链路版本号：清空后自增，界面据此清表
        # 按地址聚合的节点状态：src/dst → {frames,last_ts,last_cmd,req,resp,report}
        self._nodes: dict[int, dict[str, Any]] = {}

    # -------------------------------------------------- 解析
    def feed(self, chunk: bytes, ts: Optional[float] = None) -> list[LoraFrame]:
        """喂入一段原始字节，返回本次新解析出的帧（可能为空）。"""
        if not chunk:
            return []
        now = time.time() if ts is None else ts
        out: list[LoraFrame] = []

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
                if len(buf) < 2 + HEADER_LEN:              # 帧头 2B + 头 9B 未收满
                    break                                  # 连帧头都没收满，等
                total = FIXED_OVERHEAD + buf[LEN_OFFSET]
                if len(buf) < total:
                    break                                  # 整帧未收满，等
                candidate = bytes(buf[:total])
                crc_rx = int.from_bytes(candidate[-2:], CRC_ORDER)
                body = candidate[CRC_START_OFFSET:-2]
                if crc_rx == crc16_modbus(body):
                    frame = self._build(candidate, now, crc_rx)
                    self._track(frame)
                    out.append(frame)
                    self._recent.append(frame)
                    self._frames_ok += 1
                    self._last_ts = now
                    del buf[:total]
                else:
                    # CRC 不符：这一字节不是帧起点，滑 1 字节继续找
                    self._crc_err += 1
                    self._resync += 1
                    self._bytes_dropped += 1
                    del buf[:1]
                if len(out) >= 64:                         # 单次别做太久
                    break
        return out

    def _build(self, raw: bytes, ts: float, crc: int) -> LoraFrame:
        dst = int.from_bytes(raw[2:4], BYTE_ORDER)
        src = int.from_bytes(raw[4:6], BYTE_ORDER)
        seq = raw[6]
        cmd = raw[7]
        plen = raw[LEN_OFFSET]
        # 载荷起点 = 帧头2 + 目标2 + 源2 + Seq1 + Cmd1 + Len1 = 9（固定开销 11 含 CRC2）
        payload = raw[9:9 + plen]
        frame = LoraFrame(
            ts=ts, dst=dst, src=src, seq=seq, cmd=cmd, plen=plen,
            payload_hex=payload.hex(" ").upper(), crc=crc,
            raw_hex=raw.hex(" ").upper(),
        )
        frame.values = {
            "DIR": frame.direction,
            "DST": dst,
            "SRC": src,
            "SEQ": seq,
            "CMD": cmd,
            "CMD_NAME": cmd_name(cmd),
            "LEN": plen,
            "PAYLOAD": _payload_text(payload),
        }
        frame.cells = [
            _ts_text(ts),
            frame.direction,
            f"{dst:04X}",
            f"{src:04X}",
            str(seq),
            cmd_name(cmd),
            str(plen),
            _payload_text(payload),
        ]
        return frame

    def _node(self, addr: int) -> dict[str, Any]:
        return self._nodes.setdefault(addr, {
            "src": addr, "frames": 0, "last_ts": 0.0, "last_cmd": 0,
            "req": 0, "resp": 0, "report": 0,
        })

    def _track(self, f: LoraFrame) -> None:
        """按地址聚合节点状态（监听侧口径）：

        · frames/last_ts/last_cmd：谁在发帧、最近一次是什么；
        · req：主机点对点请求，记到**被点名从机**头上（广播不计入，无应答语义）；
        · resp：该从机的 0x81~0x87 应答数；req/resp 相除即点对点响应率；
        · report：该从机的 0xA0/0xA1 主动上报数。
        """
        node = self._node(f.src)
        node["frames"] += 1
        node["last_ts"] = f.ts
        node["last_cmd"] = f.cmd
        if f.src == MASTER_ADDR and f.dst != BROADCAST_ADDR:
            self._node(f.dst)["req"] += 1
        if CMD_RESP_MIN <= f.cmd <= CMD_RESP_MAX:
            node["resp"] += 1
        if CMD_REPORT_MIN <= f.cmd <= CMD_REPORT_MAX:
            node["report"] += 1

    # -------------------------------------------------- 读取
    def columns(self) -> list[str]:
        """解码器口径的列名（界面上实际以方案字段表为准）。"""
        return ["接收时刻", "方向", "目标", "源", "Seq", "命令", "长度", "载荷"]

    def snapshot(self, recent: int = 0) -> dict[str, Any]:
        """解析统计（+ 可选最近帧），JSON 键名与 protocol.Decoder 对齐。"""
        with self._lock:
            data: dict[str, Any] = {
                "type": "protocol",
                "kind": "lora",
                "layout": LORA_KEY,
                "layout_title": self.layout.title,
                "layout_note": self.layout.note,
                "frame_len": 0,
                "crc_offset": 0,
                "covers": 0,
                "crc_order": CRC_ORDER,
                "byte_order": BYTE_ORDER,
                "rev": self._rev,
                "crc_label": "CRC-16/MODBUS（覆盖目标地址→载荷末）",
                "columns": self.columns(),
                "frames_ok": self._frames_ok,
                "crc_err": self._crc_err,
                "resync": self._resync,
                "bytes_in": self._bytes_in,
                "bytes_dropped": self._bytes_dropped,
                "last_ts": self._last_ts,
                "pending": len(self._buf),
            }
            now = time.time()
            # 节点聚合（在线判定与展示由界面做，这里只给原始计数与最后帧时刻）
            data["nodes"] = sorted(
                ({
                    "src": n["src"],
                    "frames": n["frames"],
                    "age_s": round(now - n["last_ts"], 1) if n["last_ts"] else None,
                    "last_cmd": n["last_cmd"],
                    "req": n["req"], "resp": n["resp"], "report": n["report"],
                } for n in self._nodes.values()),
                key=lambda x: x["src"])
            if recent > 0:
                data["recent"] = [f.to_payload() for f in list(self._recent)[-recent:]]
        return data

    def reset(self) -> None:
        """清空缓冲与统计（清屏用）。"""
        with self._lock:
            self._buf.clear()
            self._frames_ok = 0
            self._crc_err = 0
            self._resync = 0
            self._bytes_in = 0
            self._bytes_dropped = 0
            self._last_ts = None
            self._recent.clear()
            self._nodes.clear()
            self._rev += 1
