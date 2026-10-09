# QYH-GD300 故障定位系统 · 上位机（v0.1）

面向 QYH-GD300 故障定位设备的本地串口上位机：自动识别接收端、解析自定义帧格式、工程量换算，
提供实时表格 / 曲线 / 报警 / 数据导出 / 手动下行，并可打包为**单文件免安装 exe**。

- 通信：串口固定 `115200 / 8 / N / 1`，扫描期**只读**，识别到接收端后锁定
- 链路：二进制定长帧 与 ASCII 文本行 两条解析链路，接口一致、上层不必区分
- 服务：FastAPI + uvicorn，**只绑 `127.0.0.1`**，浏览器访问 `http://127.0.0.1:8766`
- 桌面：pywebview（WebView2）原生窗口，前端为单文件原生 JS

---

## 功能特性

- **串口自动识别**：USB 转串口优先枚举，按「连续有数据 + 速率稳定 + 重复帧头」判据锁定接收端（约 10 s）。
- **双链路解析**
  - 二进制链路：设计文档 3.3 定长帧（默认 25 字节，CRC-16/MODBUS 覆盖 0–22，CRC 小端）；
  - 文本链路：一行一帧，支持裸数值（自动归 `F` 字段）与 `KEY=VALUE` / 多字段，未知字段自动成列。
- **自定义帧格式**：25B / 14B 预设 + 自定义（帧长、CRC 偏移、覆盖字节、CRC 与多字节字段字节序）；
  运行中切换**即时生效**（清统计、`rev` 自增、重建字段表、记事件），无需重启。
- **方案（Schema）**：可视化字段表，字段可按「键名」或「字节偏移」取值，支持线性换算 `y = k·x + b`、单位与小数位；
  内置方案只读，自定义方案可增删改并可激活。
- **滤波**：`none` / `moving_avg`（滑动平均）/ `median`（中位数）/ `low_pass`（一阶 IIR）。
- **报警**：上限 / 下限，含去抖（`debounce_s`）与滞回（`hysteresis`）及自动解警；
  进入 / 解除均入库并实时推送（`kind` 区分 `high` 与 `high_clear`）。
- **展示**：原始字节流、结构化采样表格（列随方案变化）、多通道单 Y 轴曲线（悬停读数 / 点击定位表格行 / 暂停回看）。
- **导出**：采样 CSV（长表转宽表）、报警 CSV、曲线 PNG（前端 `canvas.toBlob`）；CSV 带 BOM。
- **手动下行**：文本 / HEX 两种模式，**仅锁定后开放**（扫描期一律拒绝）。
- **会话缓冲**：SQLite（WAL）内存攒批 + 后台定时 flush，**退出即清**（长期留存依赖导出）。
- **可改系统名称**：运行时同步界面标题与原生窗口标题栏，上限 32 字符。

---

## 架构与数据流水线

分层（不可越层）：通信层 → 协议层 → 分析层 → 配置层 → 表现层。
协议层与分析层均为**纯计算、无 IO**，便于单测与复用。

```
串口字节 ──serial_io──▶ 切帧 ──protocol / protocol_text──▶ 分析 ──analysis──▶
            取值 (键/偏移) → 线性换算 → 滤波 → 判报警
                 ├─▶ 页面：表格 + 曲线      （WebSocket  type=frames）
                 ├─▶ 会话缓冲：采样入库 + 报警入库（store，SQLite/WAL）
                 └─▶ 报警推送               （WebSocket  type=alarm）
```

| 分层 | 模块 | 职责 |
| --- | --- | --- |
| 通信层 | `service/serial_io.py` | 扫描/锁定串口、只读接收、下行 `send()`（仅锁定态） |
| 协议层 | `service/protocol.py` | 二进制定长帧切帧、CRC 校验、字段表 |
| 协议层 | `service/protocol_text.py` | ASCII 文本行切帧（裸数值 / KEY=VALUE） |
| 分析层 | `service/analysis.py` | 取值、线性换算、滤波、报警判据（带状态 `Analyzer`） |
| 配置层 | `service/schema.py` | 方案数据类、内置方案、读写/校验/激活 |
| 配置层 | `service/layout_config.py` | 帧格式配置（预设/自定义）↔ 布局形态 |
| 配置层 | `service/settings.py` | 系统名称、激活方案等本地配置（便携优先） |
| 存储层 | `service/store.py` | 会话缓冲（SQLite WAL），退出即清 |
| 推送层 | `service/ws_push.py` | 线程安全 WebSocket 广播（`call_soon_threadsafe`） |
| 表现层 | `web/static/index.html` | 单文件前端（表格 / 曲线 / 报警 / 下行 / 导出） |

---

## 目录结构

```
service/          内核：FastAPI 本地服务 + 桌面壳 + 各分层模块
  main.py           本地服务入口（真实串口 / --mock），组装数据流水线
  desktop_main.py   桌面壳入口（pywebview / WebView2），单 exe 启动
  serial_io.py      通信层：串口自动识别、锁定、只读接收、手动下行
  protocol.py       协议层：二进制定长帧（25B / 14B / 自定义）
  protocol_text.py  协议层：ASCII 文本行
  analysis.py       分析层：取值 / 线性换算 / 滤波 / 报警
  schema.py         配置层：方案（字段表）与内置方案
  layout_config.py  配置层：帧格式配置
  settings.py       配置层：系统名称 / 激活方案
  store.py          存储层：会话缓冲（SQLite WAL，退出即清）
  ws_push.py        推送层：WebSocket 线程安全广播
web/static/       前端：单文件原生 JS（index.html）
tools/            验证 / 调试脚本
  verify_schemes.py 第一批端到端验证（mock，端口 8791）
  verify_batch2.py  第二批端到端验证（mock，端口 8792）
  probe_serial.py   串口探测
  capture_serial.py 串口抓包
build/            构建：build_exe.ps1（一键打包 + 双链路自检）、gd300.spec、dist/
docs/             设计与验证文档（见「文档索引」）
  assets/           截图
```

