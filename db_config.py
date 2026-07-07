"""MySQL database configuration."""
import pymysql
from contextlib import contextmanager

DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "221127",
    "database": "stock_db",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}


def set_password(pwd: str):
    DB_CONFIG["password"] = pwd


@contextmanager
def get_connection():
    conn = pymysql.connect(**DB_CONFIG)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
