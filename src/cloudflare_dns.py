#!/usr/bin/env python3
"""
Cloudflare DNS API 管理模块
"""

import logging
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class CloudflareDNS:
    """Cloudflare DNS API 客户端"""

    def __init__(self, api_token: str = None, zone_id: str = None):
        """初始化 Cloudflare DNS 客户端

        Args:
            api_token: Cloudflare API Token
            zone_id: Zone ID (可选，会自动获取)
        """
        self.api_token = api_token
        self.zone_id = zone_id

        if not self.api_token:
            raise ValueError("Cloudflare API Token 未设置，请在配置文件中配置 cloudflare.api_token")

        self.base_url = "https://api.cloudflare.com/client/v4"
        self.headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json'
        }

        # 缓存 zone 信息
        self._zones_cache = {}

    def _make_request(self, method: str, endpoint: str, data: Dict = None) -> Dict:
        """发送 API 请求
        
        Args:
            method: HTTP 方法
            endpoint: API 端点
            data: 请求数据
            
        Returns:
            API 响应数据
        """
        url = f"{self.base_url}{endpoint}"

        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=self.headers, params=data)
            elif method.upper() == 'POST':
                response = requests.post(url, headers=self.headers, json=data)
            elif method.upper() == 'PUT':
                response = requests.put(url, headers=self.headers, json=data)
            elif method.upper() == 'DELETE':
                response = requests.delete(url, headers=self.headers)
            else:
                raise ValueError(f"不支持的 HTTP 方法: {method}")

            response.raise_for_status()
            result = response.json()

            if not result.get('success', False):
                errors = result.get('errors', [])
                error_msg = ', '.join([err.get('message', str(err)) for err in errors])
                raise Exception(f"Cloudflare API 错误: {error_msg}")

            return result

        except requests.exceptions.RequestException as e:
            logger.error(f"API 请求失败: {e}")
            raise Exception(f"API 请求失败: {e}")

    def get_zones(self) -> List[Dict]:
        """获取所有 Zone 列表
        
        Returns:
            Zone 列表
        """
        try:
            result = self._make_request('GET', '/zones')
            zones = result.get('result', [])

            # 更新缓存
            for zone in zones:
                self._zones_cache[zone['name']] = zone['id']

            return zones

        except Exception as e:
            logger.error(f"获取 Zone 列表失败: {e}")
            raise

    def get_zone_id(self, domain: str) -> str:
        """获取域名的 Zone ID
        
        Args:
            domain: 域名
            
        Returns:
            Zone ID
        """
        # 如果已经设置了 zone_id，直接返回
        if self.zone_id:
            return self.zone_id

        # 从缓存中查找
        if domain in self._zones_cache:
            return self._zones_cache[domain]

        # 尝试根域名
        root_domain = self._get_root_domain(domain)
        if root_domain in self._zones_cache:
            return self._zones_cache[root_domain]

        # 从 API 获取
        try:
            result = self._make_request('GET', '/zones', {'name': root_domain})
            zones = result.get('result', [])

            if not zones:
                raise Exception(f"未找到域名 {root_domain} 的 Zone")

            zone_id = zones[0]['id']
            self._zones_cache[root_domain] = zone_id
            return zone_id

        except Exception as e:
            logger.error(f"获取 Zone ID 失败: {e}")
            raise

    def _get_root_domain(self, domain: str) -> str:
        """获取根域名
        
        Args:
            domain: 域名
            
        Returns:
            根域名
        """
        parts = domain.split('.')
        if len(parts) >= 2:
            return '.'.join(parts[-2:])
        return domain

    def list_dns_records(self, domain: str, record_type: str = None, name: str = None) -> List[Dict]:
        """列出 DNS 记录
        
        Args:
            domain: 域名
            record_type: 记录类型 (A, AAAA, CNAME, MX, TXT 等)
            name: 记录名称
            
        Returns:
            DNS 记录列表
        """
        try:
            zone_id = self.get_zone_id(domain)

            params = {}
            if record_type:
                params['type'] = record_type.upper()
            if name:
                params['name'] = name

            result = self._make_request('GET', f'/zones/{zone_id}/dns_records', params)
            return result.get('result', [])

        except Exception as e:
            logger.error(f"列出 DNS 记录失败: {e}")
            raise

    def find_dns_record(self, domain: str, record_type: str, name: str) -> Optional[Dict]:
        """查找特定的 DNS 记录
        
        Args:
            domain: 域名
            record_type: 记录类型
            name: 记录名称
            
        Returns:
            DNS 记录或 None
        """
        records = self.list_dns_records(domain, record_type, name)

        logger.debug(f"查找到 DNS 记录: {domain} {record_type} {name} -> {records}")

        return records[0] if records else None

    def create_dns_record(self, domain: str, record_type: str, name: str,
                          content: str, ttl: int = 300, priority: int = None) -> Dict:
        """创建 DNS 记录

        Args:
            domain: 域名
            record_type: 记录类型
            name: 记录名称
            content: 记录内容
            ttl: TTL 值
            priority: 优先级 (MX 记录使用)

        Returns:
            创建的 DNS 记录
        """
        try:
            zone_id = self.get_zone_id(domain)

            data = {
                'type': record_type.upper(),
                'name': name,
                'content': content,
                'ttl': ttl
            }

            if priority is not None:
                data['priority'] = priority

            result = self._make_request('POST', f'/zones/{zone_id}/dns_records', data)
            record = result.get('result', {})

            logger.info(f"DNS 记录创建成功: {name} {record_type} {content}")
            return record

        except Exception as e:
            logger.error(f"创建 DNS 记录失败: {e}")
            raise

    def delete_dns_record(self, domain: str, record_id: str) -> bool:
        """删除 DNS 记录

        Args:
            domain: 域名
            record_id: 记录 ID

        Returns:
            是否删除成功
        """
        try:
            zone_id = self.get_zone_id(domain)

            self._make_request('DELETE', f'/zones/{zone_id}/dns_records/{record_id}')

            logger.info(f"DNS 记录删除成功: {record_id}")
            return True

        except Exception as e:
            logger.error(f"删除 DNS 记录失败: {e}")
            return False

    def update_dns_record(self, domain: str, record_id: str, record_type: str,
                          name: str, content: str, ttl: int = 300, priority: int = None) -> Dict:
        """更新 DNS 记录

        Args:
            domain: 域名
            record_id: 记录 ID
            record_type: 记录类型
            name: 记录名称
            content: 记录内容
            ttl: TTL 值
            priority: 优先级

        Returns:
            更新后的 DNS 记录
        """
        try:
            zone_id = self.get_zone_id(domain)

            data = {
                'type': record_type.upper(),
                'name': name,
                'content': content,
                'ttl': ttl
            }

            if priority is not None:
                data['priority'] = priority

            result = self._make_request('PUT', f'/zones/{zone_id}/dns_records/{record_id}', data)
            record = result.get('result', {})

            logger.info(f"DNS 记录更新成功: {name} {record_type} {content}")
            return record

        except Exception as e:
            logger.error(f"更新 DNS 记录失败: {e}")
            raise

    def create_acme_challenge_record(self, domain: str, challenge_value: str) -> Dict:
        """创建 ACME 挑战记录
        
        Args:
            domain: 域名
            challenge_value: 挑战值
            
        Returns:
            创建的 DNS 记录
        """
        challenge_name = f"_acme-challenge.{domain}"
        challenge_value = '"%s"' % challenge_value.strip('"')

        # 检查是否已存在记录
        existing_record = self.find_dns_record(domain, 'TXT', challenge_name)
        if existing_record:
            # 更新现有记录
            return self.update_dns_record(
                domain=domain,
                record_id=existing_record['id'],
                record_type='TXT',
                name=challenge_name,
                content=challenge_value,
                ttl=120  # 短 TTL 用于快速验证
            )
        else:
            # 创建新记录
            return self.create_dns_record(
                domain=domain,
                record_type='TXT',
                name=challenge_name,
                content=challenge_value,
                ttl=120
            )

    def cleanup_acme_challenge_record(self, domain: str) -> bool:
        """清理 ACME 挑战记录
        
        Args:
            domain: 域名
            
        Returns:
            是否清理成功
        """
        challenge_name = f"_acme-challenge.{domain}"
        record = self.find_dns_record(domain, 'TXT', challenge_name)

        if record:
            return self.delete_dns_record(domain, record['id'])

        return True  # 记录不存在也算成功

    def wait_for_propagation(self, domain: str, record_type: str, name: str,
                             expected_content: str, timeout: int = 300) -> bool:
        """等待 DNS 传播
        
        Args:
            domain: 域名
            record_type: 记录类型
            name: 记录名称
            expected_content: 期望的记录内容
            timeout: 超时时间（秒）
            
        Returns:
            是否传播成功
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                record = self.find_dns_record(domain, record_type, name)
                if record:
                    actual_content = record.get('content', '')
                    logger.debug(f"DNS 记录查询结果: {name}")
                    logger.debug(f"  期望内容: '{expected_content}'")
                    logger.debug(f"  实际内容: '{actual_content}'")

                    # 处理 TXT 记录的双引号问题
                    # Cloudflare 可能返回带引号或不带引号的内容
                    content_matches = (
                        actual_content == expected_content or
                        actual_content == f'"{expected_content}"' or
                        actual_content.strip('"') == expected_content.strip('"')
                    )

                    logger.debug(f"  内容匹配: {content_matches}")

                    if content_matches:
                        logger.info(f"DNS 记录传播成功: {name}")
                        return True
                else:
                    logger.debug(f"DNS 记录未找到: {name}")

                logger.info(f"等待 DNS 传播: {name}")
                time.sleep(10)

            except Exception as e:
                logger.warning(f"检查 DNS 传播时出错: {e}")
                time.sleep(10)

        logger.error(f"DNS 传播超时: {name}")
        return False
