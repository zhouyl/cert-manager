# 证书管理器

基于 acme.sh 的 Let's Encrypt 证书自动化管理工具，使用 Cloudflare DNS 验证。

## 功能特性

- 🔐 通配符证书申请 (支持中断恢复)
- ☁️ Cloudflare DNS 自动验证
- 🔄 自动续期和状态管理
- 🚀 多服务器部署

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 Cloudflare API Token

编辑 `config/config.yaml` (参考 `config/config.yaml.example`)：

```yaml
cloudflare:
  api_token: "your-cloudflare-api-token"

servers:
  - name: "nginx-server"
    host: "192.168.1.100"
    username: "root"
    identity_file: "~/.ssh/id_rsa"
    cert_directory: "/etc/nginx/certs"
    reload_command: "systemctl reload nginx"
    reload: false
    enabled: true
```

获取 API Token: https://dash.cloudflare.com/profile/api-tokens

## 使用方法

### 申请证书 (支持中断恢复)

```bash
python cert_manager.py issue example.com
```

### 查看证书状态

```bash
python cert_manager.py list
```

### 续期证书

```bash
python cert_manager.py renew example.com  # 指定域名
python cert_manager.py renew              # 所有即将过期的证书
```

### 部署证书

```bash
python cert_manager.py deploy example.com --all-server
python cert_manager.py deploy example.com -s nginx-server
python cert_manager.py deploy example.com -s nginx-server -r "systemctl reload nginx"
```

## 自动续期

```bash
# 可以配置定时任务 (每天执行一次)
0 0 * * * python cert_manager.py renew
```
