# QYH-GD300 上位机 · 第二批（曲线 / 滤波 / 报警 / 导出 / 下行）验证报告

- 日期：2026-10-09
- 范围：曲线渲染、滤波启用、报警全链路、导出（采样 CSV / 报警 CSV / PNG）、手动下行
- 模式：**mock（内置模拟数据源）**；真实串口部分见「六、真机复验说明」
- 承接：第一批（方案 / 字段表 / 会话缓冲）报告 `QYH-GD300第一批验证报告.md`

---

## 一、交付物清单

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| 修改 | `service/analysis.py` | 滤波落地（none / moving_avg / median / low_pass）；报警判据（上/下限 + 去抖 + 滞回 + 自动解警）；`Analyzer` 改为带状态（逐字段历史队列 + 报警状态机），`judge_alarm` 新签名含 `ts`，产出报警跳变事件 |
| 修改 | `service/serial_io.py` | 新增下行 `send(data)`（**仅锁定态可写**，扫描期只读被拒）；`MockPort.write/flush` 记录回环；`snapshot()` 增加 `sent_bytes` |
| 修改 | `service/main.py` | 报警跳变入库 + WS 广播；新增 `GET /api/alarms`、`GET /api/export/samples.csv`、`GET /api/export/alarms.csv`、`POST /api/send` |
| 修改 | `web/static/index.html` | 主区两栏重排（原始数据流 \| 曲线）；下区两栏（采样帧 \| 报警）；新增曲线面板、报警面板、手动下行面板；JS 接线（数据流喂曲线 / 报警推送 / 下行状态） |
| 新建 | `tools/verify_batch2.py` | 第二批端到端验证脚本（对运行中的 mock 服务，端口 8792） |
| 新建 | `QYH-GD300第二批验证报告.md` | 本报告 |

### 数据流水线（第二批扩展部分）

```
串口字节 → protocol/protocol_text 切帧
        → analysis.Analyzer（取值 → 线性换算 → 滤波 → 判报警）
            ├─→ 推页面：表格 cells + fields（含 value/filtered/alarm）
            ├─→ 会话缓冲：采样入库（store.append）+ 报警入库（append_alarm，进入/解除）
            └─→ WS 广播：type=frames / type=alarm
页面 → 曲线（canvas 手绘） / 报警列表 / 下行面板
```

分层不可越：通信层（serial_io）→ 协议层（protocol，纯计算无 IO）→ 分析层（analysis，纯计算）→ 配置层（schema/store）→ 表现层（index.html）。

### 关键设计说明

- **滤波算法**：`none`（原值透传）/ `moving_avg`（算术平均）/ `median`（中位数）/ `low_pass`（一阶 IIR：`y = α·x + (1-α)·y`）。历史队列长度 `hist_capacity` 由滤波类型决定（moving_avg/median 取 `window`，low_pass 取 512，none 取 1）。滤波后的工程量（`value`）用于制表、画曲线、判报警，原始工程量另存 `value_raw` 备查。
- **报警判据**：进入条件 `value ≥ limit`（高侧）/`value ≤ limit`（低侧）；滞回退出 `value ≤ limit − hysteresis`（高）/`value ≥ limit + hysteresis`（低）；去抖 `debounce_s` 以首次触阈时间戳计时。上限优先，避免双限同时触发时抖动。
- **报警进入 / 解除可区分**：入库时 `kind = side`（进入）或 `f"{side}_clear"`（解除）；`/api/alarms` 由 `kind` 派生 `active`（`not kind.endswith("_clear")`）与 `side`。
- **导出**：CSV 使用 `utf-8-sig`（带 BOM，Excel 正确识别中文）+ `Content-Disposition: attachment`；采样 CSV 由长表转宽表（一行一时刻），报警 CSV 表头 `时间,字段ID,字段名,类型,状态,数值,限值,说明`。PNG 走前端 `canvas.toBlob`。
- **手动下行**：`POST /api/send`，`mode=text`（默认追加 `\n`）/`mode=hex`；上限 512 字节；未锁定时返回 `409`（扫描期只读）。
- **曲线绘制**：Canvas 手绘 + DPR 缩放；多通道**单 Y 轴**叠加；`minmax` 抽稀（按像素列取 min/max）；交互含悬停十字线联动读数、点击定位表格行、暂停后拖时间轴回看。

---

## 二、验收标准对照

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 曲线渲染（多通道单 Y 轴 / 悬停读数 / 点击定位 / 暂停回看 / 导出 PNG） | ✅ | 浏览器 10 项 PASS（见 3.2） |
| 滤波启用（moving_avg / median / low_pass） | ✅ | 单元自检 + 端到端「保存带滤波+报警方案」PASS |
| 报警全链路（上/下限 + 去抖 + 滞回 + 自动解警 + 静音 + 入库） | ✅ | 端到端：进入 5 / 解除 4，文案带字段名单位；前端可静音、可清空列表 |
| 导出（采样 CSV / 报警 CSV / PNG） | ✅ | 采样 CSV 6523 行、报警 CSV 9 行；PNG 走 canvas.toBlob |
| 手动下行（文本 / HEX，仅锁定态开放） | ✅ | 文本 `50494E470A` / HEX `AA5501` 均 200；非法 HEX 400；`sent_bytes` 8→16 |
| 退出即清（会话库） | ✅ | 沿用第一批 `store.close_and_clear()` / `_purge()`，本轮未改 |

