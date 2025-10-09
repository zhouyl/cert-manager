#!/usr/bin/env python3
"""
证书管理器 - 主程序
"""

import logging
import os
import sys

import click

# 添加 src 目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from config import ConfigManager
from database import DatabaseManager
from acme import ACMEManager
from deploy import DeployManager


def setup_logging(config_manager: ConfigManager):
    """设置日志"""
    log_config = config_manager.get_logging_config()
    log_file = config_manager.expand_path(log_config.get('file', './logs/cert_manager.log'))
    log_level = getattr(logging, log_config.get('level', 'INFO').upper())

    # 确保日志目录存在，限制权限
    os.makedirs(os.path.dirname(log_file), mode=0o750, exist_ok=True)

    # 创建根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # 清除现有处理器
    root_logger.handlers.clear()

    # 文件处理器 - 详细格式
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_formatter = logging.Formatter(
        '[%(name)s][%(asctime)s][%(levelname)s] - %(message)s'
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # 控制台处理器 - 简洁格式
    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = logging.Formatter('[%(asctime)s][%(levelname)s] - %(message)s')
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)


def validate_cloudflare_config(config_manager):
    """验证 Cloudflare 配置"""
    logger = logging.getLogger(__name__)
    cf_config = config_manager.get('cloudflare', {})
    api_token = cf_config.get('api_token')

    if not api_token or api_token.strip() == "":
        logger.error("❌ 错误: Cloudflare API Token 未配置")
        logger.error("请在配置文件中设置 cloudflare.api_token")
        logger.error("获取方式: https://dash.cloudflare.com/profile/api-tokens")
        sys.exit(1)


@click.group()
@click.option('--config', '-c', default='config/config.yaml', help='配置文件路径')
@click.pass_context
def cli(ctx, config):
    """证书管理器 - 使用 acme.sh 管理 Let's Encrypt 证书"""
    # 初始化配置和管理器
    config_manager = ConfigManager(config)
    setup_logging(config_manager)

    # 验证 Cloudflare 配置
    validate_cloudflare_config(config_manager)

    database_manager = DatabaseManager(config_manager.get_database_path())
    acme_manager = ACMEManager(config_manager, database_manager)
    deploy_manager = DeployManager(config_manager, database_manager)

    # 将管理器存储在上下文中
    ctx.ensure_object(dict)
    ctx.obj['config'] = config_manager
    ctx.obj['db'] = database_manager
    ctx.obj['acme'] = acme_manager
    ctx.obj['deploy'] = deploy_manager


@cli.command()
@click.argument('domain')
@click.pass_context
def issue(ctx, domain):
    """申请证书 (通配符域名，使用 Cloudflare DNS 验证)"""
    logger = logging.getLogger(__name__)
    acme_manager = ctx.obj['acme']

    try:
        logger.info(f"开始为域名 {domain} 申请通配符证书...")
        logger.info("使用 Cloudflare DNS 验证...")

        cert_files = acme_manager.issue_certificate(domain=domain)

        logger.info("✅ 证书申请成功!")
        logger.info("证书文件路径:")
        for name, path in cert_files.items():
            logger.info(f"  {name}: {path}")

        # 显示部署命令
        deploy_cmd = f"python {sys.argv[0]} deploy {domain} --all-server"
        logger.info("📋 部署命令:")
        logger.info(f"  {deploy_cmd}")

    except Exception as e:
        logger.error(f"❌ 证书申请失败: {e}")
        sys.exit(1)


@cli.command()
@click.argument('domain', required=False)
@click.pass_context
def renew(ctx, domain):
    """续期证书"""
    logger = logging.getLogger(__name__)
    acme_manager = ctx.obj['acme']

    try:
        if domain:
            # 续期指定域名
            logger.info(f"开始续期证书: {domain}")
            success = acme_manager.renew_certificate(domain)

            if success:
                logger.info(f"✅ 证书续期成功: {domain}")
            else:
                logger.error(f"❌ 证书续期失败: {domain}")
                sys.exit(1)
        else:
            # 自动续期所有需要续期的证书
            logger.info("检查需要续期的证书...")
            results = acme_manager.auto_renew_all()

            if not results:
                logger.info("✅ 没有需要续期的证书")
                return

            success_count = sum(1 for success in results.values() if success)
            total_count = len(results)

            logger.info(f"续期结果: {success_count}/{total_count} 成功")

            for domain, success in results.items():
                status = "✅" if success else "❌"
                logger.info(f"  {status} {domain}")

            if success_count < total_count:
                sys.exit(1)

    except Exception as e:
        logger.error(f"❌ 续期失败: {e}")
        sys.exit(1)


