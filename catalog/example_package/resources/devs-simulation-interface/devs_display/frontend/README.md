
# xDEVS 模型可视化与管理工具 (xDEVS Visualizer & Manager)

这是一个基于 React 和 D3.js 的高级前端应用程序，旨在可视化、管理和编辑遵循 xDEVS.py 框架的 Python 仿真模型。

它不仅利用 LLM (Google Gemini / OpenAI) 对 Python 源代码进行静态分析以提取模型结构，还集成了后端 Agent 服务，允许用户通过自然语言对话来修改项目代码或生成新模型。

## 核心架构

系统分为两部分：
1.  **Frontend (本仓库)**: React SPA，负责图形渲染、交互、与 LLM 直接通信（用于解析可视化结构）以及与后端 API 通信。
2.  **Backend (Python Server)**: 运行在 `localhost:8000` 的服务，负责文件持久化存储、项目列表管理以及执行复杂的 Agent 代码修改任务。

## 功能特性

### 1. 强大的可视化引擎
*   **物理导向布局**: 使用 D3.js 力导向图展示复杂的 Coupled Model 结构。
*   **黑盒/白盒切换**: 支持逐级展开子模型，查看内部连线细节。
*   **端口级连接**: 清晰展示 Input/Output 端口及其耦合关系。
*   **导出图片**: 支持将当前视图导出为高清 PNG，方便制作 PPT。

### 2. 多模式项目管理
*   **远程项目 (Remote)**: 连接后端 API，自动拉取服务器上的项目列表。
*   **本地/离线模式 (Local)**: 支持直接拖拽上传本地文件夹。即使没有后端，也能通过前端缓存和 LLM API 进行可视化解析。
*   **自动同步**: 上传的文件夹会自动尝试同步到后端存储。

### 3. AI Agent 助手
*   **自然语言交互**: 内置聊天窗口，支持与 AI Agent 对话。
*   **代码生成与修改**: 可以要求 Agent "修改 Department 里的医生数量" 或 "创建一个新的交通仿真模型"，Agent 会直接修改后端文件。
*   **多模型支持**: 支持切换 **Google Gemini** 或 **OpenAI (GPT-4o)** 作为底层智能引擎。

## 快速开始

### 前置要求
*   Node.js (v16+) 
    - 关于这个, 你可以直接参考软工课程文档, 我个人感觉非常好用且简洁: https://lab.cs.tsinghua.edu.cn/software-engineering/handout/react/environment/
*   Python 3.9+ (用于后端)
*   有效的 Google Gemini API Key 或 OpenAI API Key。

### 1. 启动后端服务
*(请确保您已获取后端代码库并安装依赖)*
后端服务应运行在 `http://localhost:8000` 并提供 REST API。

### 2. 启动前端
在本项目根目录下：

```bash
# 安装依赖
npm install

# 启动开发服务器
npm start
# 或
npm run dev
```

浏览器访问 `http://localhost:5173` (或命令行提示的端口)。

## 使用指南

### 配置 API
1.  打开左侧侧边栏。
2.  在 **API Configuration** 区域选择 AI 提供商 (Gemini 或 OpenAI)。
3.  输入您的 API Key。*(注意：解析代码结构依赖此 Key)*。

### 加载项目
有三种方式加载项目：
1.  **选择远程项目**: 如果后端已启动，在 "Projects" 下拉框中直接选择服务器上的项目。
2.  **上传文件夹**: 点击 "Upload Folder" 区域，选择包含 `system_model_info.json` 和 `.py` 文件的本地文件夹。系统会自动解析并尝试同步到后端。
3.  **本地缓存**: 系统会自动缓存加载过的项目，即使后端离线也可以重新查看。

### 可视化交互
1.  加载项目后，在 "Root Model" 下拉框选择顶层模型（如 `HospitalSimulation`）。
2.  点击 **Refresh Graph**。
3.  **操作图表**:
    *   **展开**: 点击黄色节点上的 `+` 号展开子结构。
    *   **固定**: 点击 `⚓` 图标或拖动节点可固定位置（开启 Physics 时）。
    *   **缩放/平移**: 使用鼠标滚轮缩放，拖拽画布平移。

### 使用 Agent
1.  在左侧选择或新建 session。
2.  在中间的 **Session Chat** 输入指令，例如："请帮我给 Department 模型增加一个名为 'emergency_stop' 的输入端口"。
3.  Agent 处理完成后，系统会自动刷新该 session 的项目列表，您可以在右侧 **Visualizer** 中重新选择项目并点击 "Refresh Graph" 查看变更。

## 目录结构

```
src/
├── components/
│   ├── SessionSelectorPanel.tsx # Session 选择和创建
│   ├── ChatInterface.tsx        # Session 对话面板
│   ├── ProjectPanel.tsx         # Project 选择和上传
│   ├── VisualizationControls.tsx # Root model 和导出控制
│   ├── ApiConfigPanel.tsx       # 可视化解析 API 配置
│   └── GraphVisualizer.tsx      # D3.js 核心绘图组件
├── services/
│   ├── agentService.ts      # 后端 API 通信服务
│   └── geminiService.ts     # 前端 LLM 代码解析服务
├── types.ts                 # 类型定义
├── App.tsx                  # 主应用逻辑
└── ...
```

## 注意事项

*   **API 消耗**: 解析大型模型结构会消耗较多 Token。
*   **后端连接**: 如果控制台提示 "Backend offline"，功能将回退到仅查看器模式（无法使用 Agent 修改代码）。
*   **安全性**: 请勿将 API Key 提交到代码仓库中。

---
*Powered by xDEVS Team*
