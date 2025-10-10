#!/usr/bin/env python3
"""
证书部署模块
"""

import logging
import os
from typing import Dict, List

import paramiko
from nginx_config import NginxConfigGenerator

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
        self.nginx_config = NginxConfigGenerator(config_manager)

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
            target_servers = config_servers
            if not target_servers:
                raise Exception("配置文件中没有启用的服务器")

        results = {}

        for server_config in target_servers:
            try:
                # 合并部署选项
                merged_config = server_config.copy()
                merged_config.update(deploy_options)

                success = self._deploy_to_server_config(domain, cert, merged_config)
                results[server_config['name']] = success

                if success:
                    logger.info(f"证书部署成功: {domain}  -> {server_config['name']} "
                                f"({server_config['host']}:{server_config.get('port', 22)})")
                else:
                    logger.error(f"证书部署失败: {domain}  -> {server_config['name']} "
                                 f"({server_config['host']}:{server_config.get('port', 22)})")

            except Exception as e:
                logger.error(f"部署证书 {domain} 到 {server_config['name']} "
                             f"({server_config['host']}:{server_config.get('port', 22)}) 时发生错误: {e}")
                results[server_config['name']] = False

            if len(target_servers) > 1:
                logger.info('-' * 80)

        return results

    def _deploy_to_server_config(self, domain: str, cert: Dict, server_config: Dict) -> bool:
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
                'username': server_config.get('username', 'root'),
                'password': server_config.get('password', None),
                'key_filename': server_config.get('key_filename', None),
            }

            # 如果配置了密码，优先使用密码认证
            # 否则尝试使用密钥文件认证
            if connect_params['password'] is None:
                if connect_params['key_filename'] is None:
                    connect_params['key_filename'] = '$HOME/.ssh/id_rsa'

                identity_file = self.config.expand_path(connect_params['key_filename'])
                if os.path.exists(identity_file):
                    if os.path.exists(identity_file):
                        connect_params['key_filename'] = identity_file
                    else:
                        logger.warning(f"密钥文件不存在: {identity_file}")

            # 连接到服务器
            ssh.connect(**connect_params, timeout=30)
            logger.info(f"SSH 连接成功: {server_config['name']} "
                        f"({server_config['host']}:{server_config.get('port', 22)})")

            # 创建 SFTP 连接
            sftp = ssh.open_sftp()

            # 确保远程目录存在
            remote_cert_dir = server_config.get('cert_directory', '/etc/nginx/certs/{domain}').replace('{domain}', domain)
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

            if server_config.get('cert_owner'):
                owner = server_config['cert_owner']
                chown_cmd = f"chown -R {owner} {remote_cert_dir}"
                try:
                    stdin, stdout, stderr = ssh.exec_command(chown_cmd)
                    exit_code = stdout.channel.recv_exit_status()
                    if exit_code != 0:
                        error = stderr.read().decode()
                        logger.warning(f"设置目录所有者失败: {error.strip()}")
                except Exception as e:
                    logger.warning(f"执行 chown 命令失败: {e}")

            # 生成 Nginx 配置文件（如果配置了）
            self._generate_nginx_config(ssh, domain, server_config)

            # 执行重载命令
            if server_config.get('reload', False) and server_config.get('reload_command'):
                self._execute_reload_command(ssh, server_config['reload_command'])

            sftp.close()
            ssh.close()
            return True

        except Exception as e:
            logger.error(f"部署服务器 {server_config['name']} "
                         f"({server_config['host']}:{server_config.get('port', 22)}) 失败: {e}")
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
            stdin, stdout, stderr = ssh.exec_command(command)
            exit_code = stdout.channel.recv_exit_status()

            if exit_code == 0:
                logger.info(f"Nginx 重载成功: {command}")
            else:
                error = stderr.read().decode()
                logger.warning(f"Nginx 重载失败: {command}, {error}")

        except Exception as e:
            logger.error(f"Nginx 重载失败: {command}, {e}")

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

    def _generate_nginx_config(self, ssh, domain: str, server_config: Dict) -> None:
        """生成 Nginx 配置文件

        Args:
            ssh: SSH 连接对象
            domain: 域名
            server_config: 服务器配置
        """
        try:
            # 生成配置文件内容
            config_content = self.nginx_config.generate_config_file_content(domain, server_config)
            if not config_content:
                return

            # 获取配置文件路径
            config_file_path = self.nginx_config.get_config_file_path(domain, server_config)
            if not config_file_path:
                return

            # 确保配置文件目录存在
            config_dir = os.path.dirname(config_file_path)
            mkdir_cmd = f"mkdir -p {config_dir}"
            stdin, stdout, stderr = ssh.exec_command(mkdir_cmd)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                error = stderr.read().decode()
                logger.warning(f"创建配置目录失败: {error}")
                return

            # 写入配置文件
            # 使用 cat 命令写入文件内容，避免特殊字符问题
            write_cmd = f"cat > {config_file_path}"
            stdin, stdout, stderr = ssh.exec_command(write_cmd)
            stdin.write(config_content)
            stdin.close()

            exit_code = stdout.channel.recv_exit_status()
            if exit_code == 0:
                logger.info(f"Nginx 配置生成成功: {config_file_path}")

                # 设置配置文件权限
                try:
                    chmod_cmd = f"chmod 644 {config_file_path}"
                    ssh.exec_command(chmod_cmd)
                except Exception as e:
                    logger.warning(f"设置配置文件权限失败: {e}")
            else:
                error = stderr.read().decode()
                logger.error(f"写入 Nginx 配置文件失败: {error}")

        except Exception as e:
            logger.error(f"生成 Nginx 配置时发生错误: {e}")

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
