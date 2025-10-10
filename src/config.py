#!/usr/bin/env python3
"""
配置管理模块
"""

import logging
import os
from typing import Any, Dict, List

import yaml

logger = logging.getLogger(__name__)


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_path: str = "config/config.yaml"):
        """初始化配置管理器
        
        Args:
            config_path: 配置文件路径
        """
        self.config_path = config_path
        self.config = self.load_config()

    def load_config(self) -> Dict[str, Any]:
        """加载配置文件
        
        Returns:
            配置字典
        """
        if not os.path.exists(self.config_path):
            logger.warning(f"配置文件不存在: {self.config_path}")
            return self.get_default_config()

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                logger.info(f"配置文件加载成功: {self.config_path}")
                return config or {}
        except Exception as e:
            logger.error(f"配置文件加载失败: {e}")
            return self.get_default_config()

    def get_default_config(self) -> Dict[str, Any]:
        """获取默认配置
        
        Returns:
            默认配置字典
        """
        return {
            'acme': {
                'script_path': '/root/.acme.sh/acme.sh',
                'ca': 'letsencrypt',
                'challenge': 'dns',
                'dns_api': '',
                'cert_dir': './certs'
            },
            'database': {
                'path': './db/cert_manager.db'
            },
            'logging': {
                'level': 'INFO',
                'file': './logs/cert_manager.log',
                'max_size': '100MB',
                'backup_count': 3
            },
            'servers': [],
            'renewal': {
                'days_before_expiry': 30,
            }
        }

    def save_config(self):
        """保存配置到文件"""
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True)
            logger.info(f"配置文件保存成功: {self.config_path}")
        except Exception as e:
            logger.error(f"配置文件保存失败: {e}")
            raise

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值
        
        Args:
            key: 配置键，支持点号分隔的嵌套键
            default: 默认值
            
        Returns:
            配置值
        """
        keys = key.split('.')
        value = self.config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def set(self, key: str, value: Any):
        """设置配置值
        
        Args:
            key: 配置键，支持点号分隔的嵌套键
            value: 配置值
        """
        keys = key.split('.')
        config = self.config

        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]

        config[keys[-1]] = value

    def get_acme_config(self) -> Dict[str, Any]:
        """获取 acme.sh 配置"""
        return self.get('acme', {})

    def get_database_path(self) -> str:
        """获取数据库路径"""
        return self.get('database.path', './db/cert_manager.db')

    def get_cert_dir(self) -> str:
        """获取证书目录"""
        return self.get('acme.cert_dir', './certs')

    def get_servers(self) -> List[Dict[str, Any]]:
        """获取服务器配置列表"""
        return self.get('servers', [])

    def get_renewal_config(self) -> Dict[str, Any]:
        """获取续期配置"""
        return self.get('renewal', {'days_before_expiry': 7})

    def get_logging_config(self) -> Dict[str, Any]:
        """获取日志配置"""
        return self.get('logging', {
            'level': 'INFO',
            'file': './logs/cert_manager.log',
            'max_size': '10MB',
            'backup_count': 5
        })

    def expand_path(self, path: str) -> str:
        """展开路径，支持相对路径、用户目录和环境变量

        Args:
            path: 原始路径

        Returns:
            展开后的绝对路径
        """
        if not path:
            return path

        # 展开环境变量
        path = os.path.expandvars(path)

        # 展开用户目录
        path = os.path.expanduser(path)

        # 如果是相对路径，转换为绝对路径
        if not os.path.isabs(path):
            # 相对于项目根目录
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(project_root, path)

        return os.path.abspath(path)
