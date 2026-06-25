# SWE agent 适配方法
推荐使用conda重新创建一个新环境。

1. 创建环境：`conda create -n sweagent python=3.12`，然后 `conda activate sweagent`
2. 安装 swe agent, 略 https://swe-agent.com/latest/installation/source/
	还要： `pip install httpx[socks]`, 以及一些其他包，来和docker里面的环境对齐。记得把xdevs.py装上。
3. 构建需要使用的docker：
```bash
cd docker_construct
docker build -t python-xdevs-simpy .
```
4. 把测试脚本里的 conda_env 变量改成这里实际的环境名（上面的例子里是 sweagent）。
5. 在本文件夹（`devs_baseline/swe_agent_run`）底下复制一份放在最外面的 `.env` 文件。或者从零创建并指定 `OPENAI_API_KEY`, `OPENAI_BASE_URL`。