# QYH-GD300 上位机 · 第一批（地基）验证报告

- 日期：2026-10-09
- 范围：自定义帧格式/帧内容 → 解析 → 工程量换算 的地基能力（不含曲线绘制/滤波启用/报警联动/导出/手动下行）
- 模式：**mock（内置模拟数据源）**；真实串口部分见「六、真机复验说明」

---

## 一、交付物清单

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| 新建 | `service/analysis.py` | 分析层（纯计算）：按字段表取值（键/偏移）+ 线性换算 `y=k·x+b` + 格式化；预留滤波/报警签名 |
| 新建 | `service/schema.py` | 方案层：方案数据类、3 个内置方案、读写/校验/迁移/激活、内置只读保护 |
| 新建 | `service/store.py` | 会话缓冲：SQLite（WAL）内存攒批 + 后台定时 flush；退出即清 + 强杀兜底 `_purge` |
| 修改 | `service/layout_config.py` | 追加 `cfg_from_scheme` / `layout_from_scheme`（方案 → 既有布局形态），原有 API 全保留 |
| 修改 | `service/settings.py` | 追加 `read_active_scheme` / `save_active_scheme`（合并写入） |
| 修改 | `service/main.py` | 串接分析层与存储；新增 `/api/fields`、`/api/store`、`/api/schemes*` |
| 修改 | `web/static/index.html` | 方案下拉、字段表编辑弹窗、上表格下曲线分屏骨架 |
| 新建 | `tools/verify_schemes.py` | 端到端方案验证脚本（对运行中的 mock 服务，端口 8791） |

### 数据流水线

```
串口字节 → protocol / protocol_text 切帧
        → analysis.Analyzer（按当前方案字段表取值 + 换算）
        → 推页面（表格 cells/fields）+ 写会话缓冲（store.SampleStore，退出即清）
```

分层不可越：通信层（serial_io）→ 协议层（protocol，纯计算无 IO）→ 分析层（analysis）→ 配置层（schema/layout_config/settings）→ 表现层（index.html）。

### 方案配置模型（统一 schema）

```json
{"scheme": {"name": "GD300-文本行", "builtin": false, "version": 1},
 "frame":  {"kind": "text", "mode": "text",
            "custom": {"frame_len": 25, "crc_offset": 23, "covers": 23,
                       "crc_order": "little", "byte_order": "big"}},
 "fields": [{"id": "f1", "source": {"type": "key", "key": "F"},
             "name": "频率", "unit": "Hz", "decimals": 0,
             "scale": {"k": 1.0, "b": 0.0},
             "filter": {"type": "none", "window": 5, "alpha": 0.2},
             "alarm": {"high": {"enabled": true, "limit": 24000, "hysteresis": 200, "debounce_s": 1.0},
                       "low":  {"enabled": false, "limit": 0, "hysteresis": 0, "debounce_s": 1.0}},
             "color": "#2E7CF6", "visible": {"table": true, "curve": true}}],
 "curve":   {"window_s": 60, "max_points": 20000, "decimate": "minmax"},
 "storage": {"mode": "session", "flush_ms": 500}}
```

字段「取值来源」两种形态：文本链路 `{"type":"key","key":"F"}`；二进制链路 `{"type":"offset","offset":6,"raw_type":"u16","byte_order":"big"}`。

---

## 二、验收标准对照

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 文本 + 二进制两链路都能按自定义字段表解析并换算 | ✅ | 自检 25B `cols=11` / text `cols=2`；端到端 `帧类型A` 改名生效 |
| 字段表可在界面增删改并即时生效（清表→rev++→重建表头） | ✅ | 浏览器：切 25B 方案后表头变 11 列；API：激活后 `columns[1]=帧类型A` |
| 方案可保存/切换/导出；SQLite 可查询；退出后库被清理 | ✅ | 方案 CRUD 全 PASS；`/api/store rows>0`；退出后 `%TEMP%\QYH-GD300` 为空 |
| `build_exe.ps1` 双链路自检仍 ALL PASS | ⚠️ 未打包验证 | 打包脚本未改；源码态双链路自检已 ALL PASS（见下） |

---

## 三、mock 模式验证结果

### 3.1 双链路自检（源码态）

```
python service/desktop_main.py --mock --self-check --proto 25
  [PASS] GET /api/status        -- state=searching mock=True
  [PASS] GET / (static UI)      -- HTTP 200, 38227 bytes
  [PASS] GET /api/settings      -- app_name=QYH-GD300 故障定位系统 max_len=32
  [PASS] GET /api/protocol      -- kind=binary layout=25 frames_ok=5 err=0 resync=0 cols=11
  [PASS] bind 127.0.0.1 only    -- not reachable from LAN, as expected
  [GD300] self-check: ALL PASS   exit=0

python service/desktop_main.py --mock --self-check --proto text
  [PASS] GET /api/protocol      -- kind=text layout=text frames_ok=5 err=0 resync=0 cols=2
  [GD300] self-check: ALL PASS   exit=0
```

### 3.2 端到端方案 API 验证（`tools/verify_schemes.py`，端口 8791）

