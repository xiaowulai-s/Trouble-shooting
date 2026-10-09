# -*- coding: utf-8 -*-
"""QYH-GD300 故障定位系统 —— 会话数据缓冲（SQLite）

定位：
    本模块只负责"当前会话"的采样值/报警落盘与区间查询，供曲线与报警列表
    使用。按需求约定「仅当前会话、退出即清」——进程退出时数据库文件被删除，
    需要长期留存必须显式导出（第二批的导出功能）。

线程安全与性能：
    串口线程按帧追加数值（峰值可达约 3000 值/秒），直接逐条 INSERT 会拖慢
    解析；因此 append() 只把行放进内存缓冲，由后台线程按 flush_ms 批量提交
    事务。开 WAL + synchronous=NORMAL，兼顾吞吐与安全。

容错：
    数据库不可用时全部降级为"不落盘"，绝不抛异常影响串口解析与界面刷新。
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import tempfile
import threading
from typing import Any, Iterable, Optional

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS samples (
    ts    REAL  NOT NULL,
    field TEXT  NOT NULL,
    value REAL,
    text  TEXT
);
CREATE INDEX IF NOT EXISTS ix_samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS alarms (
    ts      REAL NOT NULL,
    field   TEXT NOT NULL,
    kind    TEXT,
    value   REAL,
    limit_v REAL,
    text    TEXT
);
CREATE INDEX IF NOT EXISTS ix_alarms_ts ON alarms (ts);
"""

MAX_BUFFER = 20000          # 缓冲上限：超过直接丢弃并计数，防止内存爆掉


def default_path() -> pathlib.Path:
    """会话数据库位置：系统临时目录下（退出即清，不需要便携存储）。"""
    return pathlib.Path(tempfile.gettempdir()) / "QYH-GD300" / "session.db"


def _unlink(path: str) -> None:
    """删除文件；不存在或被占用都不抛异常。"""
    try:
        target = pathlib.Path(path)
        if target.exists():
            target.unlink()
    except Exception:
        pass


def _purge(path: pathlib.Path) -> None:
    """清掉一个会话库的全部文件（含 WAL/SHM 边车）。"""
    for suffix in ("", "-wal", "-shm"):
        _unlink(str(path) + suffix)


class SampleStore:
    """会话缓冲：内存攒批 + 后台线程定时提交 + 退出清库。"""

    def __init__(self, path: Optional[os.PathLike | str] = None,
                 flush_ms: int = 500) -> None:
        self._path = pathlib.Path(path) if path else default_path()
        self._flush_ms = max(50, min(10000, int(flush_ms or 500)))
        self._lock = threading.Lock()
        self._buf: list[tuple] = []
        self._alarms: list[tuple] = []
        self._conn: Optional[sqlite3.Connection] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rows = 0
        self._dropped = 0
        self._ready = False

    # -------------------------------------------------- 生命周期
    def start(self) -> bool:
        """打开数据库并启动提交线程；失败则整体降级（返回 False）。"""
        if self._ready:
            return True
        _purge(self._path)                       # 先清掉上次的残留（进程被强杀时的兜底）
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        except Exception:
            return False
        self._conn = conn
        self._ready = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="gd300-store", daemon=True)
        self._thread.start()
        return True

    def close_and_clear(self) -> None:
        """停止线程、提交余量、关闭并删除数据库文件（退出即清）。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._flush()
        conn = self._conn
        self._conn = None
        self._ready = False
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        for suffix in ("", "-wal", "-shm"):
            _unlink(str(self._path) + suffix)
        with self._lock:
            self._buf = []
            self._alarms = []
            self._rows = 0

    # -------------------------------------------------- 写入
    def append(self, ts: float, items: Iterable[tuple[str, Any]]) -> None:
        """追加一帧的换算结果：items 为 (字段 id, 数值) 序列。"""
        if not self._ready:
            return
        rows: list[tuple] = []
        for field_id, value in items:
            if value is None or isinstance(value, bool):
                continue
            try:
                rows.append((float(ts), str(field_id), float(value), None))
            except (TypeError, ValueError):
                continue
        if not rows:
            return
        with self._lock:
            if len(self._buf) + len(rows) > MAX_BUFFER:
                self._dropped += len(rows)
                return
            self._buf.extend(rows)

    def append_alarm(self, ts: float, field_id: str, kind: str, value: Any,
                     limit_v: Any, text: str = "") -> None:
        """追加一条报警记录（第二批使用，第一批仅占位保证表结构可用）。"""
        if not self._ready:
            return
        try:
            row = (float(ts), str(field_id), str(kind), _num(value), _num(limit_v), str(text))
        except (TypeError, ValueError):
            return
        with self._lock:
            if len(self._alarms) > MAX_BUFFER:
                self._dropped += 1
                return
            self._alarms.append(row)

    def _loop(self) -> None:
        interval = self._flush_ms / 1000.0
        while not self._stop.wait(interval):
            self._flush()

    def _flush(self) -> None:
        with self._lock:
            rows, alarms = self._buf, self._alarms
            self._buf, self._alarms = [], []
        conn = self._conn
        if conn is None:
            return
        try:
            if rows:
                conn.executemany(
                    "INSERT INTO samples (ts, field, value, text) VALUES (?, ?, ?, ?)", rows)
            if alarms:
                conn.executemany(
                    "INSERT INTO alarms (ts, field, kind, value, limit_v, text) "
                    "VALUES (?, ?, ?, ?, ?, ?)", alarms)
            if rows or alarms:
                conn.commit()
            with self._lock:
                self._rows += len(rows)
        except Exception:
            with self._lock:
                self._dropped += len(rows) + len(alarms)

    # -------------------------------------------------- 读取
    def query_range(self, t0: Optional[float] = None, t1: Optional[float] = None,
                    field_ids: Optional[Iterable[str]] = None,
                    limit: int = 20000) -> list[dict[str, Any]]:
        """按时间区间（+ 可选字段）查询采样点，按时间升序。"""
        self._flush()
        conn = self._conn
        if conn is None:
            return []
        sql = "SELECT ts, field, value FROM samples"
        where: list[str] = []
        args: list[Any] = []
        if t0 is not None:
            where.append("ts >= ?")
            args.append(float(t0))
        if t1 is not None:
            where.append("ts <= ?")
            args.append(float(t1))
        ids = [str(f) for f in (field_ids or [])]
        if ids:
            where.append("field IN (" + ",".join("?" * len(ids)) + ")")
            args.extend(ids)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts ASC LIMIT ?"
        args.append(max(1, min(200000, int(limit))))
        try:
            cur = conn.execute(sql, args)
            return [{"ts": row[0], "field": row[1], "value": row[2]}
                    for row in cur.fetchall()]
        except Exception:
            return []

    def query_alarms(self, limit: int = 500) -> list[dict[str, Any]]:
        """最近若干条报警（第二批使用）。"""
        self._flush()
        conn = self._conn
        if conn is None:
            return []
        try:
            cur = conn.execute(
                "SELECT ts, field, kind, value, limit_v, text FROM alarms "
                "ORDER BY ts DESC LIMIT ?", (max(1, min(10000, int(limit))),))
            return [{"ts": r[0], "field": r[1], "kind": r[2], "value": r[3],
                     "limit": r[4], "text": r[5]} for r in cur.fetchall()]
        except Exception:
            return []

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ready": self._ready,
                "rows": self._rows + len(self._buf),
                "buffered": len(self._buf),
                "dropped": self._dropped,
                "path": str(self._path),
            }


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None