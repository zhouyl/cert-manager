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
    db_manager = ctx.obj['db']

    try:
        # 检查域名是否已存在
        existing_domain = db_manager.get_domain(domain)
        if existing_domain:
            logger.error(f"❌ 域名已存在: {domain}")
            logger.error(f"   域名 ID: {existing_domain['id']}")
            logger.error(f"   创建时间: {existing_domain['created_at']}")
            logger.info("💡 如果需要续期证书，请使用 renew 命令")
            sys.exit(1)

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
@click.argument('domain', required=True)
@click.pass_context
def renew(ctx, domain):
    """执行域名手动续期"""
    logger = logging.getLogger(__name__)
    acme_manager = ctx.obj['acme']

    try:
        # 续期指定域名
        logger.info(f"开始续期证书: {domain}")
        success = acme_manager.renew_certificate(domain)

        if success:
            logger.info(f"✅ 证书续期成功: {domain}")
        else:
            logger.error(f"❌ 证书续期失败: {domain}")
            sys.exit(1)

    except Exception as e:
        logger.error(f"❌ 续期失败: {e}, " + traceback.format_exc())
        sys.exit(1)


@cli.command()
@click.pass_context
def list(ctx):
    """列出所有域名，包括自动续期状态"""
    logger = logging.getLogger(__name__)
    acme_manager = ctx.obj['acme']
    db_manager = ctx.obj['db']

    try:
        certificates = acme_manager.list_certificates()

        if not certificates:
            logger.info("没有找到证书")
            return

        # 获取域名的自动续期状态
        domains_info = {d['domain']: d for d in db_manager.list_domains()}

        logger.info("-" * 100)
        logger.info(f"{'DOMAIN ID':<10} {'DOMAIN':<25} {'STATUS':<8} {'EXPIRES AT':<15} {'DAYS':<6} {'RENEW':<7} {'VERIFY':<8}")
        logger.info("-" * 100)

        for cert in certificates:
            domain = cert['domain']
            status = cert['status']
            expires_at = cert['expires_at'][:10]  # 只显示日期部分
            days_left = cert['days_left']

            # 检查域名验证状态
            verify_status = "✅" if cert.get('status') == 'active' else "❌"

            # 获取自动续期状态和域名 ID
            domain_info = domains_info.get(domain, {})
            domain_id = domain_info.get('id', 'N/A')
            auto_renew_status = "✅" if domain_info.get('auto_renew', 1) else "❌"

            logger.info(f"{domain_id:<10} {domain:<25} {status:<8} {expires_at:<15} {days_left:<6} {auto_renew_status:<7} {verify_status:<8}")

        logger.info("-" * 100)

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
@click.argument('domain')
@click.option('--force', '-f', is_flag=True, help='强制删除，不需要确认')
@click.pass_context
def delete(ctx, domain, force):
    """删除域名及其所有相关记录和证书文件"""
    logger = logging.getLogger(__name__)
    db_manager = ctx.obj['db']
    config_manager = ctx.obj['config']

    try:
        # 检查域名是否存在
        domain_info = db_manager.get_domain(domain)
        if not domain_info:
            logger.error(f"❌ 未找到域名: {domain}")
            sys.exit(1)

        domain_id = domain_info['id']

        logger.info(f"准备删除域名及其所有相关数据:")
        logger.info(f"  域名: {domain}")
        logger.info(f"  域名 ID: {domain_id}")
        logger.info("")
        logger.info("将删除以下数据:")
        logger.info("  1. acme_rate_limits 表中的记录")
        logger.info("  2. certificate_requests 表中的记录")
        logger.info("  3. deployments 表中的记录")
        logger.info("  4. certificates 表中的记录")
        logger.info("  5. domains 表中的记录")
        logger.info("  6. certs/ 目录下的域名证书文件")

        # 如果没有 --force 标志，需要用户确认
        if not force:
            confirm = click.confirm("确认删除此域名及其所有相关数据吗?", default=False)
            if not confirm:
                logger.info("❌ 删除已取消")
                return

        # 删除证书文件
        cert_dir = os.path.join(config_manager.expand_path('certs'), domain)
        if os.path.exists(cert_dir):
            try:
                import shutil
                shutil.rmtree(cert_dir)
                logger.info(f"✅ 已删除证书文件目录: {cert_dir}")
            except Exception as e:
                logger.warning(f"⚠️  删除证书文件目录失败: {e}")
        else:
            logger.info(f"ℹ️  证书文件目录不存在: {cert_dir}")

        # 执行数据库删除
        success = db_manager.delete_domain_all_records(domain)

        if success:
            logger.info(f"✅ 域名删除成功!")
            logger.info(f"  已删除域名: {domain}")
            logger.info(f"  已清理所有相关数据库记录")
            logger.info(f"  已删除所有证书文件")
        else:
            logger.error(f"❌ 域名删除失败: 未找到域名 {domain}")
            sys.exit(1)

    except Exception as e:
        logger.error(f"❌ 删除域名失败: {e}")
        logger.exception("删除域名异常详情:")
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


