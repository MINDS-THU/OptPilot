#!/bin/zsh
set -eu
project_dir=${0:A:h}
app_bundle=${OPTPILOT_APP:-}
if [[ -z "$app_bundle" ]]; then
  for app_bundle in "/Applications/OptPilot.app" "$HOME/Applications/OptPilot.app"; do
    [[ -x "$app_bundle/Contents/MacOS/OptPilot" ]] && break
  done
fi
resources="$app_bundle/Contents/Resources"
if [[ ! -x "$resources/python-runtime/bin/python3" || ! -d "$resources/app.asar.unpacked/dist/core/optpilot" ]]; then
  print -u2 "请将 OPTPILOT_APP 指向已安装的 OptPilot.app，或在已安装 OptPilot Core 的 Python 环境中运行 demo.py。"
  exit 1
fi
exec "$resources/python-runtime/bin/python3" -IB -c '
import runpy, sys
from pathlib import Path
core, project = sys.argv[1:3]
script = str(Path(project) / "demo.py")
sys.path[:0] = [core, project]
sys.argv = [script, *sys.argv[3:]]
runpy.run_path(script, run_name="__main__")
' "$resources/app.asar.unpacked/dist/core" "$project_dir" "$@"
