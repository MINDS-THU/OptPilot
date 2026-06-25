
import pandas as pd
import numpy as np
import os
import json
import glob

# ==========================================
# 配置区 (Configuration)
# ==========================================

# 1. 模型名称对齐字典 (将两边乱七八糟的名字统一洗成标准名)
MODEL_NAME_MAPPING = {
    # --- SWE/OpenHands CSV 中的命名 ---
    "gpt-5.2": "GPT-5.2",
    "glm-4.7": "GLM-4.7",
    "glm4.7flash": "GLM-4.7-Flash",
    "llama4-17b": "Llama-4-17B",
    "qwen3-coder-30b": "Qwen3-30B",
    
    # --- DEVS JSON 中的命名 ---
    "openrouter/openai/gpt-5.2": "GPT-5.2",
    "openrouter/z-ai/glm-4.7": "GLM-4.7",
    "openrouter/meta-llama/llama-4-scout": "Llama-4-17B", 
    "openrouter/qwen/qwen3-coder-30b-a3b-instruct": "Qwen3-30B"
}

# 2. 过滤目标 (现在这里请使用清洗后的【标准名】)
TARGET_FRAMEWORKS = [
    "devs_fast_plan", 
    "openhands_fast", 
    "openhands", 
    "swe_agent", 
    "swe_agent_fast"
]
TARGET_MODELS = [
    "GPT-5.2",
    "GLM-4.7",
    "Qwen3-30B",
    "Llama-4-17B"
]
TARGET_BENCHMARKS = ["ABP", "oft", "OTrain", "IOBS", "SA", "SEIRD", "barbershop"]

# 3. 惩罚参数配置
FAILED_PENALTY_TIME = 1800  
FAILED_PENALTY_TOKEN = None 

# SWE 超时判定阈值
SWE_TIMEOUT_THRESHOLD = 1800

# ==========================================
# 第一块：读取 SWE 数据并标准化
# ==========================================
def load_swe_data(csv_file_path):
    print(f"Loading SWE data from: {csv_file_path}")
    df = pd.read_csv(csv_file_path)
    benchmark_col = 'benchmark' if 'benchmark' in df.columns else 'task_id'
    
    df['is_success'] = (df['time_effective_sec'] < SWE_TIMEOUT_THRESHOLD) & (df['failed_forced'] == 0)
    
    std_df = pd.DataFrame({
        'framework': df['framework'],
        'model': df['model'],
        'benchmark': df[benchmark_col],
        'run_id': df['attempt_id'] if 'attempt_id' in df.columns else df.index,
        'is_success': df['is_success'],
        'time_val': df['time_effective_sec'],
        'tokens': df['token_effective'] if 'token_effective' in df.columns else 0,
        'bcs': df['BCS'] if 'BCS' in df.columns else 0.0,
        'oss': df['OSS'] if 'OSS' in df.columns else 0.0
    })
    
    # 核心：执行映射，洗库
    std_df['model'] = std_df['model'].map(MODEL_NAME_MAPPING).fillna(std_df['model'])
    return std_df

# ==========================================
# 第二块：读取 DEVS 数据并标准化
# ==========================================
def load_devs_data(base_dir, cutoff_timestamp="20260510_232400"):
    print(f"Loading DEVS data from: {base_dir}")
    search_pattern = os.path.join(base_dir, "run_*", "**", "run_meta.json")
    run_meta_files = glob.glob(search_pattern, recursive=True)

    records = []
    for meta_file in run_meta_files:
        path_parts = meta_file.split(os.sep)
        run_folder = next((part for part in path_parts if part.startswith("run_")), None)
        if not run_folder or run_folder.replace("run_", "") > cutoff_timestamp: 
            continue

        try:
            with open(meta_file, 'r', encoding='utf-8') as f:
                run_meta = json.load(f)
        except Exception:
            continue

        exp_info = run_meta.get("experiment", {})
        framework = exp_info.get("framework", "unknown")
        model = exp_info.get("model_id", "unknown")
        benchmark = exp_info.get("benchmark", "unknown")
        if benchmark in ["ComplexSup2"]: continue

        gen_success = (run_meta.get("generation", {}).get("status", "") == "success")
        bcs = run_meta.get("evaluation", {}).get("total_score", 0.0)

        tokens = 0
        token_usage = run_meta.get("totals", {}).get("token_usage", {})
        for _, usage in token_usage.items():
            tokens += usage.get("total", 0)
        time_val = run_meta.get("totals", {}).get("total_duration_sec", 0.0)

        oss = 0.0
        if gen_success:
            summary_path = os.path.join(os.path.dirname(meta_file), "eval_results", "summary.json")
            if os.path.exists(summary_path):
                try:
                    with open(summary_path, 'r', encoding='utf-8') as sf:
                        results = json.load(sf).get("results", [])
                    if isinstance(results, dict): results = list(results.values())
                    if len(results) > 0:
                        all_v = [1 if (res.get("success", False) and res.get("valid_json_output", False)) else 0 for res in results]
                        oss = sum(all_v) / len(all_v)
                except Exception:
                    pass

        records.append({
            'framework': framework,
            'model': model,
            'benchmark': benchmark,
            'run_id': run_folder,
            'is_success': gen_success,
            'time_val': time_val,
            'tokens': tokens,
            'bcs': bcs,
            'oss': oss
        })
        
    std_df = pd.DataFrame(records)
    if not std_df.empty:
        # 核心：执行映射，洗库
        std_df['model'] = std_df['model'].map(MODEL_NAME_MAPPING).fillna(std_df['model'])
    return std_df