> 运行时/构建产物不入库：`build/pyinstaller-work/`、`build/dist/*.exe`、`build/edge-profile/`、
> `__pycache__/`、`settings.json`（每台机器不同）等已在 `.gitignore` 中忽略。

---

## 快速开始

### 依赖

- Python 3.11+（本项目实测 3.14）
- 运行库：`fastapi`、`uvicorn`、`pyserial`、`websockets`、`pywebview`（桌面壳）
- 桌面端运行前提：系统装有 Microsoft Edge WebView2 运行时（Win10/11 自带）

```powershell
pip install fastapi uvicorn pyserial websockets pywebview
```

### 运行（开发）

```powershell
python service/main.py                          # 真实串口：自动识别接收端（只读）
python service/main.py --mock                   # 无硬件自测：内置模拟数据源
python service/main.py --mock --port 8766       # 指定端口
```

浏览器打开 `http://127.0.0.1:8766`。桌面窗口方式：

```powershell
python service/desktop_main.py                  # 弹出原生窗口（WebView2）
python service/desktop_main.py --mock           # 模拟数据
python service/desktop_main.py --self-check     # 只起服务不开窗，自检后退出
```

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--mock` | 启用内置模拟数据源（无硬件自测） |
| `--port N` | 监听端口（默认 `8766`） |
| `--proto 25\|text` | 临时覆盖帧格式，仅本次运行、不写配置 |
| `--self-check` | 只起服务不开窗，跑自检后退出（用于验证 exe） |

### 打包 exe

```powershell
powershell -ExecutionPolicy Bypass -File build/build_exe.ps1
```

产物：`build/dist/QYH-GD300上位机.exe`（单文件、免安装、双击即弹出原生窗口）。
脚本内含 `--proto 25` 与 `--proto text` 双链路自检，任一失败即中止。

---

## HTTP / WebSocket 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | 通信状态快照（`state` / `port` / `scan` / `rate` / `mock` / `sent_bytes`） |
| GET | `/api/protocol` | 协议解析统计 + 最近结构化帧 + 表格列定义 |
| GET | `/api/fields` | 当前方案字段表快照（编辑器与曲线图例用） |
| GET | `/api/store` | 会话缓冲查询（`t0` / `t1` / `fields` / `limit`） |
| GET | `/api/alarms` | 当前会话报警记录（含进入与解除） |
| GET | `/api/export/samples.csv` | 采样 CSV 导出（长表转宽表，带 BOM） |
| GET | `/api/export/alarms.csv` | 报警 CSV 导出 |
| POST | `/api/send` | 手动下行（`mode=text\|hex`；未锁定返回 `409`） |
| GET | `/api/schemes` | 方案列表 |
| GET | `/api/schemes/{name}` | 方案详情 |
| POST | `/api/schemes` | 保存 / 更新方案 |
| DELETE | `/api/schemes/{name}` | 删除方案（内置方案只读） |
| POST | `/api/schemes/{name}/activate` | 激活方案 |
| GET | `/api/settings` | 读取系统名称 / 帧格式 / 版本 |
| POST | `/api/settings` | 保存系统名称 / 帧格式（运行中即时重切布局） |
| WS | `/ws` | 实时推送：`frames` / `alarm` / `layout` / `status` / `event` / `send` |
| GET | `/` | 静态前端 |

---

## 硬约束（安全）

- 串口固定 `115200 / 8 / N / 1`，无流控。
- **扫描 / 识别期只读**：不向任何串口写数据；手动下行仅在「已锁定」后开放。
- **串口独占**：同一串口同一时刻只能被一个程序占用（测试前请关闭其它串口工具）。
- 本地服务**只绑 `127.0.0.1`**，不对局域网开放。
- 会话缓冲**退出即清**（`%TEMP%\QYH-GD300` 不残留 `session.db`）。
- 应用名称上限 32 字符，拒绝空 / 纯空白，支持恢复默认。

---

## 文档索引（`docs/`）

| 文档 | 内容 |
| --- | --- |
| `QYH-GD300上位机方案评估报告.md` | 需求背景与方案评估 |
| `QYH-GD300上位机软件设计文档.docx` | 软件设计文档（分层、帧格式、接口） |
| `AI开发上位机_UI提示词规范包.md` | UI 提示词规范 |
| `QYH-GD300第一批验证报告.md` | 第一批（地基：方案/字段表/换算/会话缓冲）验证 |
| `QYH-GD300第二批验证报告.md` | 第二批（曲线/滤波/报警/导出/下行）验证 |
| `QYH-GD300真机复验与打包验证报告.md` | 真机复验 + exe 重打包验证 |
| `项目记忆.md` | 项目记忆：约束 / 约定 / 教训 / 决策 |

---

## 复现命令

```powershell
# 第一批端到端验证（需先在其上起 mock 服务在 8791）
python service/main.py --mock --port 8791
python tools/verify_schemes.py

# 第二批端到端验证（mock 服务在 8792）
python service/main.py --mock --port 8792
python tools/verify_batch2.py

# 打包 + 双链路自检
powershell -ExecutionPolicy Bypass -File build/build_exe.ps1
```