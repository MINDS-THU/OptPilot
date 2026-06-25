import sys
import subprocess
import os

# --- 自动生成配置 ---
# 目标仿真模块 (e.g. devs_project.run_abp_d1)
SIM_MODULE = "${SIM_MODULE}"
# 中间日志文件
RAW_LOG_FILE = "raw_simulation_output.log"

def main():
    """
    Auto-generated Entry Point for DEVS Evaluation
    """
    # 1. 传递参数
    forward_args = sys.argv[1:]

    # 2. 运行仿真
    print(f"[Entry] Running Simulation: {SIM_MODULE}...", file=sys.stderr)
    try:
        sim_process = subprocess.run(
            [sys.executable, "-m", SIM_MODULE] + forward_args,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=False
        )
        
        if sim_process.returncode != 0:
            print(f"[Entry] Simulation failed (RC={sim_process.returncode})", file=sys.stderr)
            sys.exit(sim_process.returncode)

    except Exception as e:
        print(f"[Entry] Error launching simulation: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()