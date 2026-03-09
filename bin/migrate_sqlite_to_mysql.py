#!/usr/bin/env python3
"""
将现有 SQLite 数据迁移到 MySQL。
"""

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List

import click
import pymysql
from pymysql.cursors import DictCursor

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / 'src'))

from config import ConfigManager  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s'
)

TABLE_ORDER = [
    'domains',
    'certificates',
    'certificate_requests',
    'acme_rate_limits',
    'deployments',
]


def build_mysql_kwargs(db_config: Dict) -> Dict:
    """根据配置生成 MySQL 连接参数"""
    if db_config.get('engine') != 'mysql':
        raise click.ClickException("请先在 config.yaml 中将 database.engine 设置为 mysql")

    required = ['host', 'port', 'user', 'password', 'name']
    missing = [key for key in required if db_config.get(key) is None]
    if missing:
        raise click.ClickException(f"数据库配置缺少字段: {', '.join(missing)}")

    kwargs = {
        'host': db_config['host'],
        'port': int(db_config['port']),
        'user': db_config['user'],
        'password': db_config['password'],
        'database': db_config['name'],
        'charset': db_config.get('charset', 'utf8mb4'),
        'cursorclass': DictCursor,
        'autocommit': False,
        'connect_timeout': db_config.get('connect_timeout', 10),
    }

    if db_config.get('ssl'):
        kwargs['ssl'] = db_config['ssl']

    return kwargs


def table_exists(sqlite_conn: sqlite3.Connection, table: str) -> bool:
    cursor = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    )
    return cursor.fetchone() is not None


def fetch_rows(sqlite_conn: sqlite3.Connection, table: str) -> List[Dict]:
    cursor = sqlite_conn.execute(f"SELECT * FROM {table}")
    rows = cursor.fetchall()
    return [dict(row) for row in rows]


def chunk_rows(rows: List[Dict], size: int):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


@click.command()
@click.option('--config', '-c', default='config/config.yaml', show_default=True, help='配置文件路径')
@click.option('--sqlite-path', '-s', type=click.Path(), help='SQLite 数据库文件路径')
@click.option('--batch-size', '-b', default=500, show_default=True, help='每批次插入数量')
@click.option('--dry-run', is_flag=True, help='仅显示迁移计划，不写入 MySQL')
def main(config: str, sqlite_path: str, batch_size: int, dry_run: bool):
    """迁移 SQLite 数据到 MySQL。"""
    config_manager = ConfigManager(config)
    db_config = config_manager.get_database_config()

    mysql_kwargs = build_mysql_kwargs(db_config)

    if sqlite_path:
        source_path = os.path.abspath(os.path.expanduser(sqlite_path))
    else:
        # 默认使用项目内的 ./db/cert_manager.db
        source_path = os.path.abspath(str(PROJECT_ROOT / 'db' / 'cert_manager.db'))
    if not os.path.exists(source_path):
        raise click.ClickException(f"SQLite 文件不存在: {source_path}")

    logger.info("SQLite 源: %s", source_path)
    logger.info("MySQL 目标: %s:%s/%s", mysql_kwargs['host'], mysql_kwargs['port'], mysql_kwargs['database'])

    sqlite_conn = sqlite3.connect(source_path)
    sqlite_conn.row_factory = sqlite3.Row

    mysql_conn = None
    if not dry_run:
        mysql_conn = pymysql.connect(**mysql_kwargs)

    migrated_counts = {}

    try:
        for table in TABLE_ORDER:
            if not table_exists(sqlite_conn, table):
                logger.info("跳过表 %s (SQLite 不存在)", table)
                continue

            rows = fetch_rows(sqlite_conn, table)
            total = len(rows)
            if total == 0:
                logger.info("表 %s 无数据，跳过", table)
                migrated_counts[table] = 0
                continue

            logger.info("表 %s 读取 %s 行", table, total)
            migrated_counts[table] = total

            if dry_run:
                continue

            columns = list(rows[0].keys())
            placeholders = ','.join(['%s'] * len(columns))
            update_clause = ','.join(
                f"{col}=VALUES({col})" for col in columns if col != 'id'
            )

            insert_sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            if update_clause:
                insert_sql += f" ON DUPLICATE KEY UPDATE {update_clause}"

            with mysql_conn.cursor() as cursor:
                for batch in chunk_rows(rows, batch_size):
                    values = [[row[col] for col in columns] for row in batch]
                    cursor.executemany(insert_sql, values)
                mysql_conn.commit()

            logger.info("表 %s 迁移完成", table)

        logger.info("迁移完成: %s", migrated_counts)
        if dry_run:
            logger.info("Dry-run 模式，未对 MySQL 进行写入")

    except Exception as exc:
        if mysql_conn:
            mysql_conn.rollback()
        raise click.ClickException(f"迁移失败: {exc}") from exc
    finally:
        sqlite_conn.close()
        if mysql_conn:
            mysql_conn.close()


if __name__ == '__main__':
    main()

