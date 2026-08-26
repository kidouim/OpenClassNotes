# OpenClassNotes

把课堂录音自动转换成结构化复习笔记，再编译成可以问答和可视化的本地 OpenKB 知识库。

它面向“把录音丢进去，尽量少操作”的使用方式：语音转写在本地完成，模型只负责压缩语言信息；公式不记录表达式和推导，只保留老师实际说出的名称、用途、条件、易错点和考试信号。

> 当前只做语音课程，不处理 PDF、幻灯片、图片或 OCR。

## 功能

- 本地语音转写：faster-whisper，自动识别中文和英文。
- 噪声容错：VAD 产生异常稀疏长片段时自动关闭 VAD 重试。
- 轻量笔记：不补充老师未讲的知识，不纠正不确定术语，公式只保留名称。
- 考试信号：提取必考、占比、重点、易错点并保留时间戳。
- OpenKB Wiki：把新增或修改的笔记编译成相互链接的总结、概念和实体页面。
- 有依据的问答：OpenKB 从本地 Wiki 中查找证据后回答。
- 原生动态图谱：同一页面提供 3D 力导向、思维导图和径向树视图。
- 自动续跑和增量同步：完成项自动跳过；未变化笔记不会再次消耗编译 Token。

## 系统要求

- Windows 10/11、Linux 或 macOS
- Python 3.10-3.12，推荐 Python 3.11
- DeepSeek API Key，或其他 OpenAI Chat Completions 兼容服务
- NVIDIA GPU 可选；没有可用 CUDA 时自动使用 CPU

首次转写会下载 Whisper small。OpenKB 的安装体积和依赖比旧版简单向量库更大，请预留安装时间和磁盘空间。

## Windows 安装（PowerShell）

不需要 WSL、Conda、Docker 或虚拟机。

```powershell
git clone https://github.com/kidouim/OpenClassNotes.git
cd OpenClassNotes
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
.\run.ps1
```

脚本在项目内创建 `.venv`，不会修改系统 Python 环境。指定转写设备：

```powershell
.\run.ps1 -Device cpu
.\run.ps1 -Device cuda
```

## Linux / macOS 安装

```bash
git clone https://github.com/kidouim/OpenClassNotes.git
cd OpenClassNotes
bash setup.sh
bash run.sh
```

指定设备：

```bash
CLASSNOTES_DEVICE=cpu bash run.sh
CLASSNOTES_DEVICE=cuda bash run.sh
```

## 使用方法

支持 `mp3`、`wav`、`m4a`、`flac`、`ogg` 和 `aac`。

1. 把录音放进 `mp3/`。
2. 运行 `run.ps1` 或 `run.sh`。
3. 首次运行按提示配置 API Key、Base URL 和模型名称。
4. 选择“转换新课”。
5. 用菜单 2 向全部课程提问，或用菜单 3 打开动态图谱。

```text
1) 转换新课 (mp3 → 笔记 → OpenKB)
2) 向知识库提问
3) 打开 OpenKB 3D 图谱
4) 同步已有笔记
5) 配置 API key
6) 使用指南
```

从旧版本升级后，先运行一次菜单 4。它会把 `notes/` 中已有笔记增量编译到 OpenKB；再次运行时会跳过内容未变化的文件。

需要让已完成的课程应用新版转写清洗规则时，在主菜单输入 `r`，再选择一门课程重新处理。空输入会取消，避免误操作导致全部课程重复消耗 API Token。

## Token 与隐私

这三个阶段会调用你配置的 LLM API：

1. 转写稿 → ASR 局部纠错 + 轻量结构化笔记（同一次模型处理）；
2. 笔记 → OpenKB Wiki、概念和实体关系；
3. 向 OpenKB 提问。

第 1 步中的纠错不会增加一轮 API 请求：模型在生成笔记时同时返回少量精确替换补丁，程序本地校验后应用。第 2 步是 OpenKB 为获得高质量知识结构而增加的一次编译，不是纯本地免费操作。项目按文件内容哈希增量同步，未变化笔记不会重复编译。菜单 3 的 HTML 图谱生成完全本地，不调用 LLM。