```
[PASS] GET 内置25方案                -- 200 fields=10
[PASS] 内置方案只读保护              -- 400 「GD300-25 字节」是内置只读方案，请改名后另存
[PASS] 另存自定义方案                -- 200
[PASS] 激活自定义25方案              -- 200 active=自定义25-验证
[PASS] 二进制链路解析+字段表生效     -- kind=binary ok=20 err=0 col1=帧类型A
[PASS] 空字段表被拒                  -- 400 字段表不能为空（至少 1 个字段）
[PASS] CRC 越界被拒                  -- 400 CRC 偏移 99 越界（帧长 25）
[PASS] 会话缓冲落盘                  -- rows=259 buffered=30
[PASS] 切回文本方案                  -- kind=text ok=15 col=频率(Hz)
[PASS] 删除自定义方案                -- 200
[PASS] 不存在方案 404                -- 404
RESULT: ALL PASS（11/11）
```

### 3.3 退出即清（会话缓冲）

| 场景 | 行为 | 结果 |
| --- | --- | --- |
| 优雅退出（self-check 走 lifespan finally） | `close_and_clear()` 删 `session.db` / `-wal` / `-shm` | ✅ `%TEMP%\QYH-GD300` 无残留 |
| 强杀（uvicorn 未走 finally） | 下次 `SampleStore.start()` 先 `_purge()` 清残留 | ✅ 直接验证：启动前有残留 → start 后清干净 → close 后目录为空 |

### 3.4 浏览器 DOM 验证（mock 运行中，http://127.0.0.1:8766）

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 顶栏连接状态 + 模拟标识 | ✅ | 「模拟数据源 已锁定 MOCK0」 |
| 采样帧表格 | ✅ | 文本方案列头：`接收时刻 | 频率(Hz)` |
| 曲线面板占位 | ✅ | 「曲线绘制将在第二批实现：多通道叠加、单 Y 轴、回放联动、导出 PNG。…」 |
| 设置 → 方案下拉 3 个内置方案 | ✅ | GD300-文本行 / GD300-25 字节 / GD300-14 字节，含字段数标注 |
| 字段表编辑弹窗 | ✅ | 列头 13 列；数据行与说明行正常 |
| 切换方案列头随之变化 | ✅ | 切 25B → 11 列：`接收时刻 | 帧类型 | 节点地址 | 帧序号 | 节点时间戳 | 状态标志 | 峰值 | 平均 | 噪声基线 | 脉冲计数 | 电池(V)` |
| 切回文本方案 | ✅ | 列头恢复 `接收时刻 | 频率(Hz)`（已通过 API 复核 `kind=text cols=2`） |

---

## 四、内置方案口径说明

| 内置方案 | 链路 | 字段数 | 备注 |
| --- | --- | --- | --- |
| GD300-文本行 | text | 1 | 裸数字映射到 `F` 字段，列名 `频率(Hz)` |
| GD300-25 字节 | binary | 10 | 设计文档 3.3；`BATTERY` 特判 `k=0.1` / 单位 V / 1 位小数 |
| GD300-14 字节 | binary | 6 | 评估报告 7.1 |

内置方案只读，界面提供「另存为」后再改（`builtin:true` 保护）。

---

## 五、已知限制（按约定不在第一批）

- 分段/查表/多项式换算；滤波链串联；每字段独立子图与双 Y 轴；周期重复下行；长期归档；人工确认消警与报警备注
- 曲线面板仅占位，未绘制；`analysis.apply_filter` / `judge_alarm` 为占位（原值透传 / 恒未报警）
- `store.append_alarm` 已预留但未产生数据
- 枚举文本映射（如 MSG_TYPE / FLAG 的中文名）属「查表换算」，按约定不做，界面显示数字

---

## 六、真机复验说明

- 本轮全部为 **mock 模式**验证，未接入真实设备。
- 真机约束（沿用既有设计）：串口固定 `115200 / 8 / N / 1`；扫描期**只读不写**；同一串口只能被一个程序占用（测试前请关闭其他串口工具）。
- 建议真机复验步骤：
  1. `python service/main.py`（真实串口，自动识别；识别前只读）
  2. 打开 `http://127.0.0.1:8766`，确认顶栏锁定真实端口、表格出现数据
  3. 在「设置 → 方案」另存当前方案，改字段名/单位/换算系数，确认列名与数值随之变化
  4. 退出程序，确认 `%TEMP%\QYH-GD300` 无 `session.db` 残留
- 打包复验：`powershell -ExecutionPolicy Bypass -File build/build_exe.ps1`（脚本已含 `--proto 25` 与 `--proto text` 双链路自检，本轮未重新打包，脚本本身未改动）

---

## 七、复现命令

```powershell
# 双链路自检（源码态）
python service/desktop_main.py --mock --self-check --proto 25
python service/desktop_main.py --mock --self-check --proto text

# 端到端方案验证（需先起服务在 8791）
python service/main.py --mock --port 8791
python tools/verify_schemes.py

# 浏览器手验
python service/main.py --mock      # 打开 http://127.0.0.1:8766
```

---

## 八、第二批（待办，未开始）

曲线渲染（多通道叠加 + 回放联动 + 导出 PNG）、滤波启用、报警全链路（去抖/滞回/自动解警/静音/入库）、导出（CSV / 报警 CSV / PNG）、手动下行（文本 / HEX，仅锁定态开放，复用 ws_push 广播）。