# ==========================================
# 第三块：过滤并计算核心高级指标
# (保持不变，直接接管清洗后的 df_all 即可)
# ==========================================
def process_and_calculate_metrics(df):
    print("Filtering data...")
    if TARGET_FRAMEWORKS:
        df = df[df['framework'].isin(TARGET_FRAMEWORKS)]
    if TARGET_MODELS:
        df = df[df['model'].isin(TARGET_MODELS)]
    if TARGET_BENCHMARKS:
        df = df[df['benchmark'].isin(TARGET_BENCHMARKS)]

    if FAILED_PENALTY_TIME is not None:
        df.loc[~df['is_success'], 'time_val'] = FAILED_PENALTY_TIME
    if FAILED_PENALTY_TOKEN is not None:
        df.loc[~df['is_success'], 'tokens'] = FAILED_PENALTY_TOKEN
        
    df_for_plot = df[['model', 'framework', 'is_success', 'time_val']].copy()
    df_for_plot.to_csv('cdf_plot_data.csv', index=False)
    print("已导出画图专用数据: cdf_plot_data.csv")

    print("Calculating advanced metrics...")
    results = []
    
    for model, m_group in df.groupby('model'):
        frameworks_in_model = m_group['framework'].unique()
        success_benchmarks_per_fw = []
        
        for fw in frameworks_in_model:
            fw_success_bms = m_group[(m_group['framework'] == fw) & (m_group['is_success'])]['benchmark'].unique()
            success_benchmarks_per_fw.append(set(fw_success_bms))
            
        shared_benchmarks = set.intersection(*success_benchmarks_per_fw) if success_benchmarks_per_fw else set()
        
        for fw in frameworks_in_model:
            group = m_group[m_group['framework'] == fw]
            total_runs = len(group)
            success_runs = group['is_success'].sum()
            success_rate = success_runs / total_runs if total_runs > 0 else 0
            
            bcs_mean = group['bcs'].mean()
            oss_mean = group['oss'].mean()
            
            total_time_spent = group['time_val'].sum()
            total_tokens_spent = group['tokens'].sum()
            
            ets_time = total_time_spent / success_runs if success_runs > 0 else float('inf')
            ets_tokens = total_tokens_spent / success_runs if success_runs > 0 else float('inf')
            
            shared_group = group[(group['is_success']) & (group['benchmark'].isin(shared_benchmarks))]
            shared_time_mean = shared_group['time_val'].mean() if not shared_group.empty else pd.NA
            shared_token_mean = shared_group['tokens'].mean() if not shared_group.empty else pd.NA
            
            success_group = group[group['is_success']]
            st_time = success_group['time_val'].mean() if not success_group.empty else pd.NA
            
            results.append({
                'model': model,
                'framework': fw,
                'total_runs': total_runs,
                'success_rate': success_rate,
                'bcs_mean': bcs_mean,
                'oss_mean': oss_mean,
                'ETS_time': ets_time,
                'ETS_tokens': ets_tokens,
                'shared_success_time': shared_time_mean,
                'shared_success_tokens': shared_token_mean,
                'normal_success_time': st_time,
                'shared_benchmarks_count': len(shared_benchmarks)
            })
            
    res_df = pd.DataFrame(results).sort_values(by=['model', 'framework'])
    
    print("\n" + "="*140)
    print(f"{'Model':<15} | {'Framework':<18} | {'SR':<6} | {'ETS Time':<10} | {'Shared ST':<10} | {'Norm ST':<10} | {'ETS Tokens':<12}")
    print("-" * 140)
    
    for _, row in res_df.iterrows():
        sr = f"{row['success_rate']:.1%}"
        ets_time_str = f"{row['ETS_time']:.1f}" if row['ETS_time'] != float('inf') else "INF"
        shared_st_str = f"{row['shared_success_time']:.1f}" if pd.notna(row['shared_success_time']) else "N/A"
        norm_st_str = f"{row['normal_success_time']:.1f}" if pd.notna(row['normal_success_time']) else "N/A"
        ets_tok_str = f"{row['ETS_tokens']:,.0f}" if row['ETS_tokens'] != float('inf') else "INF"
        
        print(f"{row['model']:<15} | {row['framework']:<18} | {sr:<6} | {ets_time_str:<10} | {shared_st_str:<10} | {norm_st_str:<10} | {ets_tok_str:<12}")
        
    return res_df

if __name__ == "__main__":
    SWE_CSV_PATH = "/home/czy/ML/DEVS/smolagents/HAMLET/devs_tester2/canonical_latest3_filtered_manifest.csv"
    DEVS_BASE_DIR = "/home/czy/ML/DEVS/smolagents/HAMLET/HAMLET_core/generated"
    
    df_swe = load_swe_data(SWE_CSV_PATH) if os.path.exists(SWE_CSV_PATH) else pd.DataFrame()
    df_devs = load_devs_data(DEVS_BASE_DIR) if os.path.exists(DEVS_BASE_DIR) else pd.DataFrame()
    
    df_all = pd.concat([df_swe, df_devs], ignore_index=True)
    df_all.to_csv('advanced_metrics_raw.csv', index=False)
    
    if not df_all.empty:
        final_stats = process_and_calculate_metrics(df_all)
        final_stats.to_csv('advanced_metrics_summary.csv', index=False)