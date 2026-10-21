# Codex Chat Organizer

[English](README.md) | [简体中文](README.zh-CN.md)

**Organize local Codex chats with category-prefixed titles, quick current-chat renaming, incremental updates, title protection, and restoration.**

This repository contains a local Codex skill for organizing chat titles. It is not a standalone background service or an official OpenAI plugin.

> Classification uses `[Category] Short title`. It does not move chat files, projects, or sidebar folders. Batch organization is preview-only by default; renaming requires explicit authorization.

## Features

| Feature | Behavior |
| --- | --- |
| Current-chat renaming | Generate a title from the main chat, or use a title you specify |
| Full and incremental organization | Inventory local unarchived user chats first; subsequently reclassify only new chats or chats with meaningful changes in user intent |
| Title protection | Keep confirmed titles and detected manual edits out of the batch rename flow |
| Conservative classification | Suggest changes only for low-confidence, multi-topic, or potentially customized titles |
| Runtime checks | Skip running chats, chats waiting for input, and chats whose status cannot be confirmed during batch organization |
| Rename history and restoration | Record the original title before renaming, verify the result afterward, and avoid overwriting subsequent manual edits during restoration |
| Deduplicated notifications | Stay quiet during scheduled runs unless there are new issues or suggestions requiring confirmation |

Explicit current-main-chat renaming is a separate mode: it allows the running state caused by the request itself, protects the new title on success, and does not execute other pending plans.

## Requirements and Limitations

- **Runtime:** Python 3.10+ with the standard library only. Git is needed for installation by cloning.
- **Actual renaming:** The desktop host must provide `list_threads`, `read_thread`, and `set_thread_title`. Installing the skill **does not grant these tools or permissions**. A CLI environment without the required tools can only preview changes.
- **Scope:** Local Codex user chats, unarchived by default. Remote hosts, ChatGPT chats, subagents, and standalone automation chats are excluded.
- **Platform:** Developed and validated locally on macOS. Compatibility with Windows, Linux, or every Codex version is not guaranteed.
- **Data format:** Reads `state_*.sqlite` and `thread_history_1.sqlite` in read-only mode. These are internal formats, not stable public APIs. The adapter stops on incompatible schemas rather than attempting to rewrite databases.
- **Unloaded chats:** `notLoaded` does not mean idle. The current version conservatively skips chats whose status cannot be confirmed, so some historical chats may remain unchanged.
- **Separate sidebar chats:** An explicit main-chat binding from the host, or a main chat explicitly referenced by the user, is required. Without it, the skill asks for the target rather than guessing from recency or a shared directory. The skill does not add host binding capabilities.

## Installation

### Using Codex Skill Installer

Send this request in Codex:

```text
$skill-installer install https://github.com/haronhu/codex-chat-organizer/tree/main/skills/codex-chat-organizer
```

Install the self-contained `skills/codex-chat-organizer` directory, not the entire repository. Discovery paths depend on the host version. Reopen or restart Codex if the skill does not appear.

### Clone and Install

```bash
git clone https://github.com/haronhu/codex-chat-organizer.git
cd codex-chat-organizer
python3 install.py --destination "$HOME/.agents/skills"
```

The installer creates a symlink to the repository's skill directory and **does not overwrite an existing skill with the same name**. Keep the repository directory in place; do not delete or move it after installation.

`~/.agents/skills` is the user-level discovery path documented in the current official guidance. Hosts using `~/.codex/skills` can run `python3 install.py` directly or explicitly specify that directory. Do not install duplicate copies into multiple discovery locations.

For a cloned installation, update with `git pull --ff-only`. There is no need to replace the symlink. Preserve local changes instead of forcing a reset.

## Usage

Invoke `$codex-chat-organizer` explicitly, or let Codex match your request to the skill description.

### Rename the Current Chat

The exact quick commands currently supported by the state engine are Chinese. The README language switch changes the documentation language, not the command parser or default category labels.

Send this to generate a title from the current main chat:

```text
改一个会话名
```

Or specify a title, which may be in English:

```text
改一个会话名：Payment API timeout investigation
```

This targets only the current main chat and does not start batch organization. A separate sidebar chat without a binding requires an explicit reference to the main chat.

