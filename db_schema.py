"""Database schema: stock_code + stock_detail tables with FK relationship.

Replaces the old monolithic stock_history table.
All columns map directly to baostock API responses — no computed fields.
"""
from db_config import get_connection


# ── stock_code: 股票代码主表 (from baostock query_all_stock + query_stock_basic) ──
_CREATE_STOCK_CODE = """
CREATE TABLE IF NOT EXISTS stock_code (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    code       VARCHAR(20)  NOT NULL UNIQUE COMMENT 'baostock格式: sh.600519',
    name       VARCHAR(50)  NOT NULL DEFAULT ''  COMMENT '股票名称',
    ipo_date   DATE         DEFAULT NULL         COMMENT '上市日期',
    out_date   DATE         DEFAULT NULL         COMMENT '退市日期',
    type       VARCHAR(10)  DEFAULT ''           COMMENT '股票类型',
    status     VARCHAR(10)  DEFAULT ''           COMMENT '交易状态',
    created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ── stock_detail: 日线数据表, FK → stock_code.id (baostock K线原始10字段) ──
_CREATE_STOCK_DETAIL = """
CREATE TABLE IF NOT EXISTS stock_detail (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    stock_id   INT          NOT NULL COMMENT 'FK → stock_code.id',
    date       DATE         NOT NULL,
    open       DECIMAL(10,2)   COMMENT '开盘价',
    high       DECIMAL(10,2)   COMMENT '最高价',
    low        DECIMAL(10,2)   COMMENT '最低价',
    close      DECIMAL(10,2)   COMMENT '收盘价(后复权)',
    preclose   DECIMAL(10,2)   COMMENT '前收盘价',
    volume     BIGINT          COMMENT '成交量(股)',
    amount     DECIMAL(18,2)   COMMENT '成交额(元)',
    turn       DECIMAL(8,4)    COMMENT '换手率(%)',
    pct_chg    DECIMAL(8,4)    COMMENT '涨跌幅(%)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_stock_date (stock_id, date),
    INDEX idx_stock_id (stock_id),
    INDEX idx_date (date),
    CONSTRAINT fk_stock FOREIGN KEY (stock_id) REFERENCES stock_code(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ── Keep old utility tables unchanged ──
_CREATE_UTILITY_TABLES = """
CREATE TABLE IF NOT EXISTS portfolio (
    id INT AUTO_INCREMENT PRIMARY KEY,
    type ENUM('config','position','order') NOT NULL,
    code VARCHAR(20),
    data JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_type (type),
    INDEX idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS watchlist (
    id INT AUTO_INCREMENT PRIMARY KEY,
    code VARCHAR(20) NOT NULL,
    name VARCHAR(50),
    added_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS configs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    config_key VARCHAR(50) NOT NULL,
    config_value JSON NOT NULL,
    description VARCHAR(200),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_key (config_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def init_tables():
    """Create all tables if they don't exist."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_STOCK_CODE)
            cur.execute(_CREATE_STOCK_DETAIL)
            for stmt in _CREATE_UTILITY_TABLES.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)


def drop_old_tables():
    """Drop the legacy stock_history table if it exists."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("DROP TABLE IF EXISTS stock_history")
            except Exception:
                pass
