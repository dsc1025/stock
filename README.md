# 📈 Stock Quantitative Screener

一个基于 Python 的 **A股量化选股终端**，支持放量回调策略筛选、技术指标分析、个股详情查看，通过终端交互界面快速筛选符合条件的股票。

> 数据源：历史K线使用 [baostock](http://baostock.com)，实时行情使用新浪财经API，本地 MySQL 缓存加速查询。

---

## ✨ 功能特性

- 🔍 **放量缩量回调策略** — 检测连续放量上涨后缩量回调的股票，捕捉洗盘休整后的介入机会
- 📊 **技术指标实时计算** — MA(5/10/20/60)、MACD、RSI(14)、KDJ(9,3,3)、布林带(20,2)、ATR(14)，查询时实时计算
- 📈 **交易信号生成** — 自动检测 MACD 金叉/死叉、RSI 超买/超卖、布林带突破、KDJ 低位金叉等
- ⚡ **高性能引擎** — SQL 预筛选 + 批量加载 + 内存计算三级加速，秒级筛选全市场 5000+ 股票
- 🗄️ **本地数据缓存** — MySQL 双表设计：`stock_code`（股票基本信息）+ `stock_detail`（日线原始数据）
- 🔄 **灵活的同步策略** — 支持初始化股票列表、补全缺失、增量更新、全量刷新四种模式
- 🎨 **美观终端界面** — 基于 [Rich](https://github.com/Textualize/rich) 构建的交互式 TUI

---

## 🏗️ 架构设计

```
main.py                        # 终端交互入口（菜单分发）
├── db_schema.py               # 数据库表定义
├── db_manager.py              # 辅助表 CRUD（portfolio / watchlist / configs）
├── repository/
│   └── stock_repo.py          # 数据访问层（批量查询、SQL 预筛选）
├── services/
│   ├── data_fetcher.py        # API 封装（baostock + 新浪，纯数据获取）
│   └── data_sync.py           # 数据同步编排（初始化/补全/增量/全量）
├── indicators.py              # 技术指标计算（纯函数，无 I/O）
├── stock_picker.py            # 选股引擎（纯本地数据库，无外部 API）
└── ui/
    └── display.py             # Rich 表格渲染 & 工具函数
```

### 设计原则

- **关注点分离**：数据获取、同步、指标计算、选股逻辑各自独立模块
- **指标不落库**：`stock_detail` 只存 baostock 原始 10 字段，MA/MACD/RSI/KDJ 等由 `indicators.py` 实时计算
- **纯本地选股**：`stock_picker.py` 不调用任何外部 API，所有数据来自本地 MySQL

---

## 🖥️ 界面预览

```
┌──────────────────────────────────────────────────┐
│           股票量化分析终端                         │
│  [f] 选股工具                                    │
│  [g] 个股详情                                    │
│  [c] 缓存管理 (初始化/补全/增量/全量)              │
│  [q] 退出                                        │
└──────────────────────────────────────────────────┘
```

选股结果表格：代码、名称、价格、涨幅、成交量、换手率、振幅、RSI、K值、MACD、均线位置、120日均振幅/换手率、综合评分。

---

## 🚀 快速开始

### 环境要求

- **Python** 3.11+
- **MySQL** 5.7+
- **pipenv**（推荐）

### 1. 创建数据库

```sql
CREATE DATABASE stock_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

### 2. 配置数据库连接

编辑 `db_config.py`：

```python
DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "your_password",
    "database": "stock_db",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}
```

### 3. 安装依赖

```bash
pipenv install
pipenv shell
```

### 4. 初始化数据

```bash
python main.py
```

在终端中按 `c` 进入缓存管理：

1. **按 `[1]` 初始化股票列表** — 从 baostock 拉取全量 A 股代码写入 `stock_code` 表
2. **按 `[2]` 补全缺失** — 下载所有股票的 500 天历史 K 线数据

### 5. 开始选股

数据下载完成后，按 `f` 进入选股器，输入回溯天数即可筛选。

---

## 📂 项目结构

```
stock/
├── main.py                    # 程序入口 & 终端菜单
├── db_config.py               # 数据库连接配置
├── db_schema.py               # 数据表定义（stock_code + stock_detail）
├── db_manager.py              # 辅助表管理（portfolio/watchlist/configs）
├── indicators.py              # 技术指标计算（MA/MACD/RSI/KDJ/布林/ATR）
├── stock_picker.py            # 选股引擎
├── repository/
│   └── stock_repo.py          # 数据访问层（CRUD & 批量查询）
├── services/
│   ├── data_fetcher.py        # baostock + 新浪 API 封装
│   └── data_sync.py           # 数据同步编排
├── ui/
│   └── display.py             # Rich 表格渲染
├── requirements.txt           # Python 依赖
├── Pipfile                    # Pipenv 依赖管理
└── README.md
```

---

## 🗄️ 数据库设计

### stock_code — 股票代码主表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INT PK | 自增主键 |
| code | VARCHAR(20) UNIQUE | baostock 格式 (sh.600519) |
| name | VARCHAR(50) | 股票名称 |
| ipo_date | DATE | 上市日期 |
| out_date | DATE | 退市日期 |
| type | VARCHAR(10) | 股票类型 (A股/B股等) |
| status | VARCHAR(10) | 交易状态 (正常上市/退市等) |

### stock_detail — 日线数据表 (FK → stock_code)

| 字段 | 类型 | 说明 |
|------|------|------|
| stock_id | INT FK | 关联 stock_code.id |
| date | DATE | 交易日 |
| open/high/low/close | DECIMAL(10,2) | OHLC（后复权） |
| preclose | DECIMAL(10,2) | 前收盘价 |
| volume | BIGINT | 成交量（股） |
| amount | DECIMAL(18,2) | 成交额（元） |
| turn | DECIMAL(8,4) | 换手率 (%) |
| pct_chg | DECIMAL(8,4) | 涨跌幅 (%) |

> **注意**：技术指标（MA、MACD、RSI、KDJ、布林带、ATR）**不存储在数据库中**，而是在查询时由 `indicators.py` 实时计算。

### 辅助表

| 表名 | 用途 |
|------|------|
| `watchlist` | 自选股列表 |
| `configs` | 配置持久化（JSON） |
| `portfolio` | 持仓/订单记录（JSON） |

---

## 🔧 缓存管理操作

| 操作 | 说明 |
|------|------|
| **[1] 初始化股票列表** | 从 baostock 拉取全量A股代码 → `stock_code` 表 |
| **[2] 补全缺失** | 为没有 K 线数据的股票下载 500 天历史 |
| **[3] 增量更新** | 仅下载每只股票上次缓存日期之后的新数据（日常使用） |
| **[4] 全量刷新** | 重新下载所有股票的 500 天数据（耗时，仅必要时使用） |

---

## 🎯 选股策略

当前内置策略：**放量后缩量回调**

- 回溯期内存在连续 5 日成交量 > 1.5×20日均量（放量阶段）
- 当前成交量 ≤ 5日均量（缩量回调）
- 配合换手率 0.5%~30%、振幅 0.5%~15%、股价 5~500 元的基础过滤

---

## 🔧 技术指标

| 指标 | 参数 | 说明 |
|------|------|------|
| **MA** | 5/10/20/60 | 简单移动平均线 |
| **MACD** | (12,26,9) | DIF/DEA/MACD 柱，金叉死叉信号 |
| **RSI** | 14 | Wilder 平滑，<30 超卖，>70 超买 |
| **KDJ** | (9,3,3) | K/D/J 值，低位金叉/高位死叉 |
| **布林带** | (20,2) | 上轨/中轨/下轨，突破信号 |
| **ATR** | 14 | 平均真实波幅，波动性衡量 |

---

## 📦 依赖

| 库 | 用途 |
|----|------|
| [baostock](http://baostock.com) | A股历史K线数据 |
| [pymysql](https://github.com/PyMySQL/PyMySQL) | MySQL 数据库驱动 |
| [rich](https://github.com/Textualize/rich) | 终端美化 & 交互组件 |
| [cryptography](https://cryptography.io) | MySQL 认证加密 |

---

## ⚠️ 免责声明

本工具仅供学习和研究使用，**不构成任何投资建议**。股票投资有风险，入市需谨慎。

---

## 📄 License

MIT License
