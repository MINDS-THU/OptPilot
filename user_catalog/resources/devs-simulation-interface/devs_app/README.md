
# DEVS Agent Application

这是一个基于HAMLET框架的DEVS（Discrete Event System Specification）模型构建和执行应用程序。该应用整合了example_app中的所有工具以及devs_tools中的DEVS专用工具，为用户提供了一个强大的DEVS建模、执行和分析环境。

## 功能特性

- **DEVS模型构建**：使用自然语言描述创建DEVS模型
- **DEVS模型执行**：在安全沙箱环境中执行DEVS模型
- **文件编辑**：支持本地工作目录的文件操作
- **知识库管理**：持久化存储和检索DEVS模型及相关资料
- **网络搜索**：集成OpenDeepSearch进行信息检索
- **可视化分析**：支持图像和结果的可视化分析

## 目录结构

```
devs_app/
├── __init__.py
├── run.py              # 应用主程序
├── working_dirs/       # 临时工作目录
├── persistent_storage/ # 持久化存储
└── index_dir/          # 知识库索引
```

## 安装与使用

### 1. 环境准备

确保已安装所有依赖项：

```bash
pip install -r ../../requirements.txt
```

设置必要的环境变量：

```bash
export OPENAI_API_KEY="your_openai_api_key"
export OPENAI_BASE_URL="your_openai_base_url"  # 可选
```

### 2. 运行应用

#### Gradio界面模式（推荐）

```bash
cd /path/to/HAMLET/devs_app
python run.py
```

#### 命令行模式

```bash
cd /path/to/HAMLET/devs_app
python run.py --mode cli
```

### 3. 高级用法

自定义模型：

```bash
python run.py --model_id "gpt-4o"
```

自定义工作目录：

```bash
python run.py --working_directory "/path/to/custom/working/dir"
```

## DEVS工具使用指南

### DEVSConstructSimple

使用自然语言描述创建DEVS模型：

```python
# 示例请求
"创建一个DEVS模型，名为'QueueSystem'，实现一个简单的队列系统，包含一个生成器、一个队列和一个处理器。"
```

工具会自动：
1. 生成符合要求的DEVS模型代码
2. 将代码保存到`devs_models/{model_name}.py`
3. 同步到知识库
4. 提供模型摘要

### DEVSExecute

在安全沙箱中执行DEVS模型：

```python
# 示例请求
"执行devs_models/QueueSystem.py模型，模拟时间为100秒。"
```

工具会：
1. 在受控环境中执行模型
2. 捕获执行日志和结果
3. 提取关键结果和性能指标
4. 返回执行摘要

## 示例工作流

1. **创建DEVS模型**
   ```
   请创建一个名为"ProducerConsumer"的DEVS模型，实现一个生产者-消费者系统。生产者以固定速率生成物品，消费者以随机速率消费物品，两者之间通过缓冲区连接。
   ```

2. **执行DEVS模型**
   ```
   执行devs_models/ProducerConsumer.py模型，设置模拟时间为200秒，并查看结果。
   ```

3. **分析和优化**
   ```
   基于执行结果，分析生产者-消费者系统的性能瓶颈，并提出优化建议。
   ```

## 注意事项

- 确保有足够的磁盘空间存储模型和日志
- DEVS模型执行可能消耗大量计算资源
- 长时间运行的模拟可能需要调整超时设置
- 所有DEVS模型代码都受到安全沙箱限制，只能导入指定的库

## 开发者说明

要扩展此应用的功能，可以：

1. 在`devs_tools`目录中添加新的DEVS工具
2. 在`run.py`中的`create_devs_agent`函数中注册新工具
3. 更新工具文档和示例

## 许可证

本应用遵循与HAMLET框架相同的许可证。
