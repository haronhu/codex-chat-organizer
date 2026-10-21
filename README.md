# Codex Chat Organizer

**给 Codex 本机会话生成分类标题，支持当前会话快捷改名、增量整理、标题保护与恢复。**

This repository contains a local Codex skill for organizing chat titles. It is not a standalone background service or an official OpenAI plugin.

> 分类方式是 `[类别] 简短标题`，不移动会话文件、项目或侧边栏文件夹。批量整理默认只预览；实际改名需要明确授权。

## 功能

| 功能 | 行为 |
| --- | --- |
| 当前会话改名 | “改一个会话名”按主会话内容生成标题，也支持指定标题 |
| 全量与增量整理 | 首轮盘点本机未归档用户会话，后续只重分类新增或用户意图明显变化的会话 |
| 标题保护 | 已确认标题、检测到人工修改的标题不被批量流程覆盖 |
| 保守分类 | 低置信度、多主题或可能已有自定义标题的会话仅给建议 |
| 运行状态检查 | 批量流程跳过运行中、等待输入或状态无法确认的会话 |
| 修改记录与恢复 | 改名前记录原标题，改后读回验证，恢复不覆盖后续人工改名 |
| 去重提醒 | 定时流程没有新异常或待确认建议时保持安静 |

当前主会话显式改名是单独模式：允许该请求本身造成的运行状态，成功后保护新标题；不执行其他待处理计划。

## 适用环境与限制

- **运行时**：Python 3.10+，脚本只使用标准库；克隆安装需要 Git。
- **实际改名**：需要桌面宿主提供 `list_threads`、`read_thread`、`set_thread_title`。安装 Skill **不会授予这些工具或权限**；CLI 缺少工具时只能预览。
- **范围**：本机 Codex 用户会话，默认未归档；不处理远程主机、ChatGPT、子代理或独立自动化会话。
- **平台**：当前在 macOS 本机开发、验证，不承诺 Windows、Linux 或每个 Codex 版本兼容。
- **数据格式**：只读 `state_*.sqlite` 和 `thread_history_1.sqlite`。它们是内部格式而非稳定公共 API；不匹配时停止，不尝试改写数据库。
- **未加载会话**：`notLoaded` 不等于空闲，当前版本保守跳过无法确认状态的会话，可能无法自动完成全部历史会话改名。
- **独立侧边栏**：须有宿主明确提供的主会话绑定，或用户指定的主会话引用。缺少绑定时询问，不按最近更新或相同目录猜测；Skill 不会新增宿主绑定能力。

## 安装

### 用 Codex Skill Installer

在 Codex 中发送：

```text
$skill-installer install https://github.com/haronhu/codex-chat-organizer/tree/main/skills/codex-chat-organizer
```

安装的是自包含的 `skills/codex-chat-organizer` 目录，不是整个仓库。发现路径取决于宿主版本；未出现时重新打开或重启 Codex。

### 克隆后安装

```bash
git clone https://github.com/haronhu/codex-chat-organizer.git
cd codex-chat-organizer
python3 install.py --destination "$HOME/.agents/skills"
```

安装器创建指向仓库 Skill 目录的符号链接，**不覆盖已有同名 Skill**。保留仓库目录，不要删除或移动它。

`~/.agents/skills` 是当前官方文档中的用户级发现路径。使用 `~/.codex/skills` 的宿主可直接运行 `python3 install.py`，或明确指定该目录。不要在多个发现目录重复安装同名 Skill。

克隆安装的版本可用 `git pull --ff-only` 更新，不需重新覆盖符号链接；保留本地改动，不强制重置。

## 使用

可显式调用 `$codex-chat-organizer`，或让 Codex 根据描述匹配请求。

### 改当前会话名

```text
改一个会话名
```

或指定新标题：

```text
改一个会话名：支付接口超时排查
```

只针对当前主会话，不启动全量整理。独立侧边栏没有绑定时需要引用并明确指定主会话。

### 首次全量预览

