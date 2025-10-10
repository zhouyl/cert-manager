#!/usr/bin/env python3
"""
ACME 证书申请模块
"""

import logging
import os
import subprocess
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from cryptography import x509
from cryptography.hazmat.backends import default_backend

from cloudflare_dns import CloudflareDNS

logger = logging.getLogger(__name__)


class ACMEManager:
    """ACME 证书管理器"""

    def __init__(self, config_manager, database_manager):
        """初始化 ACME 管理器

        Args:
            config_manager: 配置管理器
            database_manager: 数据库管理器
        """
        self.config = config_manager
        self.db = database_manager
        self.acme_config = self.config.get_acme_config()
        self.cert_dir = self.config.expand_path(self.acme_config.get('cert_dir', './certs'))

        # 初始化 Cloudflare DNS 客户端
        try:
            cf_config = self.config.get('cloudflare', {})
            self.cf_dns = CloudflareDNS(
                api_token=cf_config.get('api_token'),
                zone_id=cf_config.get('zone_id')
            )
            logger.debug("Cloudflare DNS 客户端初始化成功")
        except Exception as e:
            logger.warning(f"Cloudflare DNS 客户端初始化失败: {e}")
            self.cf_dns = None

        # 确保证书目录存在，限制权限
        os.makedirs(self.cert_dir, mode=0o750, exist_ok=True)

    def check_acme_installation(self) -> bool:
        """检查 acme.sh 是否已安装
        
        Returns:
            是否已安装
        """
        script_path = self.config.expand_path(
            self.acme_config.get('script_path', '$HOME/.acme.sh/acme.sh')
        )
        return os.path.exists(script_path) and os.access(script_path, os.X_OK)

    def install_acme(self, email: str) -> bool:
        """安装 acme.sh

        Args:
            email: 用于注册的邮箱地址

        Returns:
            是否安装成功
        """
        try:
            logger.info("开始安装 acme.sh...")
            logger.info(f"使用邮箱: {email}")

            # 下载并安装 acme.sh
            install_cmd = f'curl https://get.acme.sh | sh -s email={email}'

            result = subprocess.run(
                install_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode == 0:
                logger.info("acme.sh 安装成功")
                return True
            else:
                logger.error(f"acme.sh 安装失败: {result.stderr}")
                logger.error(f"失败的命令: {install_cmd}")
                logger.error("建议手动执行上述命令进行调试")
                return False

        except Exception as e:
            logger.error(f"安装 acme.sh 时发生错误: {e}")
            logger.exception("安装 acme.sh 异常详情:")
            return False

    def issue_certificate(self, domain: str) -> Dict[str, str]:
        """申请证书 (使用 Cloudflare DNS 验证)

        Args:
            domain: 域名 (根域名，会自动申请通配符证书)

        Returns:
            证书文件路径字典
        """
        # 检查本地速率限制
        if not self.db.check_acme_rate_limit(domain, limit_minutes=5):
            rate_info = self.db.get_acme_rate_limit_info(domain)
            if rate_info:
                last_attempt = rate_info['last_attempt']
                logger.error(f"❌ 域名 {domain} 受速率限制保护")
                logger.error(f"上次尝试时间: {last_attempt}")
                logger.error("请等待 5 分钟后再次尝试，避免触发 Let's Encrypt 速率限制")
                raise Exception(f"域名 {domain} 在本地速率限制中，请稍后再试")

        if not self.check_acme_installation():
            # 需要安装 acme.sh，要求用户输入邮箱
            import click
            email = click.prompt("请输入用于 acme.sh 注册的邮箱地址", type=str)
            if not self.install_acme(email):
                raise Exception("acme.sh 未安装且安装失败")

        if not self.cf_dns:
            raise Exception("Cloudflare DNS 客户端未初始化，请检查配置")

        # 检查是否有未完成的申请
        existing_request = self.db.get_certificate_request(domain)

        if existing_request:
            logger.info(f"发现未完成的证书申请: {domain}, 状态: {existing_request['status']}")

            if existing_request['status'] == 'challenge_added':
                logger.info("继续等待域名验证...")
                return self._continue_certificate_process(domain, existing_request)
            elif existing_request['status'] == 'verified':
                logger.info("域名验证已完成，开始生成证书...")
                return self._generate_certificate(domain, existing_request)

        # 开始新的申请流程
        logger.info(f"开始为域名 {domain} 申请通配符证书...")
        return self._start_certificate_process(domain)

    def _start_certificate_process(self, domain: str) -> Dict[str, str]:
        """开始证书申请流程"""
        try:
            # 检查是否已存在 DNS 挑战记录
            challenge_name = f"_acme-challenge.{domain}"
            existing_record = self.cf_dns.find_dns_record(domain, 'TXT', challenge_name)

            if existing_record:
                # 使用现有的 DNS 挑战记录
                challenge_value = existing_record.get('content', '').strip('"')
                challenge_value = f'"{challenge_value}"'  # 确保格式一致

                logger.info(f"发现现有 DNS 挑战记录: {challenge_name}")
                logger.info(f"使用现有挑战值进行验证")

                # 保存申请状态
                self.db.add_certificate_request(
                    domain=domain,
                    challenge_record_id=existing_record.get('id'),
                    challenge_value=challenge_value
                )
                self.db.update_certificate_request_status(domain, 'challenge_added')

            else:
                # 生成新的挑战值并添加 DNS 记录
                logger.info("第一步：添加 DNS 挑战记录...")

                import secrets
                import base64
                challenge_value = base64.b64encode(secrets.token_bytes(32)).decode().rstrip('=')
                challenge_value = '"%s"' % challenge_value.strip('"')  # TXT 记录需要包裹在引号中

                # 添加 _acme-challenge TXT 记录
                challenge_record = self.cf_dns.create_acme_challenge_record(domain, challenge_value)

                # 保存申请状态
                self.db.add_certificate_request(
                    domain=domain,
                    challenge_record_id=challenge_record.get('id'),
                    challenge_value=challenge_value
                )
                self.db.update_certificate_request_status(domain, 'challenge_added')

                logger.info(f"DNS 挑战记录已添加: _acme-challenge.{domain}")

            # 第二步：等待验证并生成证书
            return self._continue_certificate_process(domain, self.db.get_certificate_request(domain))

        except Exception as e:
            logger.error(f"添加 DNS 挑战记录失败: {e}")
            logger.exception("添加 DNS 挑战记录异常详情:")
            self.db.update_certificate_request_status(domain, 'failed')
            raise

    def _continue_certificate_process(self, domain: str, request_info: Dict) -> Dict[str, str]:
        """继续证书申请流程"""
        try:
            logger.info("等待域名验证通过...")

            # 等待 DNS 传播
            challenge_name = f"_acme-challenge.{domain}"
            if not self.cf_dns.wait_for_propagation(
                    domain, 'TXT', challenge_name, request_info['challenge_value'], timeout=300
            ):
                raise Exception("DNS 记录传播超时")

            logger.info("域名验证通过，开始生成证书...")
            self.db.update_certificate_request_status(domain, 'verified')

            return self._generate_certificate(domain, request_info)

        except KeyboardInterrupt:
            logger.info("用户中断操作，可以稍后继续...")
            raise
        except Exception as e:
            logger.error(f"域名验证失败: {e}")
            logger.exception("域名验证异常详情:")
            self.db.update_certificate_request_status(domain, 'failed')
            raise

    def _generate_certificate(self, domain: str, request_info: Dict) -> Dict[str, str]:
        """生成证书"""
        try:
            ca = self.acme_config.get('ca', 'letsencrypt')
            wildcard_domain = f"*.{domain}"

            # 创建域名专用目录，限制权限
            domain_cert_dir = os.path.join(self.cert_dir, domain)
            os.makedirs(domain_cert_dir, mode=0o750, exist_ok=True)

            # 构建 acme.sh 命令
            script_path = self.config.expand_path(
                self.acme_config.get('script_path', '$HOME/.acme.sh/acme.sh')
            )
            # 使用 --dns dns_cf，添加 --force 和 --debug 选项
            cmd = [
                script_path,
                '--issue',
                '-d', wildcard_domain,
                '-d', domain,
                '--server', ca,
                '--dns', 'dns_cf',
                '--force',  # 强制执行，即使记录已存在
                '--cert-file', os.path.join(domain_cert_dir, 'cert.pem'),
                '--key-file', os.path.join(domain_cert_dir, 'privkey.pem'),
                '--fullchain-file', os.path.join(domain_cert_dir, 'fullchain.pem'),
                '--ca-file', os.path.join(domain_cert_dir, 'chain.pem')
            ]

            # 设置环境变量
            self._setup_cloudflare_env()

            # 执行命令
            cmd_str = ' '.join(cmd)
            logger.info(f"执行命令: {cmd_str}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600
            )

            if result.returncode != 0:
                error_msg = f"证书生成失败: {result.stderr}"
                logger.error(error_msg)
                logger.error(f"失败的命令: {cmd_str}")
                logger.error("建议手动执行上述命令进行调试")
                raise Exception(error_msg)

            logger.info(f"证书生成成功: {domain}")

            # 验证证书文件
            cert_files = self._verify_certificate_files(domain_cert_dir)

            # 获取证书信息
            cert_info = self._get_certificate_info(cert_files['cert'])

            # 读取证书文件内容
            cert_contents = self._read_certificate_files(cert_files)

            # 保存到数据库（包含文件内容）
            domain_id = self.db.add_domain(domain)
            cert_id = self.db.add_certificate(
                domain_id=domain_id,
                cert_path=cert_files['cert'],
                key_path=cert_files['key'],
                fullchain_path=cert_files['fullchain'],
                chain_path=cert_files['chain'],
                issued_at=cert_info['issued_at'],
                expires_at=cert_info['expires_at'],
                cert_content=cert_contents['cert'],
                key_content=cert_contents['key'],
                fullchain_content=cert_contents['fullchain'],
                chain_content=cert_contents['chain']
            )

            logger.info(f"证书记录已保存到数据库，ID: {cert_id}")
            logger.info(f"证书文件已保存到: {domain_cert_dir}")
            logger.info("证书内容已同时保存到数据库")

            # 清理申请记录和 DNS 记录
            self._cleanup_certificate_request(domain, request_info)

            return cert_files

        except Exception as e:
            logger.error(f"生成证书时发生错误: {e}")
            logger.exception("生成证书异常详情:")
            self.db.update_certificate_request_status(domain, 'failed')
            raise

    def _read_certificate_files(self, cert_files: Dict[str, str]) -> Dict[str, str]:
        """读取证书文件内容

        Args:
            cert_files: 证书文件路径字典

        Returns:
            证书文件内容字典
        """
        contents = {}

        for file_type, file_path in cert_files.items():
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    contents[file_type] = f.read()
                logger.debug(f"已读取 {file_type} 文件内容: {file_path}")
            except Exception as e:
                logger.error(f"读取 {file_type} 文件失败: {file_path}, 错误: {e}")
                contents[file_type] = None

        return contents

    def _cleanup_certificate_request(self, domain: str, request_info: Dict):
        """清理证书申请记录和 DNS 记录"""
        try:
            # 删除 DNS 挑战记录
            if request_info.get('challenge_record_id'):
                self.cf_dns.delete_dns_record(domain, request_info['challenge_record_id'])
                logger.info("DNS 挑战记录已清理")

            # 删除申请记录
            self.db.delete_certificate_request(domain)
            logger.info("证书申请记录已清理")

        except Exception as e:
            logger.warning(f"清理申请记录时发生错误: {e}")

    def _verify_certificate_files(self, cert_dir: str) -> Dict[str, str]:
        """验证证书文件是否存在
        
        Args:
            cert_dir: 证书目录
            
        Returns:
            证书文件路径字典
        """
        files = {
            'cert': os.path.join(cert_dir, 'cert.pem'),
            'key': os.path.join(cert_dir, 'privkey.pem'),
            'fullchain': os.path.join(cert_dir, 'fullchain.pem'),
            'chain': os.path.join(cert_dir, 'chain.pem')
        }

        for name, path in files.items():
            if not os.path.exists(path):
                raise Exception(f"证书文件不存在: {path}")
            if os.path.getsize(path) == 0:
                raise Exception(f"证书文件为空: {path}")

        return files

    def _get_certificate_info(self, cert_path: str) -> Dict[str, datetime]:
        """获取证书信息
        
        Args:
            cert_path: 证书文件路径
            
        Returns:
            证书信息字典
        """
        try:
            with open(cert_path, 'rb') as f:
                cert_data = f.read()

            cert = x509.load_pem_x509_certificate(cert_data, default_backend())

            return {
                'issued_at': cert.not_valid_before,
                'expires_at': cert.not_valid_after
            }
        except Exception as e:
            logger.error(f"读取证书信息失败: {e}")
            # 如果无法读取证书，使用默认值
            now = datetime.now()
            return {
                'issued_at': now,
                'expires_at': now + timedelta(days=90)  # Let's Encrypt 默认90天
            }

    def _setup_cloudflare_env(self):
        """设置 Cloudflare 环境变量"""
        cf_config = self.config.get('cloudflare', {})

        # 设置 API Token
        api_token = cf_config.get('api_token')
        if api_token:
            os.environ['CF_Token'] = api_token
        else:
            raise Exception("Cloudflare API Token 未配置，请在配置文件中设置 cloudflare.api_token")

        # 设置 Zone ID (可选)
        zone_id = cf_config.get('zone_id')
        if zone_id:
            os.environ['CF_Zone_ID'] = zone_id

        # 设置 Account ID (可选)
        account_id = cf_config.get('account_id')
        if account_id and account_id.strip():
            os.environ['CF_Account_ID'] = account_id.strip()
        else:
            # 确保不使用错误的 account_id
            if 'CF_Account_ID' in os.environ:
                del os.environ['CF_Account_ID']

        logger.info("Cloudflare 环境变量设置完成")

    def renew_certificate(self, domain: str) -> bool:
        """续期证书

        Args:
            domain: 域名

        Returns:
            是否续期成功
        """
        try:
            logger.info(f"开始续期证书: {domain}")

            script_path = self.config.expand_path(
                self.acme_config.get('script_path', '$HOME/.acme.sh/acme.sh')
            )
            wildcard_domain = f"*.{domain}"

            # 构建续期命令
            cmd = [
                script_path,
                '--renew',
                '-d', wildcard_domain,
                '--force'  # 强制续期
            ]

            cmd_str = ' '.join(cmd)
            logger.info(f"执行续期命令: {cmd_str}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600
            )

            if result.returncode != 0:
                logger.error(f"证书续期失败: {result.stderr}")
                logger.error(f"失败的命令: {cmd_str}")
                logger.error("建议手动执行上述命令进行调试")
                return False

            logger.info(f"证书续期成功: {domain}")

            # 更新数据库中的证书信息
            domain_cert_dir = os.path.join(self.cert_dir, domain)
            cert_files = self._verify_certificate_files(domain_cert_dir)
            cert_info = self._get_certificate_info(cert_files['cert'])

            # 获取当前证书记录
            current_cert = self.db.get_certificate(domain)
            if current_cert:
                # 标记旧证书为过期
                self.db.update_certificate_status(current_cert['id'], 'expired')

            # 添加新的证书记录
            domain_id = self.db.add_domain(domain)
            self.db.add_certificate(
                domain_id=domain_id,
                cert_path=cert_files['cert'],
                key_path=cert_files['key'],
                fullchain_path=cert_files['fullchain'],
                chain_path=cert_files['chain'],
                issued_at=cert_info['issued_at'],
                expires_at=cert_info['expires_at']
            )

            return True

        except Exception as e:
            logger.error(f"续期证书时发生错误: {e}")
            return False

    def check_renewals(self) -> List[str]:
        """检查需要续期的证书

        Returns:
            需要续期的域名列表
        """
        renewal_config = self.config.get_renewal_config()
        days_before = renewal_config.get('days_before_expiry', 30)

        expiring_certs = self.db.get_expiring_certificates(days_before)
        domains_to_renew = []

        for cert in expiring_certs:
            domain = cert['domain']
            expires_at = datetime.fromisoformat(cert['expires_at'])
            days_left = (expires_at - datetime.now()).days

            logger.info(f"证书即将过期: {domain}, 剩余 {days_left} 天")
            domains_to_renew.append(domain)

        return domains_to_renew

    def auto_renew_all(self) -> Dict[str, bool]:
        """自动续期所有需要续期的证书

        Returns:
            续期结果字典 {域名: 是否成功}
        """
        domains_to_renew = self.check_renewals()
        results = {}

        for domain in domains_to_renew:
            try:
                success = self.renew_certificate(domain)
                results[domain] = success
                if success:
                    logger.info(f"自动续期成功: {domain}")
                else:
                    logger.error(f"自动续期失败: {domain}")
            except Exception as e:
                logger.error(f"自动续期 {domain} 时发生错误: {e}")
                results[domain] = False

        return results

    def list_certificates(self) -> List[Dict]:
        """列出所有证书及其状态

        Returns:
            证书列表
        """
        certificates = self.db.list_certificates()

        for cert in certificates:
            # 计算剩余天数
            expires_at = datetime.fromisoformat(cert['expires_at'])
            days_left = (expires_at - datetime.now()).days
            cert['days_left'] = days_left

            # 判断状态
            if days_left < 0:
                cert['status_desc'] = '已过期'
            elif days_left <= 30:
                cert['status_desc'] = '即将过期'
            else:
                cert['status_desc'] = '正常'

        return certificates

    def get_certificate_paths(self, domain: str) -> Optional[Dict[str, str]]:
        """获取域名的证书文件路径

        Args:
            domain: 域名

        Returns:
            证书文件路径字典或None
        """
        cert = self.db.get_certificate(domain)
        if not cert:
            return None

        return {
            'cert': cert['cert_path'],
            'key': cert['key_path'],
            'fullchain': cert['fullchain_path'],
            'chain': cert['chain_path']
        }
