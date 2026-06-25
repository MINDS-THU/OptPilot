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
        # with open(RAW_LOG_FILE, 'wb') as f_log:
            # 这里的 cwd 默认为当前目录，假设 run.py 在根目录
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

    # # 直接把总结内容输出到 stdout
    # with open(RAW_LOG_FILE, 'rb') as f_read_log:
    #     # 直接把读到的 bytes 写入 stdout 的二进制流，不做任何转换
    #     sys.stdout.buffer.write(f_read_log.read())
        
    #     # 如果一定要强制刷新一下缓冲区，可以加一句：
    #     sys.stdout.flush()

    # # 3. 运行总结
    # print(f"[Entry] Running Summary: {SUMMARY_SCRIPT}...", file=sys.stderr)
    # try:
    #     if not os.path.exists(SUMMARY_SCRIPT):
    #          print(f"[Entry] Error: Summary script not found at {SUMMARY_SCRIPT}", file=sys.stderr)
    #          sys.exit(1)

    #     with open(RAW_LOG_FILE, 'rb') as f_read_log:
    #         sum_process = subprocess.run(
    #             [sys.executable, SUMMARY_SCRIPT],
    #             stdin=f_read_log,
    #             stdout=sys.stdout, # 最终结果输出到 stdout
    #             stderr=sys.stderr,
    #             check=False
    #         )
    #     sys.exit(sum_process.returncode)

    # except Exception as e:
    #     print(f"[Entry] Error launching summary: {e}", file=sys.stderr)
    #     sys.exit(1)

if __name__ == "__main__":
    main()