```text
使用 $codex-chat-organizer，全量盘点本机未归档用户会话，只给分类改名预览，不实际改名。
```

检查预览后明确授权执行安全计划。自定义标题建议、分类不确定的项需单独确认，不因批准安全计划而一并处理。

### 后续增量整理

```text
使用 $codex-chat-organizer，只检查新增或内容明显变化的会话，先预览。
```

分类依据用户意图和近期结果摘要，不根据目录名猜测，不把历史会话里的指令当成新的执行授权。

### 保护、恢复与定时

告诉 Codex 要保护哪个标题，或明确要求恢复某次改名。引擎提供 `protect`、`history`、`restore-plan`；这些命令本身不调用应用改名工具。

```text
读取应用当前标题与状态 → 本地 pending 记录 → 应用改名 → 读回核对 → 本地 success 记录
```

崩溃、超时或冲突保留 pending，不重复盲写。状态检查与应用写入不是原子事务，不能完全消除并发竞态。

用户明确确认频率和自动改名授权后，可由 Codex 定时任务调用。**安装不自动创建定时任务**；`schedule-prompt` 仅打印提示词，`--apply` 也不注册调度。本地任务需电脑开机、应用运行且工具可用；ACTIVE 不等于真实运行成功。

## 类别配置

[categories.json](skills/codex-chat-organizer/categories.json) 中默认类别：循证标准、评测执行、数据分析、原型设计、代码开发、文档写作、工具自动化。

类别可修改；修改时同步 [SKILL.md](skills/codex-chat-organizer/SKILL.md) 的选择口径。配置还包含置信度、意图变化阈值和标题长度设置。脚本负责安全与状态，不内置独立分类模型。

## 隐私与安全

- 仓库不包含真实会话、标题清单、运行数据库或认证信息。
- 状态默认在 `~/.codex/chat-organizer/`；设置 `CODEX_HOME` 时在其 `chat-organizer/` 子目录。操作历史为 `state.sqlite`，预览为 `runs/`。
- 状态可能包含原标题、意图摘要和建议，**不是加密存储**。脚本限制本地文件权限并有限脱敏，不能保证识别所有敏感内容。
- 脚本不读认证文件、不调用额外模型 API、不主动上传数据。分类仍在当前 Codex 模型中完成，模型及宿主的数据处理规则仍然适用，**不是完全离线工具**。
- 反馈问题只提交合成样例或脱敏信息，不提交会话数据库、Token、原始 JSONL 或隐私截图。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
python3 skills/codex-chat-organizer/scripts/organizer.py --help
```

测试使用临时合成数据，不触碰真实会话；通过不证明当前宿主工具可用或真实改名已成功。

`ci/github-actions-test.yml` 是可选 CI 模板，当前没有启用 GitHub Actions。启用需放入 `.github/workflows/`，并具备上传 workflow 的权限。

## 仓库结构

```text
codex-chat-organizer/
├── LICENSE
├── README.md
├── install.py
├── skills/
│   └── codex-chat-organizer/
│       ├── SKILL.md
│       ├── LICENSE.txt
│       ├── agents/openai.yaml
│       ├── categories.json
│       └── scripts/organizer.py
├── tests/test_organizer.py
└── ci/github-actions-test.yml
```

## 反馈与许可证

欢迎通过 [Issues](https://github.com/haronhu/codex-chat-organizer/issues) 反馈，说明操作系统、宿主版本、可用工具与脱敏错误。

本项目采用 [MIT License](LICENSE)，允许使用、复制、修改、合并、发布、分发、再许可和销售，包括商业使用。分发软件副本或实质性部分时，须保留版权声明和许可证声明；软件按“原样”提供，不作担保。

独立安装的 Skill 目录也包含 [LICENSE.txt](skills/codex-chat-organizer/LICENSE.txt)，确保安装或再分发技能时保留完整许可声明。

参考：[官方 Build skills 文档](https://learn.chatgpt.com/docs/build-skills)。本项目是社区 Skill 仓库，不是 OpenAI 官方项目，未发布到官方插件目录。