录音、转写、笔记、Wiki 和图谱均保存在本地。API Key 只保存在 `~/.class-notes/config.json`，不会复制进项目中的 OpenKB 目录；应用只在请求时把它传给 OpenKB。

## 数据位置

| 路径 | 内容 |
|---|---|
| `mp3/` | 待处理录音 |
| `transcripts/` | 自动纠错后的带时间戳转写稿 |
| `notes/` | 提炼后的 Markdown 笔记 |
| `graphs/processing_state.json` | 自动续跑状态 |
| `knowledge/wiki/` | OpenKB 生成的总结、概念、实体与链接 |
| `knowledge/output/visualize/graph.html` | OpenKB 原生自包含动态图谱 |
| `~/.class-notes/config.json` | API 配置，仅当前用户可读写 |
| `~/.class-notes/raw_transcripts/` | 仅供失败恢复的隐藏原始转写备份 |

上述课程数据目录默认已写入 `.gitignore`，不会随普通 `git add .` 提交。

## 架构

```text
音频
  ↓ faster-whisper（本地）
带时间戳转写
  ↓ DeepSeek / OpenAI 兼容 API（同次返回纠错补丁和笔记）
自动纠错后的转写 + 轻量结构化笔记
  ↓ OpenKB 编译（仅新增或修改文件）
互相链接的本地 Wiki
  ├─ 有依据的知识库问答
  └─ 3D / 思维导图 / 径向树可视化（本地）
```

OpenClassNotes 使用两层约束防止知识库变成模型自行补写的教材：先把来源约束加入 OpenKB 编译提示，再用确定性的落盘校验把概念页正文收敛为原笔记中的逐字摘录。概念标题无法被笔记原句完整支持时，该节点不会进入知识库；课程到概念只保留一条有效边，图中名称来自 Markdown 标题而不是文件名。OpenKB 仍负责概念规划、Wiki 结构、跨课程同名概念复用、问答和原生可视化；公式不展开，`[unclear]` 不猜测。

ASR 清洗采用受约束的局部替换，不让模型自由重写全文。补丁必须绑定转写行号、逐字匹配原片段且置信度不低于 0.95；数字、否定关系、跨行修改、重复命中和过长扩写会被本地拒绝。只有实际应用纠错时才隐藏备份原始转写，普通用户始终看到清洗后的版本。

## 开发与测试

Windows：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Linux / macOS：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

自动测试不需要真实 API Key，也不会调用外部 LLM。

## 常见问题

### PowerShell 禁止运行脚本

只对当前终端临时放行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

### Hugging Face 提示未认证或 Windows 不支持符号链接

这是模型下载缓存警告，不代表转写失败。未设置 `HF_TOKEN` 时下载限额较低；Windows 无符号链接权限时会多占一些磁盘。`run.ps1` 已隐藏符号链接提示，不要求管理员权限或开发者模式。

### GPU 初始化或转写失败

程序默认自动回退 CPU，也可以明确运行：

```powershell
.\run.ps1 -Device cpu
```

### 图谱为空

菜单 3 只可视化已经编译的 OpenKB Wiki，不会偷偷触发一轮付费编译。先转换一节新课；从旧版本升级则运行菜单 4 同步已有笔记。

### 图谱没有自动打开

程序仍会打印本地 `graph.html` 的完整路径，手动双击即可。页面是自包含文件，不需要启动服务器。

## 第三方项目与许可证

[VectifyAI/OpenKB](https://github.com/VectifyAI/OpenKB) 作为 PyPI 依赖使用，采用 Apache License 2.0。OpenKB 负责 Wiki 编译、基于知识库的问答和原生可视化；本项目没有复制或修改其源代码。

OpenClassNotes 同样采用 Apache License 2.0，详见根目录 `LICENSE`。
