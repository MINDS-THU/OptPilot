# Meta GPT 适配方法

推荐使用conda重新创建一个新环境。

1. 创建环境：`conda create -n metagpt python=3.10`，然后 `conda activate metagpt`
2. 安装 meta agent，见 https://github.com/FoundationAgents/MetaGPT
	- 省流：可以直接运行 pip install --upgrade git+https://github.com/geekan/MetaGPT.git 
3. 再装点通用环境，比如 `pip install numpy pandas pyyaml tqdm tomli click argcomplete userpath`, `pip install ../swe_agent_run/docker_construct/xdevspy`
4. 把测试脚本里的 conda_env 变量改成这里实际的环境名（上面的例子里是 metagpt ）。
5. 在本文件夹（`devs_baseline/meta_gpt_run`）底下复制一份放在最外面的 `.env` 文件，并添加一条 `META_GPT_HUMAN_IN_LOOP=false`。或者从零创建并指定 `OPENAI_API_KEY`, `OPENAI_BASE_URL`。