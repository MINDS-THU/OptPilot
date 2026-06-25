```bash
conda create -n openhands python=3.12
conda activate openhands
pip install openhands-sdk openhands-tools openhands-workspace openhands-agent-server
pip install numpy pandas pyyaml tqdm tomli click argcomplete userpath
pip install ../swe_agent_run/docker_construct/xdevspy
```