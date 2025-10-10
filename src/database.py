#!/usr/bin/env python3
"""
数据库模块 - 管理证书、域名和服务器配置
"""

import logging
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class DatabaseManager:
    """数据库管理器"""

    def __init__(self, db_path: str):
        """初始化数据库管理器
        
        Args:
            db_path: 数据库文件路径
        """
        self.db_path = db_path
        self.ensure_db_directory()
        self.init_database()

    def ensure_db_directory(self):
        """确保数据库目录存在"""
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, mode=0o750, exist_ok=True)  # 限制目录权限

    def get_connection(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # 使结果可以通过列名访问
        return conn

    def init_database(self):
        """初始化数据库表"""
        with self.get_connection() as conn:
            # 域名表
            conn.execute('''
                         CREATE TABLE IF NOT EXISTS domains
                         (
                             id         INTEGER PRIMARY KEY AUTOINCREMENT,
                             domain     TEXT UNIQUE NOT NULL,
                             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                             updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                         )
                         ''')

            # 证书表
            conn.execute('''
                         CREATE TABLE IF NOT EXISTS certificates
                         (
                             id             INTEGER PRIMARY KEY AUTOINCREMENT,
                             domain_id      INTEGER   NOT NULL,
                             cert_path      TEXT      NOT NULL,
                             key_path       TEXT      NOT NULL,
                             fullchain_path TEXT      NOT NULL,
                             chain_path     TEXT      NOT NULL,
                             cert_content   TEXT,
                             key_content    TEXT,
                             fullchain_content TEXT,
                             chain_content  TEXT,
                             issued_at      TIMESTAMP NOT NULL,
                             expires_at     TIMESTAMP NOT NULL,
                             status         TEXT      DEFAULT 'active',
                             created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                             updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                             FOREIGN KEY (domain_id) REFERENCES domains (id)
                         )
                         ''')



            # 部署记录表
            conn.execute('''
                         CREATE TABLE IF NOT EXISTS deployments
                         (
                             id             INTEGER PRIMARY KEY AUTOINCREMENT,
                             certificate_id INTEGER NOT NULL,
                             server_id      INTEGER NOT NULL,
                             deployed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                             status         TEXT      DEFAULT 'success',
                             error_message  TEXT,
                             FOREIGN KEY (certificate_id) REFERENCES certificates (id),
                             FOREIGN KEY (server_id) REFERENCES servers (id)
                         )
                         ''')

            # 证书申请状态表
            conn.execute('''
                         CREATE TABLE IF NOT EXISTS certificate_requests
                         (
                             id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                             domain              TEXT UNIQUE NOT NULL,
                             status              TEXT      DEFAULT 'pending',
                             challenge_record_id TEXT,
                             challenge_value     TEXT,
                             created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                             updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                         )
                         ''')

            # ACME 速率限制表
            conn.execute('''
                         CREATE TABLE IF NOT EXISTS acme_rate_limits
                         (
                             id          INTEGER PRIMARY KEY AUTOINCREMENT,
                             domain      TEXT      NOT NULL UNIQUE,
                             last_attempt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                             attempt_count INTEGER DEFAULT 1,
                             created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                             updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                         )
                         ''')

            conn.commit()
            logger.debug("数据库初始化完成")

    def check_acme_rate_limit(self, domain: str, limit_minutes: int = 5) -> bool:
        """检查 ACME 速率限制

        Args:
            domain: 域名
            limit_minutes: 限制时间（分钟）

        Returns:
            True 如果可以继续请求，False 如果被限制
        """
        with self.get_connection() as conn:
            cursor = conn.execute('''
                SELECT last_attempt, attempt_count
                FROM acme_rate_limits
                WHERE domain = ?
            ''', (domain,))

            row = cursor.fetchone()
            # 使用 UTC 时间与数据库保持一致
            now = datetime.utcnow()

            if not row:
                # 首次请求，记录并允许
                self.record_acme_attempt(domain)
                return True

            # SQLite 存储的是 UTC 时间，需要转换
            last_attempt_str = row['last_attempt']
            # 如果时间字符串没有时区信息，假设是本地时间
            if '+' not in last_attempt_str and 'Z' not in last_attempt_str:
                last_attempt = datetime.fromisoformat(last_attempt_str)
            else:
                last_attempt = datetime.fromisoformat(last_attempt_str.replace('Z', '+00:00'))

            attempt_count = row['attempt_count']

            # 检查是否在限制时间内
            time_diff = now - last_attempt
            if time_diff.total_seconds() < limit_minutes * 60:
                logger.warning(f"域名 {domain} 在速率限制中，上次尝试: {last_attempt}")
                logger.warning(f"已尝试 {attempt_count} 次，需等待 {limit_minutes} 分钟")
                remaining_seconds = limit_minutes * 60 - time_diff.total_seconds()
                logger.warning(f"剩余等待时间: {remaining_seconds:.0f} 秒")
                # 增加尝试计数但不允许继续
                self.record_acme_attempt(domain, increment=True)
                return False

            # 超过限制时间，记录新的尝试并允许
            self.record_acme_attempt(domain, reset=True)
            return True

    def record_acme_attempt(self, domain: str, reset: bool = False, increment: bool = False) -> None:
        """记录 ACME 尝试

        Args:
            domain: 域名
            reset: 是否重置计数
            increment: 是否只增加计数（不更新时间）
        """
        with self.get_connection() as conn:
            if reset:
                # 重置计数
                conn.execute('''
                    UPDATE acme_rate_limits
                    SET last_attempt = CURRENT_TIMESTAMP,
                        attempt_count = 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE domain = ?
                ''', (domain,))
            elif increment:
                # 只增加计数，不更新时间
                conn.execute('''
                    UPDATE acme_rate_limits
                    SET attempt_count = attempt_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE domain = ?
                ''', (domain,))
            else:
                # 插入或更新记录
                conn.execute('''
                    INSERT INTO acme_rate_limits (domain, last_attempt, attempt_count)
                    VALUES (?, CURRENT_TIMESTAMP, 1)
                    ON CONFLICT(domain) DO UPDATE SET
                        last_attempt = CURRENT_TIMESTAMP,
                        attempt_count = attempt_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                ''', (domain,))

            conn.commit()

    def get_acme_rate_limit_info(self, domain: str) -> Optional[Dict]:
        """获取 ACME 速率限制信息

        Args:
            domain: 域名

        Returns:
            速率限制信息字典或None
        """
        with self.get_connection() as conn:
            cursor = conn.execute('''
                SELECT * FROM acme_rate_limits WHERE domain = ?
            ''', (domain,))

            row = cursor.fetchone()
            return dict(row) if row else None

    def add_domain(self, domain: str) -> int:
        """添加域名
        
        Args:
            domain: 域名
            
        Returns:
            域名ID
        """
        with self.get_connection() as conn:
            cursor = conn.execute(
                'INSERT OR IGNORE INTO domains (domain) VALUES (?)',
                (domain,)
            )
            if cursor.rowcount == 0:
                # 域名已存在，获取现有ID
                cursor = conn.execute('SELECT id FROM domains WHERE domain = ?', (domain,))
                return cursor.fetchone()['id']
            return cursor.lastrowid

    def get_domain(self, domain: str) -> Optional[Dict]:
        """获取域名信息
        
        Args:
            domain: 域名
            
        Returns:
            域名信息字典或None
        """
        with self.get_connection() as conn:
            cursor = conn.execute('SELECT * FROM domains WHERE domain = ?', (domain,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_domains(self) -> List[Dict]:
        """列出所有域名"""
        with self.get_connection() as conn:
            cursor = conn.execute('SELECT * FROM domains ORDER BY domain')
            return [dict(row) for row in cursor.fetchall()]

    def add_certificate(self, domain_id: int, cert_path: str, key_path: str,
                        fullchain_path: str, chain_path: str,
                        issued_at: datetime, expires_at: datetime,
                        cert_content: str = None, key_content: str = None,
                        fullchain_content: str = None, chain_content: str = None) -> int:
        """添加证书记录

        Args:
            domain_id: 域名ID
            cert_path: 证书文件路径
            key_path: 私钥文件路径
            fullchain_path: 完整证书链路径
            chain_path: 证书链路径
            issued_at: 签发时间
            expires_at: 过期时间
            cert_content: 证书文件内容
            key_content: 私钥文件内容
            fullchain_content: 完整证书链内容
            chain_content: 证书链内容

        Returns:
            证书ID
        """
        with self.get_connection() as conn:
            cursor = conn.execute('''
                                  INSERT INTO certificates
                                      (domain_id, cert_path, key_path, fullchain_path, chain_path,
                                       cert_content, key_content, fullchain_content, chain_content,
                                       issued_at, expires_at)
                                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                  ''', (domain_id, cert_path, key_path, fullchain_path, chain_path,
                                        cert_content, key_content, fullchain_content, chain_content,
                                        issued_at, expires_at))
            return cursor.lastrowid

    def get_certificate(self, domain: str) -> Optional[Dict]:
        """获取域名的最新证书

        Args:
            domain: 域名

        Returns:
            证书信息字典或None
        """
        with self.get_connection() as conn:
            cursor = conn.execute('''
                                  SELECT c.*, d.domain
                                  FROM certificates c
                                           JOIN domains d ON c.domain_id = d.id
                                  WHERE d.domain = ?
                                    AND c.status = 'active'
                                  ORDER BY c.created_at DESC
                                  LIMIT 1
                                  ''', (domain,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_certificates(self) -> List[Dict]:
        """列出所有证书"""
        with self.get_connection() as conn:
            cursor = conn.execute('''
                                  SELECT c.*, d.domain
                                  FROM certificates c
                                           JOIN domains d ON c.domain_id = d.id
                                  WHERE c.status = 'active'
                                  ORDER BY c.expires_at
                                  ''')
            return [dict(row) for row in cursor.fetchall()]

    def get_expiring_certificates(self, days: int = 30) -> List[Dict]:
        """获取即将过期的证书

        Args:
            days: 提前多少天

        Returns:
            即将过期的证书列表
        """
        with self.get_connection() as conn:
            cursor = conn.execute('''
                SELECT c.*, d.domain
                FROM certificates c
                JOIN domains d ON c.domain_id = d.id
                WHERE c.status = 'active'
                AND datetime(c.expires_at) <= datetime('now', '+{} days')
                ORDER BY c.expires_at
            '''.format(days))
            return [dict(row) for row in cursor.fetchall()]

    def update_certificate_status(self, cert_id: int, status: str):
        """更新证书状态

        Args:
            cert_id: 证书ID
            status: 新状态
        """
        with self.get_connection() as conn:
            conn.execute(
                'UPDATE certificates SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                (status, cert_id)
            )





    def add_certificate_request(self, domain: str, challenge_record_id: str = None,
                                challenge_value: str = None) -> int:
        """添加证书申请记录

        Args:
            domain: 域名
            challenge_record_id: 挑战记录 ID
            challenge_value: 挑战值

        Returns:
            申请记录 ID
        """
        with self.get_connection() as conn:
            cursor = conn.execute('''
                INSERT OR REPLACE INTO certificate_requests
                (domain, challenge_record_id, challenge_value, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (domain, challenge_record_id, challenge_value))
            return cursor.lastrowid

    def get_certificate_request(self, domain: str) -> Optional[Dict]:
        """获取证书申请记录

        Args:
            domain: 域名

        Returns:
            申请记录字典或None
        """
        with self.get_connection() as conn:
            cursor = conn.execute(
                'SELECT * FROM certificate_requests WHERE domain = ?',
                (domain,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_certificate_request_status(self, domain: str, status: str):
        """更新证书申请状态

        Args:
            domain: 域名
            status: 新状态 (pending, challenge_added, verified, completed, failed)
        """
        with self.get_connection() as conn:
            conn.execute('''
                         UPDATE certificate_requests
                         SET status     = ?,
                             updated_at = CURRENT_TIMESTAMP
                         WHERE domain = ?
                         ''', (status, domain))

    def delete_certificate_request(self, domain: str):
        """删除证书申请记录

        Args:
            domain: 域名
        """
        with self.get_connection() as conn:
            conn.execute('DELETE FROM certificate_requests WHERE domain = ?', (domain,))