@cli.command()
@click.argument('domain', required=False)
@click.option('--exec', 'exec_flag', is_flag=True, help='执行自动续期')
@click.option('--on', 'enable_flag', is_flag=True, help='启用域名的自动续期')
@click.option('--off', 'disable_flag', is_flag=True, help='禁用域名的自动续期')
@click.pass_context
def auto_renew(ctx, domain, exec_flag, enable_flag, disable_flag):
    """自动续期管理

    使用方式：

    1. 显示帮助信息：
       python cert_manager.py auto-renew

    2. 执行自动续期（为所有已开启的域名续期）：
       python cert_manager.py auto-renew --exec

    3. 启用域名的自动续期：
       python cert_manager.py auto-renew <domain> --on

    4. 禁用域名的自动续期：
       python cert_manager.py auto-renew <domain> --off
    """
    logger = logging.getLogger(__name__)
    acme_manager = ctx.obj['acme']
    db_manager = ctx.obj['db']

    try:
        # 如果没有任何标志和域名，显示帮助信息
        if not exec_flag and not enable_flag and not disable_flag and not domain:
            logger.info("自动续期管理")
            logger.info("-" * 60)
            logger.info("使用方式：")
            logger.info("")
            logger.info("1. 显示帮助信息：")
            logger.info("   python cert_manager.py auto-renew")
            logger.info("")
            logger.info("2. 执行自动续期（为所有已开启的域名续期）：")
            logger.info("   python cert_manager.py auto-renew --exec")
            logger.info("")
            logger.info("3. 启用域名的自动续期：")
            logger.info("   python cert_manager.py auto-renew <domain> --on")
            logger.info("")
            logger.info("4. 禁用域名的自动续期：")
            logger.info("   python cert_manager.py auto-renew <domain> --off")
            logger.info("-" * 60)
            return

        # 执行自动续期
        if exec_flag:
            if domain or enable_flag or disable_flag:
                logger.error("❌ --exec 标志不能与其他参数一起使用")
                sys.exit(1)

            logger.info("检查需要续期的证书...")
            results = acme_manager.auto_renew_all()

            if not results:
                logger.info("✅ 没有需要续期的证书")
                return

            success_count = sum(1 for success in results.values() if success)
            total_count = len(results)

            logger.info(f"续期结果: {success_count}/{total_count} 成功")

            for domain_name, success in results.items():
                status = "✅" if success else "❌"
                logger.info(f"  {status} {domain_name}")

            if success_count < total_count:
                sys.exit(1)

        # 管理单个域名的自动续期状态
        elif domain:
            domain_info = db_manager.get_domain(domain)
            if not domain_info:
                logger.error(f"❌ 未找到域名: {domain}")
                sys.exit(1)

            if enable_flag and disable_flag:
                logger.error("❌ 不能同时使用 --on 和 --off 标志")
                sys.exit(1)

            if not enable_flag and not disable_flag:
                logger.error(f"❌ 请指定 --on 或 --off 标志来管理域名 {domain} 的自动续期")
                sys.exit(1)

            if enable_flag:
                success = db_manager.update_domain_auto_renew(domain, True)
                if success:
                    logger.info(f"✅ 已启用域名 {domain} 的自动续期")
                else:
                    logger.error(f"❌ 启用自动续期失败: {domain}")
                    sys.exit(1)
            elif disable_flag:
                success = db_manager.update_domain_auto_renew(domain, False)
                if success:
                    logger.info(f"✅ 已禁用域名 {domain} 的自动续期")
                else:
                    logger.error(f"❌ 禁用自动续期失败: {domain}")
                    sys.exit(1)
        else:
            logger.error("❌ 无效的命令组合")
            sys.exit(1)

    except Exception as e:
        logger.error(f"❌ 操作失败: {e}, " + traceback.format_exc())
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
