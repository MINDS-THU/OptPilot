import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns

# ==========================================
# 1. 顶会级学术视觉配置 (Publication-Ready Aesthetics)
# ==========================================
sns.set_theme(style="whitegrid", context="paper")
plt.rcParams.update({
    "font.family": "serif",        # 学术论文常用 serif 字体
    "font.size": 14,
    "axes.labelsize": 16,
    "axes.titlesize": 18,
    "legend.fontsize": 14,
    "xtick.labelsize": 12,
    "ytick.labelsize": 12,
    "lines.linewidth": 3.0,        # 线条加粗，打印出来更清晰
    "figure.dpi": 300              # 300 DPI 保证导出 PDF/PNG 极度清晰
})

# 框架视觉映射 (控制颜色和线型，让 Ours 最显眼)
FRAMEWORK_STYLE = {
    "devs_fast_plan": {
        "label": "Ours (DEVS-Gen)",  
        "color": "#E63946",          # 醒目的猩红色
        "linestyle": "-"             # 粗实线
    },
    
    # --- OpenHands 家族 ---
    "openhands": {
        "label": "OpenHands",        
        "color": "#1D3557",          # 沉稳的海军蓝 (深色)
        "linestyle": "--"            # 传统虚线
    },
    "openhands_fast": {
        "label": "OpenHands-Lite",   
        "color": "#457B9D",          # 钢蓝色 (浅色)
        "linestyle": ":"             # 细密点线
    },
    
    # --- SWE-Agent 家族 ---
    "swe_agent": {
        "label": "SWE-Agent",        
        "color": "#2A9D8F",          # 森林青绿 (深色)
        "linestyle": "-."            # 点划线
    },
    "swe_agent_fast": {
        "label": "SWE-Agent-Lite",   
        "color": "#8AB17D",          # 莫兰迪浅绿 (浅色)
        "linestyle": (0, (3, 1, 1, 1, 1, 1)) # 自定义双点划线 (-..-..)
    }
}

# ==========================================
# 2. 核心画图逻辑 (已反转横纵坐标)
# ==========================================
def plot_cdf_for_model(df, model_name, max_time_cutoff=2000):
    """
    为单个模型绘制 Time-to-Success 累积图 (X轴: 成功率, Y轴: 时间)
    """
    plt.figure(figsize=(8, 4))
    
    # 筛选该模型的数据
    if model_name is not None:
        df_model = df[df['model'] == model_name]
    else:
        model_name = "all"
        df_model = df
        
    frameworks = df_model['framework'].unique()
    
    # 获取全局最大 Y 轴时间范围 (原来是 X 轴)
    max_time_in_data = df_model[df_model['is_success'] == True]['time_val'].max()
    y_max = min(max_time_in_data * 1.1, max_time_cutoff) # 留 10% 的上方空白
    
    for fw in frameworks:
        df_fw = df_model[df_model['framework'] == fw]
        total_runs = len(df_fw)
        if total_runs == 0:
            continue
            
        # 1. 提取成功的局，并按耗时从小到大排序
        success_times = sorted(df_fw[df_fw['is_success'] == True]['time_val'].tolist())
        
        # 2. 构造阶梯图 (Step Plot) 的 X 和 Y
        # X 轴现在是成功率 (sr_vals)，Y 轴现在是时间 (time_vals)
        sr_vals = [0]
        time_vals = [0]
        
        for i, t in enumerate(success_times):
            # 每多成功一个，成功率增加 1/total_runs
            sr_vals.append((i + 1) / total_runs)
            time_vals.append(t)
            
        # 为了让线平滑地向上延伸到图的顶部边缘（表示时间继续流逝但成功率不再增加）
        if sr_vals:
            sr_vals.append(sr_vals[-1])
            time_vals.append(y_max)
            
        # 3. 绘制线条
        style = FRAMEWORK_STYLE.get(fw, {"label": fw, "color": "gray", "linestyle": "-"})
        
        # 【关键修改】：横纵反转后，必须用 where='pre' 来保持严谨的“先消耗时间，后增加成功率”的物理意义
        plt.step(sr_vals, time_vals, where='pre', 
                 label=style["label"], 
                 color=style["color"], 
                 linestyle=style["linestyle"],
                 alpha=0.9)

    # ==========================================
    # 3. 坐标轴与图例修饰 (互换与重置)
    # ==========================================
    # plt.title(f"Time-to-Success (TTS) Distribution", pad=15)
    plt.xlabel("Target Success Rate")
    plt.ylabel("Time-to-Success (seconds)")
    
    # X轴范围固定为 0 到 100% (即 0.0 到 1.05)，Y轴范围为 0 到 y_max
    plt.xlim(0, 1.05)
    plt.ylim(0, y_max)
    
    # 【关键修改】：将 X 轴转换为百分比显示
    import matplotlib.ticker as mtick
    plt.gca().xaxis.set_major_formatter(mtick.PercentFormatter(1.0))
    
    # 图例放在右下角 (反转后，右下角通常会是一片空白，因为曲线都往左上角或右上角飙了，所以这个位置极其完美)
    plt.legend(loc="upper left", frameon=True, shadow=False, edgecolor='gray')
    
    # 紧凑布局并保存
    plt.tight_layout()
    
    # 清洗文件名中的特殊字符
    safe_model_name = model_name.replace("/", "_").replace(".", "_")
    output_filename = f"CDF_{safe_model_name}.pdf"
    plt.savefig(output_filename, format='pdf', bbox_inches='tight')
    print(f"Saved highly-polished plot: {output_filename}")
    plt.close()

if __name__ == "__main__":
    # 读取第一步生成的数据
    CSV_FILE = "cdf_plot_data.csv"
    try:
        df_cdf = pd.read_csv(CSV_FILE)
    except FileNotFoundError:
        print(f"Error: 找不到 {CSV_FILE}。请先运行 compute_advanced_metrics.py！")
        exit()
        
    models = df_cdf['model'].unique()
    
    # 遍历每个模型单独出图
    for model in models:
        plot_cdf_for_model(df_cdf, model, max_time_cutoff=1800)
    
    # 增加的宏观 All 视角出图
    plot_cdf_for_model(df_cdf, None, max_time_cutoff=1800)
        
    print("All plots generated successfully!")