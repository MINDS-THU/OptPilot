import os
import json
import glob
import pandas as pd

def main():
    # 基础路径配置
    base_dir = "/home/czy/ML/DEVS/smolagents/HAMLET/HAMLET_core/generated"
    cutoff_timestamp = "20260510_232400"  # 截止时间戳
    
    # 输出文件路径
    output_benchmark_csv = os.path.join(base_dir, "metrics_by_benchmark.csv")
    output_model_csv = os.path.join(base_dir, "metrics_by_framework_model.csv")

    # 递归查找所有的 run_meta.json
    search_pattern = os.path.join(base_dir, "run_*", "**", "run_meta.json")
    run_meta_files = glob.glob(search_pattern, recursive=True)

    records = []

    for meta_file in run_meta_files:
        path_parts = meta_file.split(os.sep)
        run_folder = next((part for part in path_parts if part.startswith("run_")), None)
        
        if not run_folder:
            continue
            
        # 过滤超过截止时间戳的运行
        folder_time = run_folder.replace("run_", "")
        if folder_time > cutoff_timestamp:
            continue

        try:
            with open(meta_file, 'r', encoding='utf-8') as f:
                run_meta = json.load(f)
        except Exception as e:
            print(f"无法读取 {meta_file}: {e}")
            continue

        # 提取实验配置
        exp_info = run_meta.get("experiment", {})
        framework = exp_info.get("framework", "unknown")
        model_id = exp_info.get("model_id", "unknown")
        benchmark = exp_info.get("benchmark", "unknown")
        if benchmark in ["ComplexSup2"]:
            continue

        # 提取生成和评估状态
        gen_status = run_meta.get("generation", {}).get("status", "")
        gen_success = (gen_status == "success")
        
        # 提取 BCS (最后 score)
        bcs = run_meta.get("evaluation", {}).get("total_score", 0.0)

        # 提取 Token 和 Time
        tokens = 0
        time_val = 0.0
        if gen_success:
            token_usage = run_meta.get("totals", {}).get("token_usage", {})
            for model, usage in token_usage.items():
                tokens += usage.get("total", 0)
            
            time_val = run_meta.get("totals", {}).get("total_duration_sec", 0.0)

        # 提取 OSS 指标
        oss = 0
        if gen_success:
            summary_path = os.path.join(os.path.dirname(meta_file), "eval_results", "summary.json")
            if os.path.exists(summary_path):
                try:
                    with open(summary_path, 'r', encoding='utf-8') as sf:
                        summary = json.load(sf)
                        
                    results = summary.get("results", [])
                    if isinstance(results, dict):
                        results = list(results.values())
                    
                    if len(results) > 0:
                        all_v = []
                        for res in results:
                            succ = res.get("success", False)
                            valid_json = res.get("valid_json_output", False)
                            log_format = res.get("details", {}).get("type_averages", {}).get("log_format_correctness", 0)
                            
                            if not (succ and valid_json):
                                all_v.append(0)
                            else:
                                all_v.append(1)
                        oss = sum(all_v) / len(all_v)
                except Exception as e:
                    pass # 静默处理错误，OSS默认为0

        records.append({
            "run_folder": run_folder,
            "framework": framework,
            "model_id": model_id,
            "benchmark": benchmark,
            "gen_success": gen_success,
            "BCS": bcs,
            "OSS": oss,
            "tokens": tokens,
            "time_val": time_val
        })

    if not records:
        print("未找到符合条件的实验记录。")
        return

    df = pd.DataFrame(records)

    # 核心计算逻辑：通用的分组计算函数，包含均值和方差
    def compute_metrics(group):
        success_runs = group[group["gen_success"] == True]
        
        # effective: 如果没有正常生成，使用惩罚值
        def get_eff_token(row):
            return row["tokens"] if row["gen_success"] else 5000000
            
        def get_eff_time(row):
            return row["time_val"] if row["gen_success"] else 4000
            
        eff_tokens = group.apply(get_eff_token, axis=1)
        eff_times = group.apply(get_eff_time, axis=1)

        # 辅助函数：计算均值和样本方差。如果只有1条记录，方差记为 0.0
        def calc_mean_var(series):
            if series.empty:
                return pd.NA, pd.NA
            elif len(series) == 1:
                return series.mean(), 0.0
            else:
                return series.mean(), series.var()

        oss_mean, oss_var = calc_mean_var(group["OSS"])
        bcs_mean, bcs_var = calc_mean_var(group["BCS"])
        
        obs_token_mean, obs_token_var = calc_mean_var(success_runs["tokens"])
        obs_time_mean, obs_time_var = calc_mean_var(success_runs["time_val"])
        
        eff_token_mean, eff_token_var = calc_mean_var(eff_tokens)
        eff_time_mean, eff_time_var = calc_mean_var(eff_times)
        
        return pd.Series({
            "total_runs": len(group),
            "success_runs": len(success_runs),
            "OSS_mean": oss_mean,
            "OSS_var": oss_var,
            "BCS_mean": bcs_mean,
            "BCS_var": bcs_var,
            "observed_token_mean": obs_token_mean,
            "observed_token_var": obs_token_var,
            "effective_token_mean": eff_token_mean,
            "effective_token_var": eff_token_var,
            "observed_time_mean": obs_time_mean,
            "observed_time_var": obs_time_var,
            "effective_time_mean": eff_time_mean,
            "effective_time_var": eff_time_var
        })

    # ==========================
    # 1. 按 framework, model_id, benchmark 细粒度聚合
    # ==========================
    summary_benchmark_df = df.groupby(["framework", "model_id", "benchmark"]).apply(compute_metrics).reset_index()
    summary_benchmark_df.to_csv(output_benchmark_csv, index=False)

    # ==========================
    # 2. 按 framework, model_id 粗粒度（宏观）聚合
    # ==========================
    summary_model_df = df.groupby(["framework", "model_id"]).apply(compute_metrics).reset_index()
    summary_model_df.to_csv(output_model_csv, index=False)

    # 打印运行结果汇总信息
    print(f"\n统计完成！共分析了 {len(df)} 个有效实验运行 (Run)。")
    print("-" * 50)
    print(f"已生成两份数据报告 (包含均值与方差) 保存在 {base_dir} 目录下：")
    print(f"1. 细粒度结果 (带benchmark): metrics_by_benchmark.csv")
    print(f"2. 宏观结果 (仅framework+model): metrics_by_framework_model.csv")
    print("-" * 50)
    
    print("\n【按 Framework 和 Model 聚合的预览结果 (截取部分列)】:")
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 1000)
    # 只打印部分列避免控制台换行过于杂乱
    print(summary_model_df[["framework", "model_id", "total_runs", "success_runs", "OSS_mean", "OSS_var", "BCS_mean", "BCS_var"]].head(10))

if __name__ == "__main__":
    main()