@cli.command()
@click.pass_context
def list(ctx):
    """列出通过本项目申请的证书域名及状态"""
    logger = logging.getLogger(__name__)
    acme_manager = ctx.obj['acme']

    try:
        certificates = acme_manager.list_certificates()

        if not certificates:
            logger.info("没有找到证书")
            return

        logger.info(f"{'域名':<25} {'状态':<12} {'过期时间':<12} {'剩余天数':<10} {'验证状态':<10}")
        logger.info("-" * 80)

        for cert in certificates:
            domain = cert['domain']
            status = cert['status_desc']
            expires_at = cert['expires_at'][:10]  # 只显示日期部分
            days_left = cert['days_left']

            # 检查域名验证状态
            verify_status = "✅ 已验证" if cert.get('status') == 'active' else "❌ 未验证"

            logger.info(f"{domain:<25} {status:<12} {expires_at:<12} {days_left:<10} {verify_status:<10}")

    except Exception as e:
        logger.error(f"❌ 列出证书失败: {e}")
        sys.exit(1)


@cli.command()
@click.argument('domain')
@click.option('--all-server', is_flag=True, help='部署到所有启用的服务器')
@click.option('--server', '-s', help='指定服务器名称')
@click.option('--identity', '-i', help='SSH 密钥文件路径')
@click.option('--directory', '-d', help='证书部署目录')
@click.option('--reload', '-r', help='重载命令')
@click.pass_context
def deploy(ctx, domain, all_server, server, identity, directory, reload):
    """部署证书到服务器"""
    logger = logging.getLogger(__name__)
    deploy_manager = ctx.obj['deploy']

    try:
        logger.info(f"开始部署证书: {domain}")

        # 构建部署选项
        deploy_options = {}
        if identity:
            deploy_options['identity_file'] = identity
        if directory:
            deploy_options['cert_directory'] = directory
        if reload:
            deploy_options['reload_command'] = reload

        if all_server:
            logger.info("目标服务器: 所有启用的服务器")
            results = deploy_manager.deploy_certificate(domain, None, deploy_options)
        elif server:
            logger.info(f"目标服务器: {server}")
            results = deploy_manager.deploy_certificate(domain, server, deploy_options)
        else:
            logger.error("❌ 请指定 --all-server 或 --server 参数")
            sys.exit(1)

        success_count = sum(1 for success in results.values() if success)
        total_count = len(results)

        logger.info(f"部署结果: {success_count}/{total_count} 成功")

        for server_name, success in results.items():
            status = "✅" if success else "❌"
            logger.info(f"  {status} {server_name}")

        if success_count < total_count:
            sys.exit(1)

    except Exception as e:
        logger.error(f"❌ 部署失败: {e}")
        sys.exit(1)


@cli.group()
def server():
    """服务器管理 (基于配置文件)"""
    pass


@server.command('list')
@click.pass_context
def list_servers(ctx):
    """列出配置文件中的服务器"""
    logger = logging.getLogger(__name__)
    config_manager = ctx.obj['config']

    try:
        servers = config_manager.get_servers()

        if not servers:
            logger.info("配置文件中没有服务器配置")
            logger.info("请在 config/config.yaml 中添加服务器配置")
            return

        logger.info(f"{'name':<15} {'host':<20} {'user':<10} {'port':<6} {'status':<6}")
        logger.info("-" * 65)

        for server in servers:
            name = server.get('name', 'N/A')
            host = server.get('host', 'N/A')
            username = server.get('username', 'N/A')
            port = server.get('port', 22)
            enabled = "active" if server.get('enabled', True) else "disabled"

            logger.info(f"{name:<15} {host:<20} {username:<10} {port:<6} {enabled:<6}")

    except Exception as e:
        logger.error(f"❌ 列出服务器配置失败: {e}")
        sys.exit(1)


@server.command('test')
@click.argument('name')
@click.pass_context
def test_server(ctx, name):
    """测试配置文件中指定服务器的连接"""
    logger = logging.getLogger(__name__)
    config_manager = ctx.obj['config']

    try:
        servers = config_manager.get_servers()
        server_config = None

        for server in servers:
            if server.get('name') == name:
                server_config = server
                break

        if not server_config:
            logger.error(f"❌ 在配置文件中未找到服务器: {name}")
            logger.error("请检查 config/config.yaml 中的服务器配置")
            sys.exit(1)

        logger.info(f"测试服务器连接: {name}")

        # 直接使用 deploy_manager 测试连接
        deploy_manager = ctx.obj['deploy']
        success = deploy_manager.test_server_connection(name)

        if success:
            logger.info(f"✅ 服务器连接测试成功: {name}")
        else:
            logger.error(f"❌ 服务器连接测试失败: {name}")
            sys.exit(1)

    except Exception as e:
        logger.error(f"❌ 测试服务器连接失败: {e}")
        sys.exit(1)


if __name__ == '__main__':
    cli()