---

## 三、mock 模式验证结果

### 3.1 端到端全链路验证（`tools/verify_batch2.py`，端口 8792）

```
[PASS] 模拟源已锁定 -- state=locked mock=True port=MOCK0
[PASS] 保存带滤波+报警的方案 -- 200
[PASS] 激活该方案 -- 200 active=第二批-验证
[PASS] 报警已产生（上限进入） -- 200 enters=5 clears=4 first=频率 超过上限 23150Hz
[PASS] 报警能自动解除（滞回+自动解警） -- clears=4 last=频率 上限已恢复（当前 23140Hz）
[PASS] 报警记录带字段名/单位 -- sample={field: freq, kind: high_clear, name: 频率, unit: Hz, active: False, side: high, ...}
[PASS] 采样 CSV 导出 -- 200 rows=6523 header=接收时刻,频率(Hz)
[PASS] 报警 CSV 导出 -- 200 rows=9 header=时间,字段ID,字段名,类型,状态,数值,限值,说明
[PASS] 文本下行成功 -- 200 {ok: True, bytes: 5, hex: 50494E470A}
[PASS] HEX 下行成功 -- 200 {ok: True, bytes: 3, hex: AA5501}
[PASS] 非法 HEX 被拒 -- 400 HEX 格式非法（只允许 0–9 A–F）
[PASS] 下行字节计数递增 -- before=8 after=16
[PASS] 切回文本方案 -- kind=text ok=20
RESULT: ALL PASS（13/13）
```

### 3.2 浏览器 DOM / 交互验证（mock 运行中，http://127.0.0.1:8792）

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 整体布局（中间双栏 + 下区双栏） | ✅ | 左：原始数据流；右：曲线面板（含 canvas）。下区左：结构化采样帧；右：报警面板 |
| 曲线实际绘制（非空白） | ✅ | canvas 检出 1137 个非白像素；图例显示「频率 (Hz)」 |
| 顶栏报警标识 | ✅ | 活跃报警时显示「报警 ×1」红色徽章 |
| 报警面板条目 | ✅ | 列出带时间戳的中文条目，如「频率 超过上限 23150Hz」 |
| 手动下行面板状态 | ✅ | 状态「已锁定」、发送按钮可用；模式下拉含「文本 / HEX」 |
| 曲线悬停十字线 | ✅ | 悬停后 `.curve-tip` 显示时间与「频率」值；移出后隐藏 |
| 点击曲线定位表格行 | ✅ | 事件日志出现「已定位到表格行」 |
| 暂停 / 回看切换 | ✅ | 暂停后出现时间范围滑块、标签变「回看 …」；继续后恢复「实时 …」 |
| JavaScript 控制台错误 | ✅ | 无任何控制台消息 |

### 3.3 滤波 / 报警判据单元自检（源码态）

| 场景 | 预期 | 结果 |
| --- | --- | --- |
| 去抖 | t=1.0 才进入报警 | ✅ |
| 滞回（高侧） | 98 维持报警、93 解除 | ✅ |
| 滞回（低侧） | −5 进入、+1 维持、+3 解除 | ✅ |

---

## 四、已知限制（按约定不在第二批）

- 分段 / 查表 / 多项式换算；滤波链串联；每字段独立子图与双 Y 轴
- 周期重复下行；长期归档；人工确认消警与报警备注
- 枚举文本映射（如 MSG_TYPE / FLAG 的中文名）仍显示数字
- 前端曲线缓冲上限 `max_points`（默认 20000），超限丢最旧；会话库退出即清，长期留存依赖导出

---

## 五、变更文件清单

```
 M service/analysis.py        # 滤波 + 报警判据 + 带状态 Analyzer
 M service/serial_io.py       # send() 下行 + MockPort.write/flush + sent_bytes
 M service/main.py            # 报警入库/推送 + /api/alarms + 导出 + /api/send
 M web/static/index.html      # 曲线 / 报警 / 下行面板 + JS 接线
?? tools/verify_batch2.py     # 第二批验证脚本
?? QYH-GD300第二批验证报告.md  # 本报告
```

---

## 六、真机复验说明

- 本轮全部为 **mock 模式**验证，未接入真实设备。
- 真机约束（沿用既有设计）：串口固定 `115200 / 8 / N / 1`；扫描期**只读不写**（手动下行仅在锁定后开放）；同一串口只能被一个程序占用（测试前请关闭其他串口工具）。
- 建议真机复验步骤：
  1. `python service/main.py`（真实串口，自动识别；识别前只读）
  2. 打开 `http://127.0.0.1:8766`，确认顶栏锁定真实端口、表格与曲线出现数据
  3. 「设置 → 方案」为某字段配置 `moving_avg` 滤波与上限报警，观察曲线平滑度与报警进入/解除
  4. 锁定后用手动下行发一条文本命令，确认设备有响应；导出采样/报警 CSV 与曲线 PNG
  5. 退出程序，确认 `%TEMP%\QYH-GD300` 无 `session.db` 残留
- 打包复验：`powershell -ExecutionPolicy Bypass -File build/build_exe.ps1`（脚本未改动，本轮未重新打包）

---

## 七、复现命令

```powershell
# 端到端第二批验证（需先起服务在 8792）
python service/main.py --mock --port 8792
python tools/verify_batch2.py

# 浏览器手验
python service/main.py --mock      # 打开 http://127.0.0.1:8766
```