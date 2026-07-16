# 📈 Stock Quantitative Screener

一个基于 Python 的 **A股量化选股终端**，支持多策略筛选、技术指标分析、实时行情监控，通过终端交互界面快速筛选符合条件的股票。

> 数据源：历史K线使用 [baostock](http://baostock.com)，实时行情使用新浪财经API，本地 MySQL 缓存加速查询。

---

## ✨ 功能特性

- 🔍 **多维度筛选** — 支持换手率、振幅、涨跌幅、成交量、RSI、MACD、KDJ、布林带、均线趋势、ATR 等 20+ 技术指标组合筛选
- 📦 **9 套预设策略** — 覆盖动量突破、超跌反弹、趋势跟踪、量能驱动、短线博弈、稳健价值、突破追涨、高弹性反弹、高波动活跃等多种风格
- ⚡ **高性能引擎** — SQL 预筛选 + 批量加载 + 内存缓存三级加速，秒级筛选全市场 5000+ 股票
- 📊 **综合评分系统** — 根据信号权重自动计算每只股票的得分并排序
- 🗄️ **本地数据缓存** — MySQL 存储历史K线和技术指标，支持增量更新，断网也可离线选股
- 🎨 **美观终端界面** — 基于 [Rich](https://github.com/Textualize/rich) 构建的交互式 TUI，彩色表格 + 进度条
- 📋 **自选股管理** — 支持自选股列表的增删查改
- ⚙️ **灵活配置** — JSON 配置文件 + 数据库双写，支持手动编辑或终端内交互配置

---

## 🖥️ 界面预览

```
┌─────────────────────────────────────────────┐
│         📊 Stock Quantitative Screener       │
│  [f] 选股器    [c] 缓存管理    [q] 退出     │
└─────────────────────────────────────────────┘
```

选股结果表格实时展示：代码、名称、价格、涨跌幅、量比、换手率、振幅、RSI、KDJ-K、MACD、均线位置、综合评分等关键指标。

---

## 🚀 快速开始

### 环境要求

- **Python** 3.11+
- **MySQL** 5.7+ （用于本地数据缓存）
- **pipenv**（推荐）或 pip

### 1. 克隆仓库

```bash
git clone https://github.com/your-username/stock-screener.git
cd stock-screener
```

### 2. 创建数据库

```sql
CREATE DATABASE stock_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

### 3. 配置数据库连接

编辑 `db_config.py`，修改数据库连接信息：

```python
DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "your_password",   # ← 修改为你的密码
    "database": "stock_db",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}
```

### 4. 安装依赖

```bash
# 使用 pipenv（推荐）
pipenv install
pipenv shell

# 或使用 pip
pip install -r requirements.txt
```

### 5. 初始化数据

首次运行需要下载历史K线数据到本地 MySQL：

```bash
python main.py
```

在终端中按 `c` 进入缓存管理 → 选择"下载全量A股历史数据"，等待下载完成（约 5-10 分钟，取决于网速）。

### 6. 开始选股

数据下载完成后，按 `f` 进入选股器，选择预设策略或自定义筛选条件即可。

---

## 📂 项目结构

```
stock/
├── main.py                    # 主程序入口 & 终端交互界面
├── data_engine.py             # 数据引擎（历史K线 + 实时行情 + 技术指标计算）
├── db_manager.py              # 数据库管理（MySQL CRUD & 批量查询优化）
├── db_config.py               # 数据库连接配置
├── stock_picker_config.json   # 选股器配置（默认筛选条件）
├── requirements.txt           # Python 依赖
├── Pipfile                    # Pipenv 依赖管理
├── filters/                   # 预设筛选策略（JSON 文件）
│   ├── 01_动量突破型.json
│   ├── 02_超跌反弹型.json
│   ├── 03_趋势跟踪型.json
│   ├── 04_量能驱动型.json
│   ├── 05_短线博弈型.json
│   ├── 06_稳健价值型.json
│   ├── 07_突破追涨型.json
│   ├── 08_高弹性反弹型.json
│   └── 09_高波动活跃型.json
└── README.md
```

---

## 🎯 预设策略说明

| # | 策略名称 | 核心逻辑 | 适用场景 |
|---|---------|---------|---------|
| 01 | **动量突破型** | 放量突破 + RSI强势区(55-75) + 均线多头支撑 | 强势股追涨 |
| 02 | **超跌反弹型** | RSI超卖(<30) + KDJ低位金叉 + 布林下轨 | 抄底反弹 |
| 03 | **趋势跟踪型** | MA多头排列 + MACD金叉 + 价格站上MA20/MA60 | 趋势行情 |
| 04 | **量能驱动型** | 成交量2倍暴增 + 高换手 + RSI强势 | 主力异动 |
| 05 | **短线博弈型** | 超高换手(>10%) + 高振幅(>5%) + ATR高波动 | 短线快进快出 |
| 06 | **稳健价值型** | 低换手(<8%) + 低振幅(<5%) + 均线多头 + 高最低价比 | 稳健持仓 |
| 07 | **突破追涨型** | 布林上轨突破 + 放量 + MACD金叉共振 | 强势突破 |
| 08 | **高弹性反弹型** | 高ATR + 超跌RSI(25-45) + 布林下轨 + KDJ低位 | 高弹性抄底 |
| 09 | **高波动活跃型** | 120日均振幅>8% + 均换手>15% | 高波动短线 |

---

## 🔧 技术指标说明

| 指标 | 说明 |
|------|------|
| **RSI(14)** | 相对强弱指数，<30 超卖，>70 超买 |
| **MACD** | 异同移动平均线，金叉/死叉信号 |
| **KDJ** | 随机指标，K/D 值及交叉信号 |
| **布林带** | 上轨/中轨/下轨，价格位置判断 |
| **MA** | 移动平均线 (5/10/20/60日)，趋势判断 |
| **ATR(14)** | 平均真实波幅，衡量波动性 |
| **量比** | 当日成交量 / 5日均量 |
| **120日均振幅** | 长期平均振幅，筛选高弹性标的 |
| **最低价/最高价比** | 日内反弹强度指标 |

---

## ⚙️ 自定义筛选策略

你可以在 `filters/` 目录下创建新的 JSON 文件来定义自己的策略：

```json
{
  "name": "我的自定义策略",
  "filters": {
    "turnover":       { "min": 3.0, "max": 30.0, "enabled": true },
    "amplitude":      { "min": 2.0, "max": 15.0, "enabled": true },
    "pct_change":     { "min": 1.0, "max": 8.0, "enabled": true },
    "price_range":    { "min": 5,   "max": 300,  "enabled": true },
    "rsi":            { "min": 30,  "max": 70,   "enabled": true },
    "macd_golden_cross": { "enabled": true },
    "ma_trend":       { "type": "bullish", "enabled": true }
  },
  "signal_weights": {
    "macd_golden": 2.0,
    "volume": 1.0
  }
}
```

文件命名规则：`NN_策略名称.json`（NN 为两位数字序号），程序会自动按文件名排序加载。

---

## 🗄️ 数据库表结构

| 表名 | 用途 |
|------|------|
| `stock_history` | 历史K线 + 预计算技术指标（唯一键：code+date） |
| `watchlist` | 自选股列表 |
| `configs` | 配置持久化存储 |
| `portfolio` | 持仓/订单记录 |

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

本工具仅供学习和研究使用，**不构成任何投资建议**。股票投资有风险，入市需谨慎。使用者应自行承担所有投资决策带来的风险。

---

## 📄 License

MIT License
