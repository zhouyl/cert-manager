#!/bin/bash

# 证书自动续期脚本
# 用于 crontab 定时执行

# 脚本目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Python 解释器路径
PYTHON_PATH="python"

# 证书管理器脚本路径
CERT_MANAGER="$PROJECT_DIR/cert_manager.py"

# 日志文件
LOG_FILE="$PROJECT_DIR/logs/auto_renew.log"

# 确保日志目录存在
mkdir -p "$(dirname "$LOG_FILE")"

# 记录开始时间
echo "$(date '+%Y-%m-%d %H:%M:%S') - 开始自动续期检查" >> "$LOG_FILE"

# 切换到项目目录
cd "$PROJECT_DIR"

# 执行自动续期
if "$PYTHON_PATH" "$CERT_MANAGER" renew >> "$LOG_FILE" 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - 自动续期检查完成" >> "$LOG_FILE"
    exit 0
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') - 自动续期检查失败" >> "$LOG_FILE"
    exit 1
fi
