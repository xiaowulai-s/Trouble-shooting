# -*- coding: utf-8 -*-
"""QYH-GD300 故障定位系统 —— 帧格式配置（settings.json ↔ protocol.Layout 的桥）

分层理由：
    protocol.py 是纯计算层，不做任何文件读写；settings.py 只管配置落盘、
    不认识协议结构。把"配置→布局"的翻译单独放这里，两边都不被污染。

配置形态（settings.json 里的 "layout" 键）：
    {
      "mode":   "text" | "25" | "14" | "custom",  # 用哪条链路
      "base":   "25" | "14",              # custom 模式下的字段表基准
      "custom": {"frame_len":25, "crc_offset":23, "covers":23,
                 "crc_order":"little", "byte_order":"big"}
    }

    "text"：设备固件直接输出 ASCII 文本行（当前固件形态，内置默认）；
    "25"/"14"：两套二进制定长帧（设计文档 3.3 / 评估报告 7.1）。

档 1+ 的边界：字段表（名字/偏移/长度）不可编辑，只有上面 5 项常数可调。

优先级：命令行 --proto 显式指定 > settings.json 配置 > 内置默认（text）。
    命令行属于"临时试一下"，不写配置文件。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from service.protocol import (DEFAULT_LAYOUT, LAYOUT_OVERRIDES, LAYOUTS, Layout,
                             build_layout)
from service.protocol_text import TEXT_KEY, TEXT_LAYOUT
from service.settings import read_settings, update_settings

SETTINGS_KEY = "layout"

MODE_TEXT = TEXT_KEY                                    # "text"：ASCII 文本行
MODE_CUSTOM = "custom"
MODE_PRESETS: tuple[str, ...] = tuple(LAYOUTS)          # ("25", "14")
MODES: tuple[str, ...] = (MODE_TEXT,) + MODE_PRESETS + (MODE_CUSTOM,)

# 内置默认：设备当前固件发出来的就是文本行，故默认走文本链路；
# 两套二进制定长帧仍保留在列表里，选中即可回归旧协议。
DEFAULT_MODE = MODE_TEXT
# 命令行 --proto 的合法取值（text 也允许显式指定，便于自检与临时试验）
PROTO_CHOICES: tuple[str, ...] = (MODE_TEXT,) + MODE_PRESETS

CUSTOM_DEFAULTS: dict[str, Any] = {
    "frame_len": DEFAULT_LAYOUT.frame_len,
    "crc_offset": DEFAULT_LAYOUT.crc_offset,
    "covers": DEFAULT_LAYOUT.crc_covers,
    "crc_order": DEFAULT_LAYOUT.crc_order,
    "byte_order": DEFAULT_LAYOUT.byte_order,
}


def default_config() -> dict[str, Any]:
    return {"mode": DEFAULT_MODE, "base": MODE_PRESETS[0],
            "custom": dict(CUSTOM_DEFAULTS)}


def _normalize_custom(raw: object) -> dict[str, Any]:
    """容错：缺失/非法项退回默认值（不抛异常，配置损坏也要能启动）。"""
    data = raw if isinstance(raw, dict) else {}
    out = dict(CUSTOM_DEFAULTS)
    for key in LAYOUT_OVERRIDES:
        if key not in data:
            continue
        value = data[key]
        if key in ("crc_order", "byte_order"):
            text = str(value).lower()
            if text in ("little", "big"):
                out[key] = text
        else:
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                pass
    return out


def normalize_config(raw: object) -> dict[str, Any]:
    """把任意输入规整成合法配置；非法一律回退默认，绝不抛异常。"""
    data = raw if isinstance(raw, dict) else {}
    mode = str(data.get("mode", "")).strip().lower()
    base = str(data.get("base", "")).strip().lower()
    cfg = {
        "mode": mode if mode in MODES else DEFAULT_MODE,
        "base": base if base in MODE_PRESETS else MODE_PRESETS[0],
        "custom": _normalize_custom(data.get("custom")),
    }
    # custom 模式下参数可能被改坏（如 CRC 越界）：校验不过就退回预设
    if cfg["mode"] == MODE_CUSTOM:
        try:
            layout_from_config(cfg)
        except ValueError:
            cfg["mode"] = MODE_PRESETS[0]
    return cfg


def read_config() -> dict[str, Any]:
    """读配置（含容错）；文件缺失/损坏时返回默认配置。"""
    return normalize_config(read_settings().get(SETTINGS_KEY))


def layout_from_config(cfg: dict[str, Any]) -> Layout:
    """按配置构造布局对象；text 返回文本伪布局，custom 常数非法时抛 ValueError。"""
    if cfg["mode"] == MODE_TEXT:
        return TEXT_LAYOUT
    if cfg["mode"] != MODE_CUSTOM:
        return LAYOUTS[cfg["mode"]]
    base = LAYOUTS[cfg["base"]]
    layout = build_layout(base, **cfg["custom"])
    return replace(
        layout,
        title=f"自定义 {layout.frame_len} 字节（基准 {cfg['base']}）",
        note=(f"手工调整：帧长 {layout.frame_len}，CRC 偏移 {layout.crc_offset}，"
              f"CRC 覆盖 0–{layout.crc_covers - 1}，"
              f"CRC {'低字节在前' if layout.crc_order == 'little' else '高字节在前'}，"
              f"字段{'大端' if layout.byte_order == 'big' else '小端'}"),
    )


def resolve_layout_choice(explicit: Optional[str] = None) -> tuple[Layout, dict[str, Any], str]:
    """决定本次运行用哪套布局。

    返回 (布局, 配置, 来源)；来源为 "cli"（命令行显式指定，不落盘）
    或 "config"（来自 settings.json）。
    """
    cfg = read_config()
    if explicit:
        key = str(explicit).strip().lower()
        if key == MODE_TEXT:
            return TEXT_LAYOUT, cfg, "cli"
        if key in MODE_PRESETS:
            return LAYOUTS[key], cfg, "cli"
    return layout_from_config(cfg), cfg, "config"


def save_config(payload: object) -> dict[str, Any]:
    """校验并保存配置（供 POST /api/settings 使用）；非法抛 ValueError。"""
    data = payload if isinstance(payload, dict) else {}
    mode = str(data.get("mode", "")).strip().lower()
    if mode not in MODES:
        raise ValueError(f"未知帧格式模式 {data.get('mode')!r}，可选：{'/'.join(MODES)}")
    base = str(data.get("base", "")).strip().lower()
    if data.get("base") is not None and base not in MODE_PRESETS:
        raise ValueError(f"未知字段表基准 {data.get('base')!r}，可选：{'/'.join(MODE_PRESETS)}")
    cfg = {
        "mode": mode,
        "base": base if base in MODE_PRESETS else MODE_PRESETS[0],
        "custom": _normalize_custom(data.get("custom")),
    }
    if mode == MODE_CUSTOM:
        layout_from_config(cfg)                 # 常数非法时在此抛 ValueError
    update_settings({SETTINGS_KEY: cfg})
    return cfg


def describe(layout: Layout, cfg: dict[str, Any], source: str) -> dict[str, Any]:
    """给界面的帧格式快照：可选项 + 当前生效值 + 来源。"""
    return {
        "mode": cfg["mode"],
        "base": cfg["base"],
        "custom": dict(cfg["custom"]),
        "modes": list(MODES),
        "kind": "text" if layout.key == MODE_TEXT else "binary",
        "presets": [{"key": key, "title": LAYOUTS[key].title} for key in MODE_PRESETS],
        "source": source,
        "effective": {
            "key": layout.key,
            "title": layout.title,
            "note": layout.note,
            "frame_len": layout.frame_len,
            "crc_offset": layout.crc_offset,
            "covers": layout.crc_covers,
            "crc_order": layout.crc_order,
            "byte_order": layout.byte_order,
        },
    }