### First Full Preview

```text
Use $codex-chat-organizer to inventory all local unarchived user chats and preview category-prefixed title changes. Do not rename anything.
```

Review the preview, then explicitly authorize the safe plan. Suggestions involving customized titles or uncertain categories need separate confirmation; approving the safe plan does not approve every suggestion.

### Subsequent Incremental Organization

```text
Use $codex-chat-organizer to check only new chats or chats with meaningful content changes. Preview the changes first.
```

Classification uses user intent and recent outcome summaries. It does not infer topics from directory names or treat instructions in historical chats as new authorization.

### Protection, Restoration, and Scheduling

Tell Codex which title to protect, or explicitly request restoration of a previous rename. The engine provides `protect`, `history`, and `restore-plan`; these commands do not call the application's rename tool themselves.

```text
Read current application title/status → Local pending record → Application rename → Read-back verification → Local success record
```

Crashes, timeouts, and conflicts leave a pending record instead of triggering blind retries. Status checks and application writes are not atomic, so concurrent-operation races cannot be eliminated completely.

Codex scheduled tasks can invoke the skill after the user explicitly confirms a cadence and authorizes automatic renaming. **Installation does not create scheduled tasks.** `schedule-prompt` only prints a prompt, and `--apply` does not register a schedule. Local runs require the computer to be on, the app to be running, and the required tools to be available. An ACTIVE configuration is not proof of a successful run.

## Category Configuration

Default category labels in [categories.json](skills/codex-chat-organizer/categories.json):

| Label | Meaning |
| --- | --- |
| 循证标准 | Evidence-based evaluation standards |
| 评测执行 | Evaluation execution |
| 数据分析 | Data analysis |
| 原型设计 | Prototype design |
| 代码开发 | Code development |
| 文档写作 | Document writing |
| 工具自动化 | Tools and automation |

Labels can be changed. Update the corresponding selection guidance in [SKILL.md](skills/codex-chat-organizer/SKILL.md) as well. Configuration also includes confidence thresholds, intent-change thresholds, and title-length settings. The script handles safety and state; it does not include a standalone classification model.

## Privacy and Safety

- The repository contains no real chats, title inventories, runtime databases, or credentials.
- State is stored in `~/.codex/chat-organizer/` by default, or in `chat-organizer/` under `CODEX_HOME` when configured. Operation history is in `state.sqlite`, and local previews are in `runs/`.
- State may contain original titles, intent summaries, and suggestions. **It is not encrypted.** The script restricts local file permissions and applies limited redaction; it cannot guarantee detection of all sensitive content.
- The script does not read authentication files, call additional model APIs, or upload data on its own. Classification still happens in the current Codex model, so the model's and host's data-handling policies apply. **This is not a fully offline tool.**
- Report issues using synthetic examples or redacted information. Do not submit chat databases, tokens, raw JSONL files, or screenshots containing private data.

## Development and Validation

```bash
python3 -m unittest discover -s tests -v
python3 skills/codex-chat-organizer/scripts/organizer.py --help
```

Tests use temporary synthetic data and do not modify real chats. Passing tests do not prove host tool availability or successful real-world renaming.

`ci/github-actions-test.yml` is an optional CI template. GitHub Actions is not currently enabled. To enable it, place the template in `.github/workflows/` using credentials with permission to upload workflow files.

## Repository Layout

```text
codex-chat-organizer/
├── LICENSE
├── README.md
├── README.zh-CN.md
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

## Feedback and License

Report issues through [GitHub Issues](https://github.com/haronhu/codex-chat-organizer/issues), including your operating system, host version, available tools, and redacted errors.

This project is licensed under the [MIT License](LICENSE), allowing use, copying, modification, merging, publication, distribution, sublicensing, and sale, including commercial use. Copies or substantial portions of the software must retain the copyright and permission notices. The software is provided "as is", without warranty.

The standalone skill directory also includes [LICENSE.txt](skills/codex-chat-organizer/LICENSE.txt), so the complete license notice is retained when installing or redistributing the skill.

Reference: [Official Build skills documentation](https://learn.chatgpt.com/docs/build-skills). This is a community skill repository, not an official OpenAI project, and has not been published to the official plugin directory.
