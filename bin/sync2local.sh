#!/bin/bash

# 获取当前脚本的根目录
root=$(cd "$(dirname "$0")" || exit; cd ../; pwd)
cd "$root" || exit

remote_dir=/data/www/cert-manager
server=root@srv-02

# 同步文件
rsync -azv -e "ssh -p 22" "$server:$remote_dir/" "$root/"
