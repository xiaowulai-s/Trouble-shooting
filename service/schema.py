# -*- coding: utf-8 -*-
"""QYH-GD300 故障定位系统 —— 自定义方案（帧格式 + 字段表 + 曲线/存储策略）

方案（Scheme）是对"怎么解析 / 怎么换算 / 怎么展示"的一份完整描述：

    frame    上行链路与切帧常数：文本行（text）或二进制定长帧（binary）
    fields   自定义字段表：从帧里取哪个值 → 线性换算 → 单位 / 小数位 / 是否进表进曲线
    curve    曲线显示策略（窗口时长、最大点数、抽稀方式）——第二批使用
    storage  会话存储策略（仅当前会话，退出即清）——第一批已落地

分层（与 layout_config / analysis 的边界）：
    本模块只管"方案的读写 / 校验 / 迁移 / 内置保护"，不认识解析器与界面；
    落盘复用 settings.py 的"便携目录优先 + %LOCALAPPDATA% 回退 + 合并写入"，
    方案字典存在 settings.json 的 "schemes" 键下，当前激活方案名存在
    "active_scheme" 键下。

容错原则：
    读取路径一律不抛异常（配置损坏也要能启动，非法项退回默认）；
    仅"保存/激活"路径对非法输入抛 ValueError，由 API 层转成 400 提示。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Optional

from service.analysis import FILTER_TYPES, RAW_TYPES, DEFAULT_RAW_TYPE
from service.protocol import DEFAULT_LAYOUT, LAYOUTS
from service.protocol_text import DEFAULT_KEY, TEXT_KEY
from service.settings import (read_active_scheme, read_settings,
                             save_active_scheme, update_settings)

SCHEMES_KEY = "schemes"                 # settings.json 中存方案字典的键
LAYOUT_SETTINGS_KEY = "layout"          # 旧版帧格式配置键（仅用于推断默认方案）
SCHEME_VERSION = 1
MAX_SCHEME_NAME = 32
MAX_FIELDS = 64
MAX_FIELD_NAME = 24

BUILTIN_TEXT = "GD300-文本行"
BUILTIN_25 = "GD300-25 字节"
BUILTIN_14 = "GD300-14 字节"
DEFAULT_COLOR = "#2E7CF6"

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SIZES = {1: "u8", 2: "u16", 4: "u32"}
LAYOUTS_KEY_DEFAULT = next(iter(LAYOUTS))          # 二进制定长帧的默认基准 key（"25"）


# ------------------------------------------------------------------ 小工具
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


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    return fallback


def _order(value: Any, fallback: str = "big") -> str:
    text = str(value).strip().lower()
    return text if text in ("little", "big") else fallback


def _raw_type(value: Any) -> str:
    text = str(value).strip().lower()
    return text if text in RAW_TYPES else DEFAULT_RAW_TYPE


def _default_custom() -> dict[str, Any]:
    """与 protocol.DEFAULT_LAYOUT 对齐的自定义帧默认常数。"""
    return {
        "frame_len": DEFAULT_LAYOUT.frame_len,
        "crc_offset": DEFAULT_LAYOUT.crc_offset,
        "covers": DEFAULT_LAYOUT.crc_covers,
        "crc_order": DEFAULT_LAYOUT.crc_order,
        "byte_order": DEFAULT_LAYOUT.byte_order,
    }


def _default_filter() -> dict[str, Any]:
    return {"type": "none", "window": 5, "alpha": 0.2}


def _default_alarm_side() -> dict[str, Any]:
    return {"enabled": False, "limit": 0, "hysteresis": 0, "debounce_s": 1.0}


def _default_alarm() -> dict[str, Any]:
    return {"high": _default_alarm_side(), "low": _default_alarm_side()}


def _default_visible() -> dict[str, Any]:
    return {"table": True, "curve": True}


# ------------------------------------------------------------------ 数据结构
@dataclass
class FieldSpec:
    """一个自定义字段：取值来源 + 换算 + 展示 + 阈值（阈值第二批生效）。"""

    id: str
    source: dict[str, Any]
    name: str
    unit: str = ""
    decimals: int = 0
    scale: dict[str, Any] = dc_field(default_factory=dict)
    filter: dict[str, Any] = dc_field(default_factory=dict)
    alarm: dict[str, Any] = dc_field(default_factory=dict)
    color: str = DEFAULT_COLOR
    visible: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": dict(self.source),
            "name": self.name,
            "unit": self.unit,
            "decimals": self.decimals,
            "scale": dict(self.scale),
            "filter": dict(self.filter),
            "alarm": {k: dict(v) for k, v in self.alarm.items()},
            "color": self.color,
            "visible": dict(self.visible),
        }


@dataclass
class Scheme:
    """一套自定义方案。"""

    name: str
    builtin: bool = False
    version: int = SCHEME_VERSION
    frame: dict[str, Any] = dc_field(default_factory=dict)
    fields: list[FieldSpec] = dc_field(default_factory=list)
    curve: dict[str, Any] = dc_field(default_factory=dict)
    storage: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return scheme_to_dict(self)


# ------------------------------------------------------------------ 规整（容错）
def _normalize_alarm_side(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    return {
        "enabled": _as_bool(data.get("enabled"), False),
        "limit": _as_float(data.get("limit"), 0.0),
        "hysteresis": abs(_as_float(data.get("hysteresis"), 0.0)),
        "debounce_s": max(0.0, _as_float(data.get("debounce_s"), 1.0)),
    }


def normalize_field(raw: Any, index: int = 0) -> FieldSpec:
    """把任意输入规整成一个合法字段（不抛异常）。"""
    data = raw if isinstance(raw, dict) else {}
    fid = str(data.get("id", "")).strip() or f"f{index + 1}"

    src = data.get("source")
    src = src if isinstance(src, dict) else {}
    if str(src.get("type", "key")).lower() == "offset":
        source = {"type": "offset",
                  "offset": max(0, _as_int(src.get("offset"), 0)),
                  "raw_type": _raw_type(src.get("raw_type")),
                  "byte_order": _order(src.get("byte_order"))}
    else:
        source = {"type": "key", "key": str(src.get("key", "")).strip() or DEFAULT_KEY}

    scale = data.get("scale")
    scale = scale if isinstance(scale, dict) else {}
    filt = data.get("filter")
    filt = filt if isinstance(filt, dict) else {}
    ftype = str(filt.get("type", "none")).strip().lower()
    if ftype not in FILTER_TYPES:
        ftype = "none"
    alarm = data.get("alarm")
    alarm = alarm if isinstance(alarm, dict) else {}
    vis = data.get("visible")
    vis = vis if isinstance(vis, dict) else {}

    color = str(data.get("color", "")).strip()
    return FieldSpec(
        id=fid,
        source=source,
        name=str(data.get("name", "")).strip() or fid,
        unit=str(data.get("unit", "")).strip()[:16],
        decimals=max(0, min(6, _as_int(data.get("decimals"), 0))),
        scale={"k": _as_float(scale.get("k"), 1.0), "b": _as_float(scale.get("b"), 0.0)},
        filter={"type": ftype,
                "window": max(1, min(999, _as_int(filt.get("window"), 5))),
                "alpha": min(1.0, max(0.0, _as_float(filt.get("alpha"), 0.2)))},
        alarm={"high": _normalize_alarm_side(alarm.get("high")),
               "low": _normalize_alarm_side(alarm.get("low"))},
        color=color if _COLOR_RE.match(color) else DEFAULT_COLOR,
        visible={"table": _as_bool(vis.get("table"), True),
                 "curve": _as_bool(vis.get("curve"), True)},
    )


def _normalize_frame(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    kind = str(data.get("kind", "text")).strip().lower()
    if kind not in ("text", "binary"):
        kind = "text"
    custom_raw = data.get("custom")
    custom_raw = custom_raw if isinstance(custom_raw, dict) else {}
    custom = _default_custom()
    for key in ("frame_len", "crc_offset", "covers"):
        if key in custom_raw:
            custom[key] = _as_int(custom_raw[key], custom[key])
    custom["crc_order"] = _order(custom_raw.get("crc_order"), custom["crc_order"])
    custom["byte_order"] = _order(custom_raw.get("byte_order"), custom["byte_order"])

    base = str(data.get("base", "")).strip().lower()
    base = base if base in LAYOUTS else LAYOUTS_KEY_DEFAULT
    mode = str(data.get("mode", "")).strip().lower()
    if kind == "text":
        mode = TEXT_KEY
    elif mode not in (base, "custom"):
        mode = base
    return {"kind": kind, "mode": mode, "base": base, "custom": custom}


def normalize_scheme(raw: Any, name: Optional[str] = None) -> Scheme:
    """把任意输入规整成一个可用方案（不抛异常，用于读取路径）。"""
    data = raw if isinstance(raw, dict) else {}
    sname = str(name if name is not None else data.get("name", "")).strip()
    fields_raw = data.get("fields")
    fields_raw = fields_raw if isinstance(fields_raw, list) else []
    fields = [normalize_field(f, i) for i, f in enumerate(fields_raw)]

    curve = data.get("curve")
    curve = curve if isinstance(curve, dict) else {}
    decimate = str(curve.get("decimate", "minmax")).strip().lower()
    if decimate not in ("minmax", "none"):
        decimate = "minmax"

    storage = data.get("storage")
    storage = storage if isinstance(storage, dict) else {}
    return Scheme(
        name=sname,
        builtin=_as_bool(data.get("builtin"), False),
        version=max(1, _as_int(data.get("version"), SCHEME_VERSION)),
        frame=_normalize_frame(data.get("frame")),
        fields=fields,
        curve={"window_s": max(1, min(3600, _as_int(curve.get("window_s"), 60))),
               "max_points": max(100, min(500000, _as_int(curve.get("max_points"), 20000))),
               "decimate": decimate},
        storage={"mode": "session",
                 "flush_ms": max(50, min(10000, _as_int(storage.get("flush_ms"), 500)))},
    )


# ------------------------------------------------------------------ 校验（保存路径）
def validate(scheme: Scheme) -> None:
    """校验方案；非法抛 ValueError（由 API 层转 400）。"""
    if not scheme.name:
        raise ValueError("方案名不能为空")
    if len(scheme.name) > MAX_SCHEME_NAME:
        raise ValueError(f"方案名最多 {MAX_SCHEME_NAME} 个字符")
    if not scheme.fields:
        raise ValueError("字段表不能为空（至少 1 个字段）")
    if len(scheme.fields) > MAX_FIELDS:
        raise ValueError(f"字段数过多（最多 {MAX_FIELDS} 个）")

    seen: set[str] = set()
    for f in scheme.fields:
        if not _ID_RE.match(f.id):
            raise ValueError(f"字段 ID 非法：{f.id!r}（字母开头，仅字母/数字/下划线）")
        if f.id in seen:
            raise ValueError(f"字段 ID 重复：{f.id}")
        seen.add(f.id)
        if not f.name:
            raise ValueError(f"字段 {f.id} 缺少显示名")
        if len(f.name) > MAX_FIELD_NAME:
            raise ValueError(f"字段「{f.name}」显示名过长（最多 {MAX_FIELD_NAME} 字）")
        if len(f.unit) > 16:
            raise ValueError(f"字段「{f.name}」单位过长（最多 16 字）")
        if not (0 <= f.decimals <= 6):
            raise ValueError(f"字段「{f.name}」小数位需在 0–6")
        if f.source["type"] == "offset":
            if f.source["raw_type"] not in RAW_TYPES:
                raise ValueError(f"字段「{f.name}」原始类型非法：{f.source['raw_type']}")
            if f.source["offset"] > 8192:
                raise ValueError(f"字段「{f.name}」偏移过大")

    if scheme.frame["kind"] == "binary":
        custom = scheme.frame["custom"]
        if custom["frame_len"] < 4:
            raise ValueError("帧长至少 4 字节")
        if not (2 <= custom["crc_offset"] <= custom["frame_len"] - 2):
            raise ValueError(f"CRC 偏移 {custom['crc_offset']} 越界（帧长 {custom['frame_len']}）")
        if not (1 <= custom["covers"] <= custom["crc_offset"]):
            raise ValueError(f"CRC 覆盖长度 {custom['covers']} 非法（应 ≤ CRC 偏移）")


# ------------------------------------------------------------------ 内置方案
def _make_field(fid: str, source: dict[str, Any], name: str, unit: str = "",
                decimals: int = 0) -> FieldSpec:
    return FieldSpec(id=fid, source=source, name=name, unit=unit, decimals=decimals,
                     scale={"k": 1.0, "b": 0.0}, filter=_default_filter(),
                     alarm=_default_alarm(), color=DEFAULT_COLOR, visible=_default_visible())


def _builtin_text() -> Scheme:
    return Scheme(
        name=BUILTIN_TEXT, builtin=True,
        frame={"kind": "text", "mode": TEXT_KEY, "base": LAYOUTS_KEY_DEFAULT,
               "custom": _default_custom()},
        fields=[_make_field("freq", {"type": "key", "key": DEFAULT_KEY}, "频率", "Hz", 0)],
        curve={"window_s": 60, "max_points": 20000, "decimate": "minmax"},
        storage={"mode": "session", "flush_ms": 500},
    )


def _builtin_from_layout(layout, name: str) -> Scheme:
    fields: list[FieldSpec] = []
    for f in layout.fields:
        if not f.show:
            continue
        spec = _make_field(
            f.name.strip().lower(),
            {"type": "offset", "offset": f.offset,
             "raw_type": _SIZES.get(f.size, "u8"), "byte_order": layout.byte_order},
            f.label, "", 0)
        if f.name == "BATTERY":                     # 电池：0.1V/LSB（沿用原表格口径）
            spec.scale = {"k": 0.1, "b": 0.0}
            spec.unit = "V"
            spec.decimals = 1
        fields.append(spec)
    return Scheme(
        name=name, builtin=True,
        frame={"kind": "binary", "mode": layout.key, "base": layout.key,
               "custom": {"frame_len": layout.frame_len, "crc_offset": layout.crc_offset,
                          "covers": layout.crc_covers, "crc_order": layout.crc_order,
                          "byte_order": layout.byte_order}},
        fields=fields,
        curve={"window_s": 60, "max_points": 20000, "decimate": "minmax"},
        storage={"mode": "session", "flush_ms": 500},
    )


BUILTINS: dict[str, Scheme] = {
    BUILTIN_TEXT: _builtin_text(),
    BUILTIN_25: _builtin_from_layout(LAYOUTS[LAYOUTS_KEY_DEFAULT], BUILTIN_25),
    BUILTIN_14: _builtin_from_layout(
        LAYOUTS["14"] if "14" in LAYOUTS else LAYOUTS[LAYOUTS_KEY_DEFAULT], BUILTIN_14),
}

_BUILTIN_BY_LAYOUT = {TEXT_KEY: BUILTIN_TEXT, LAYOUTS_KEY_DEFAULT: BUILTIN_25, "14": BUILTIN_14}


def builtin_for_layout(layout_key: str) -> Scheme:
    """按链路 key（text/25/14）取对应的内置方案（未知 key 回退文本方案）。"""
    return copy.deepcopy(BUILTINS[_BUILTIN_BY_LAYOUT.get(str(layout_key), BUILTIN_TEXT)])


# ------------------------------------------------------------------ 目录读写
def _stored_schemes() -> dict[str, Any]:
    data = read_settings().get(SCHEMES_KEY)
    return data if isinstance(data, dict) else {}


def summary(scheme: Scheme) -> dict[str, Any]:
    return {"name": scheme.name, "builtin": scheme.builtin,
            "kind": scheme.frame.get("kind", "text"), "mode": scheme.frame.get("mode", ""),
            "fields": len(scheme.fields), "version": scheme.version}


def list_schemes() -> list[dict[str, Any]]:
    """全部方案摘要：内置在前，自定义在后。"""
    items = [summary(s) for s in BUILTINS.values()]
    for name, raw in _stored_schemes().items():
        if name in BUILTINS:
            continue                                    # 同名不覆盖内置
        items.append(summary(normalize_scheme(raw, name)))
    return items


def load_scheme(name: str) -> Scheme:
    """读取一个方案（内置或已保存）；不存在抛 KeyError。"""
    key = str(name or "").strip()
    if key in BUILTINS:
        return copy.deepcopy(BUILTINS[key])
    raw = _stored_schemes().get(key)
    if raw is None:
        raise KeyError(key)
    scheme = normalize_scheme(raw, key)
    scheme.builtin = False
    return scheme


def load_active_scheme() -> Scheme:
    """读取当前激活方案；缺失/非法时按旧帧格式配置挑一个内置方案（保证有可用方案）。"""
    name = read_active_scheme()
    if name:
        try:
            return load_scheme(name)
        except KeyError:
            pass
    return _default_for_settings()


def _default_for_settings() -> Scheme:
    """没有激活方案时，依据 settings.json 里旧的 "layout" 匹配合适的内置方案。"""
    raw = read_settings().get(LAYOUT_SETTINGS_KEY)
    data = raw if isinstance(raw, dict) else {}
    mode = str(data.get("mode", "")).strip().lower()
    if mode == "14":
        return copy.deepcopy(BUILTINS[BUILTIN_14])
    if mode in (LAYOUTS_KEY_DEFAULT, "custom"):
        scheme = copy.deepcopy(BUILTINS[BUILTIN_25])
        if mode == "custom":
            custom = data.get("custom")
            frame = {"kind": "binary", "mode": "custom",
                     "base": str(data.get("base", "")).strip().lower(), "custom": custom}
            scheme.frame = _normalize_frame(frame)
        return scheme
    return copy.deepcopy(BUILTINS[BUILTIN_TEXT])


def save_scheme(raw: Any) -> Scheme:
    """校验并保存一个自定义方案；内置方案只读（抛 ValueError）。"""
    scheme = normalize_scheme(raw)
    scheme.builtin = False
    validate(scheme)
    if scheme.name in BUILTINS:
        raise ValueError(f"「{scheme.name}」是内置只读方案，请改名后另存")
    stored = dict(_stored_schemes())
    stored[scheme.name] = scheme_to_dict(scheme)
    update_settings({SCHEMES_KEY: stored})
    return scheme


def delete_scheme(name: str) -> None:
    """删除自定义方案；内置不可删（抛 ValueError），不存在抛 KeyError。"""
    key = str(name or "").strip()
    if key in BUILTINS:
        raise ValueError("内置方案不可删除")
    stored = dict(_stored_schemes())
    if key not in stored:
        raise KeyError(key)
    del stored[key]
    update_settings({SCHEMES_KEY: stored})


def activate_scheme(name: str) -> Scheme:
    """把某方案设为当前激活方案（须已存在）；返回该方案。"""
    scheme = load_scheme(name)                      # 不存在在此抛 KeyError
    save_active_scheme(scheme.name)
    return scheme


def scheme_to_dict(scheme: Scheme) -> dict[str, Any]:
    """方案 → JSON 友好字典。"""
    return {
        "name": scheme.name,
        "builtin": scheme.builtin,
        "version": scheme.version,
        "frame": copy.deepcopy(scheme.frame),
        "fields": [f.to_dict() for f in scheme.fields],
        "curve": dict(scheme.curve),
        "storage": dict(scheme.storage),
    }


def fields_view(scheme: Scheme) -> list[dict[str, Any]]:
    """字段表快照（供 /api/fields 与界面字段表编辑器使用）。"""
    return [f.to_dict() for f in scheme.fields]