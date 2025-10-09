#!/usr/bin/env python3
"""
证书部署模块
"""

import logging
import os
from typing import Dict, List

import paramiko

logger = logging.getLogger(__name__)


class DeployManager:
    """证书部署管理器"""

    def __init__(self, config_manager, database_manager):
        """初始化部署管理器
        
        Args:
            config_manager: 配置管理器
            database_manager: 数据库管理器
        """
        self.config = config_manager
        self.db = database_manager

    def deploy_certificate(self, domain: str, server_name: str = None,
                           deploy_options: Dict = None) -> Dict[str, bool]:
        """部署证书到服务器

        Args:
            domain: 域名
            server_name: 服务器名称，为None时部署到所有启用的服务器
            deploy_options: 部署选项覆盖

        Returns:
            部署结果字典 {服务器名称: 是否成功}
        """
        deploy_options = deploy_options or {}
        # 获取证书信息
        cert = self.db.get_certificate(domain)
        if not cert:
            raise Exception(f"未找到域名 {domain} 的证书")

        # 从配置文件获取服务器列表
        config_servers = self.config.get_servers()
        if not config_servers:
            raise Exception("配置文件中没有服务器配置")

        # 过滤目标服务器
        if server_name:
            target_servers = [s for s in config_servers if s.get('name') == server_name]
            if not target_servers:
                raise Exception(f"在配置文件中未找到服务器: {server_name}")
        else:
            target_servers = [s for s in config_servers if s.get('enabled', True)]
            if not target_servers:
                raise Exception("配置文件中没有启用的服务器")

        results = {}

        for server_config in target_servers:
            try:
                # 合并部署选项
                merged_config = server_config.copy()
                merged_config.update(deploy_options)

                success = self._deploy_to_server_config(cert, merged_config)
                results[server_config['name']] = success

                if success:
                    logger.info(f"证书部署成功: {domain} -> {server_config['name']}")
                else:
                    logger.error(f"证书部署失败: {domain} -> {server_config['name']}")

            except Exception as e:
                logger.error(f"部署证书到 {server_config['name']} 时发生错误: {e}")
                logger.exception(f"部署到 {server_config['name']} 异常详情:")
                results[server_config['name']] = False

        return results

    def _deploy_to_server_config(self, cert: Dict, server_config: Dict) -> bool:
        """部署证书到单个服务器

        Args:
            cert: 证书信息
            server_config: 服务器配置

        Returns:
            是否部署成功
        """
        try:
            # 创建 SSH 连接
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            # 连接参数
            connect_params = {
                'hostname': server_config['host'],
                'port': server_config.get('port', 22),
                'username': server_config['username']
            }

            # 使用密钥文件认证
            if server_config.get('identity_file'):
                identity_file = self.config.expand_path(server_config['identity_file'])
                if os.path.exists(identity_file):
                    connect_params['key_filename'] = identity_file
                else:
                    logger.warning(f"密钥文件不存在: {identity_file}")

            # 连接到服务器
            ssh.connect(**connect_params, timeout=30)
            logger.info(f"SSH 连接成功: {server_config['host']}")

            # 创建 SFTP 连接
            sftp = ssh.open_sftp()

            # 确保远程目录存在
            remote_cert_dir = server_config['cert_directory']
            self._ensure_remote_directory(ssh, remote_cert_dir)

            # 上传证书文件
            cert_files = {
                'cert.pem': cert['cert_path'],
                'privkey.pem': cert['key_path'],
                'fullchain.pem': cert['fullchain_path'],
                'chain.pem': cert['chain_path']
            }

            for remote_name, local_path in cert_files.items():
                if not os.path.exists(local_path):
                    logger.warning(f"本地证书文件不存在: {local_path}")
                    continue

                remote_path = os.path.join(remote_cert_dir, remote_name)
                sftp.put(local_path, remote_path)
                logger.info(f"文件上传成功: {local_path} -> {remote_path}")

                # 设置文件权限 (证书文件可读)
                try:
                    sftp.chmod(remote_path, 0o644)
                except Exception as e:
                    logger.warning(f"设置文件权限失败 {remote_path}: {e}")

            # 设置私钥文件权限 (仅所有者可读)
            privkey_remote = os.path.join(remote_cert_dir, 'privkey.pem')
            try:
                sftp.chmod(privkey_remote, 0o600)
            except Exception as e:
                logger.warning(f"设置私钥文件权限失败 {privkey_remote}: {e}")

            sftp.close()

            # 执行重载命令
            if server_config.get('reload', False) and server_config.get('reload_command'):
                self._execute_reload_command(ssh, server_config['reload_command'])

            ssh.close()
            return True

        except Exception as e:
            logger.error(f"部署到服务器 {server_config['name']} 失败: {e}")
            logger.exception(f"部署到服务器 {server_config['name']} 异常详情:")
            try:
                ssh.close()
            except:
                pass
            return False

    def _ensure_remote_directory(self, ssh: paramiko.SSHClient, directory: str):
        """确保远程目录存在
        
        Args:
            ssh: SSH 连接
            directory: 目录路径
        """
        try:
            stdin, stdout, stderr = ssh.exec_command(f'mkdir -p {directory}')
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                error = stderr.read().decode()
                logger.warning(f"创建远程目录失败: {error}")
        except Exception as e:
            logger.warning(f"创建远程目录时发生错误: {e}")

    def _execute_reload_command(self, ssh: paramiko.SSHClient, command: str):
        """执行重载命令
        
        Args:
            ssh: SSH 连接
            command: 重载命令
        """
        try:
            logger.info(f"执行重载命令: {command}")
            stdin, stdout, stderr = ssh.exec_command(command)
            exit_code = stdout.channel.recv_exit_status()

            if exit_code == 0:
                logger.info("重载命令执行成功")
            else:
                error = stderr.read().decode()
                logger.warning(f"重载命令执行失败: {error}")

        except Exception as e:
            logger.error(f"执行重载命令时发生错误: {e}")

    def test_server_connection(self, server_name: str) -> bool:
        """测试服务器连接

        Args:
            server_name: 服务器名称

        Returns:
            是否连接成功
        """
        # 从配置文件获取服务器配置
        config_servers = self.config.get_servers()
        server_config = None

        for server in config_servers:
            if server.get('name') == server_name:
                server_config = server
                break

        if not server_config:
            logger.error(f"在配置文件中未找到服务器: {server_name}")
            return False

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_params = {
                'hostname': server_config['host'],
                'port': server_config.get('port', 22),
                'username': server_config['username']
            }

            if server_config.get('identity_file'):
                identity_file = self.config.expand_path(server_config['identity_file'])
                if os.path.exists(identity_file):
                    connect_params['key_filename'] = identity_file

            ssh.connect(**connect_params, timeout=10)

            # 测试执行简单命令
            stdin, stdout, stderr = ssh.exec_command('echo "connection test"')
            exit_code = stdout.channel.recv_exit_status()

            ssh.close()

            if exit_code == 0:
                logger.info(f"服务器连接测试成功: {server_name}")
                return True
            else:
                logger.error(f"服务器连接测试失败: {server_name}")
                return False

        except Exception as e:
            logger.error(f"测试服务器连接时发生错误: {e}")
            logger.exception("测试服务器连接异常详情:")
            return False

    def get_deployment_history(self, domain: str = None, server_name: str = None) -> List[Dict]:
        """获取部署历史
        
        Args:
            domain: 域名过滤
            server_name: 服务器名称过滤
            
        Returns:
            部署历史列表
        """
        return self.db.get_deployment_history(domain, server_name)

    def generate_deploy_command(self, domain: str) -> str:
        """生成部署命令
        
        Args:
            domain: 域名
            
        Returns:
            部署命令字符串
        """
        return f"python cert_manager.py deploy {domain}"
