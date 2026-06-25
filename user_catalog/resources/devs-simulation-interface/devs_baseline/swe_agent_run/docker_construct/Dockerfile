FROM python:3.11.10-bullseye

ARG DEBIAN_FRONTEND=noninteractive  
ENV TZ=Etc/UTC

# 安装通用的 PyPI 包 
RUN pip install --upgrade pip
RUN pip install pipx
RUN pipx install swe-rex
RUN pipx ensurepath

RUN pip install --no-cache-dir \
    numpy \
    simpy \
    pandas \
    pyyaml \
    tqdm \
    pytest \
    tomli \
    click \
    argcomplete \
    userpath \
    swe-rex

# === 安装本地 xdevs 包 ===

# 将当前目录下的 xdevs 文件夹复制到镜像里的临时目录
COPY ./xdevspy /tmp/xdevs_source

# 进入该目录并执行 pip install .
RUN cd /tmp/xdevs_source && \
    pip install . && \
    rm -rf /tmp/xdevs_source

COPY ./xdevs_usage /data/xdevs_usage

# 设置 Git 信息 
RUN git config --global user.email "agent@bot.com" && \
    git config --global user.name "SWE Agent"

# 验证安装
RUN python -c "import xdevs; print('Successfully installed xdevs version:', xdevs.__version__ if hasattr(xdevs, '__version__') else 'installed')"

WORKDIR /

SHELL ["/bin/bash", "-c"]