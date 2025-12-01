#!/bin/bash

# 获取当前脚本的根目录
root=$(cd "$(dirname "$0")" || exit; cd ../; pwd)
cd "$root" || exit

# 定义服务器和远程目录
servers=("root@srv-02")

remote_dir=/data/www/cert-manager
pip_bin="\$HOME/miniconda3/bin/pip"

for entry in "${servers[@]}"; do
    # 分离 host 和 port
    host=$(echo "$entry" | cut -d: -f1)
    port=$(echo "$entry" | cut -s -d: -f2)

    # 如果未指定端口，默认 22
    port=${port:-22}

    echo -e "\n-------------------- publish to $host (port $port) --------------------\n"

    echo "rsync -azv -e 'ssh -p $port' --chown=root:root --include-from=$root/bin/include.txt --exclude-from=$root/bin/exclude.txt $root/ $host:$remote_dir"

    # 传输文件
    rsync -azv -e "ssh -p $port" \
        --chown=root:root \
        --include-from="$root/bin/include.txt" \
        --exclude-from="$root/bin/exclude.txt" \
        "$root/" "$host:$remote_dir"

    # 安装依赖
    ssh -p "$port" "$host" "$pip_bin install -r $remote_dir/requirements.txt"

    echo -e "\nrelease to $host done!"
done
