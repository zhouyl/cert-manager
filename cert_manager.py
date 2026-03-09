#!/usr/bin/env python3
"""
证书管理器 - 主程序
"""

import logging
import os
import sys
import traceback

import click

# 添加 src 目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from config import ConfigManager
from database import DatabaseManager
from acme import ACMEManager
from deploy import DeployManager
from nginx_config import NginxConfigGenerator


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
    console_formatter = logging.Formatter('[%(levelname)s] %(message)s')
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
        logger.error(f"❌ 续期失败: {e}, " + traceback.format_exc())
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

        logger.info("-" * 80)
        logger.info(f"{'ID':<8} {'DOMAIN':<25} {'STATUS':<12} {'EXPIRES AT':<15} {'DAYS':<10} {'VERIFY':<10}")
        logger.info("-" * 80)

        for cert in certificates:
            id = cert['id']
            domain = cert['domain']
            status = cert['status']
            expires_at = cert['expires_at'][:10]  # 只显示日期部分
            days_left = cert['days_left']

            # 检查域名验证状态
            verify_status = "✅" if cert.get('status') == 'active' else "❌"

            logger.info(f"{id:<10} {domain:<25} {status:<12} {expires_at:<15} {days_left:<10} {verify_status:<10}")

        logger.info("-" * 80)

    except Exception as e:
        logger.error(f"❌ 列出证书失败: {e}")
        sys.exit(1)


@cli.command()
@click.argument('domain')
@click.option('--all-server', '-a', is_flag=True, help='部署到所有启用的服务器')
@click.option('--server', '-s', help='指定服务器名称')
@click.option('--identity', '-i', help='SSH 密钥文件路径')
@click.option('--directory', '-d', help='证书部署目录')
@click.option('--reload', '-r', is_flag=True, help='重载命令')
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
            deploy_options['reload'] = reload

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


@cli.command()
@click.argument('cert_id', type=int)
@click.option('--force', '-f', is_flag=True, help='强制删除，不需要确认')
@click.pass_context
def delete(ctx, cert_id, force):
    """删除证书及其相关记录"""
    logger = logging.getLogger(__name__)
    db_manager = ctx.obj['db']

    try:
        # 获取证书信息用于显示
        with db_manager.get_connection() as conn:
            cursor = conn.execute(
                'SELECT c.id, d.domain, c.expires_at FROM certificates c '
                'JOIN domains d ON c.domain_id = d.id WHERE c.id = ?',
                (cert_id,)
            )
            cert_info = cursor.fetchone()

        if not cert_info:
            logger.error(f"❌ 未找到证书 ID: {cert_id}")
            sys.exit(1)

        domain = cert_info['domain']
        expires_at = cert_info['expires_at']

        logger.info(f"准备删除证书:")
        logger.info(f"  证书 ID: {cert_id}")
        logger.info(f"  域名: {domain}")
        logger.info(f"  过期时间: {expires_at}")

        # 如果没有 --force 标志，需要用户确认
        if not force:
            confirm = click.confirm("确认删除此证书及其相关记录吗?", default=False)
            if not confirm:
                logger.info("❌ 删除已取消")
                return

        # 执行删除
        success = db_manager.delete_certificate_by_id(cert_id)

        if success:
            logger.info(f"✅ 证书删除成功!")
            logger.info(f"  已删除证书 ID: {cert_id}")
            logger.info(f"  已删除域名 {domain} 的相关证书申请记录")
        else:
            logger.error(f"❌ 证书删除失败: 未找到证书 ID {cert_id}")
            sys.exit(1)

    except Exception as e:
        logger.error(f"❌ 删除证书失败: {e}")
        logger.exception("删除证书异常详情:")
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


@cli.group()
def nginx():
    """Nginx 配置管理"""
    pass


@nginx.command('preview')
@click.argument('domain')
@click.option('--server', '-s', help='指定服务器名称')
@click.pass_context
def preview_nginx_config(ctx, domain, server):
    """预览 Nginx 配置文件内容"""
    logger = logging.getLogger(__name__)
    config_manager = ctx.obj['config']

    try:
        nginx_config = NginxConfigGenerator(config_manager)
        servers = config_manager.get_servers()

        if server:
            # 查找指定服务器
            server_config = None
            for srv in servers:
                if srv.get('name') == server:
                    server_config = srv
                    break

            if not server_config:
                logger.error(f"❌ 未找到服务器: {server}")
                sys.exit(1)

            servers = [server_config]

        for server_config in servers:
            if not server_config.get('enabled', True):
                continue

            server_name = server_config.get('name', 'unknown')

            if not nginx_config.should_generate_config(server_config):
                logger.info(f"服务器 {server_name} 未配置 cert_conf_file，跳过")
                continue

            logger.info(f"=== 服务器: {server_name} ===")

            config_content = nginx_config.generate_config_file_content(domain, server_config)
            config_path = nginx_config.get_config_file_path(domain, server_config)

            logger.info(f"配置文件路径: {config_path}")
            logger.info("配置文件内容:")
            logger.info("-" * 50)
            logger.info(config_content)
            logger.info("-" * 50)
            logger.info("")

    except Exception as e:
        logger.error(f"❌ 预览 Nginx 配置失败: {e}")
        logger.exception("预览 Nginx 配置异常详情:")
        sys.exit(1)


if __name__ == '__main__':
    cli()
