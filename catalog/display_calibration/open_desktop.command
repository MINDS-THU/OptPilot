#!/bin/zsh
set -eu

project_dir=${0:A:h}
app_bundle=${OPTPILOT_APP:-}
if [[ -z "$app_bundle" ]]; then
  for app_bundle in "/Applications/OptPilot.app" "$HOME/Applications/OptPilot.app"; do
    [[ -x "$app_bundle/Contents/MacOS/OptPilot" ]] && break
  done
fi
if [[ ! -x "$app_bundle/Contents/MacOS/OptPilot" ]]; then
  print -u2 "未找到 OptPilot 桌面版。请将 OPTPILOT_APP 设为已安装的 OptPilot.app 路径。"
  exit 1
fi
if [[ ! -f "$project_dir/演示结果.json" ]]; then
  print -u2 "此目录还没有演示结果。请先按 README.md 的步骤运行一次演示。"
  exit 1
fi

print "打开示例项目：$project_dir"
print "首次打开时可点「Open project」选择此目录；双击关系图中的评测查看评分。"
/usr/bin/open -n -a "$app_bundle" \
  --env "OPTPILOT_DATA_DIR=$project_dir/.demo/desktop" \
  --env "OPTPILOT_RESOURCE_STORE=$project_dir/.demo/registry"
