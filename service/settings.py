# -*- coding: utf-8 -*-
"""QYH-GD300 故障定位系统 —— 本地配置（系统名称等界面设置）

存储策略（便携优先 + 自动回退）：
    1. 优先读写 exe（未打包时为项目根目录）下的 settings.json
       —— 绿色便携，整个目录拷走设置跟着走
    2. 该位置不可写时（例如装在 Program Files 下），自动回退到
       %LOCALAPPDATA%/QYH-GD300/settings.json

读写一律容错：文件缺失、损坏、内容非法、磁盘不可写，都只是退回默认值，
绝不影响上位机启动与运行。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading

DEFAULT_APP_NAME = "QYH-GD300 故障定位系统"
APP_VERSION = "v0.1"
MAX_APP_NAME = 32
FILE_NAME = "settings.json"
ACTIVE_SCHEME_KEY = "active_scheme"        # 当前激活的自定义方案名

_lock = threading.Lock()


def app_root() -> pathlib.Path:
    """打包后是 exe 所在目录，未打包时是项目根目录。"""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent.parent


def _candidates() -> list[pathlib.Path]:
    """按优先级列出配置文件位置：便携目录优先，%LOCALAPPDATA% 兜底。"""
    paths = [app_root() / FILE_NAME]
    base = os.environ.get("LOCALAPPDATA")
    if base:
        paths.append(pathlib.Path(base) / "QYH-GD300" / FILE_NAME)
    return paths


def normalize_app_name(raw: object) -> str:
    """校验并规范化名称。

    - 非文本、空值、纯空白 → ValueError
    - 折叠连续空白、去首尾空白
    - 超长按 MAX_APP_NAME 截断（前端另有 maxlength 限制）
    """
    if not isinstance(raw, str):
        raise ValueError("名称必须是文本")
    name = " ".join(raw.split())
    if not name:
        raise ValueError(f"名称不能为空（最多 {MAX_APP_NAME} 个字符）")
    return name[:MAX_APP_NAME]


def _load(path: pathlib.Path) -> dict | None:
    """读一个候选位置的 JSON；缺失/损坏/非法一律返回 None。"""
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def read_settings() -> dict:
    """读取整个配置字典（便携目录优先）；没有任何可用文件时返回 {}。"""
    with _lock:
        for path in _candidates():
            data = _load(path)
            if data is not None:
                return dict(data)
    return {}


def update_settings(patch: dict) -> dict:
    """把若干键合并进配置并落盘，返回合并后的完整配置。

    只改传进来的键，其余键原样保留（否则改系统名称会把帧格式配置抹掉）。
    所有候选位置都写不了时抛 OSError。
    """
    last_error: Exception | None = None
    with _lock:
        for path in _candidates():
            try:
                current = _load(path) or {}
                current.update(patch)
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(current, fh, ensure_ascii=False, indent=2)
                return dict(current)
            except Exception as e:          # 目录不可写等，回退到下一个位置
                last_error = e
    raise OSError(f"配置写入失败：{last_error}")


def read_app_name() -> str:
    """读取已保存的名称；文件缺失/损坏/非法时返回默认名称。"""
    with _lock:
        for path in _candidates():
            data = _load(path)
            if data is None:
                continue
            try:
                return normalize_app_name(data.get("app_name"))
            except ValueError:
                continue
    return DEFAULT_APP_NAME


def save_app_name(raw: object) -> str:
    """保存名称，返回实际写入的文本；所有位置都写不了时抛 OSError。"""
    name = normalize_app_name(raw)
    update_settings({"app_name": name})
    return name


def read_active_scheme(default: str = "") -> str:
    """读取当前激活方案名；缺失/非法时返回 default。"""
    value = read_settings().get(ACTIVE_SCHEME_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()[:MAX_APP_NAME]
    return default


def save_active_scheme(name: object) -> str:
    """合并写入当前激活方案名（不覆盖其余键）；非法名抛 ValueError。"""
    text = str(name or "").strip()
    if not text:
        raise ValueError("方案名不能为空")
    text = text[:MAX_APP_NAME]
    update_settings({ACTIVE_SCHEME_KEY: text})
    return text