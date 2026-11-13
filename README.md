# ARGUS

ARGUS 是一个跑在家里 Windows 电脑上的私人 AI 管家。家里用麦克风阵列喊名字唤醒对话，出门用配套的 iPhone App 说话或打字。它记得你的事，到点、到地方会主动提醒你，还能接上日历、邮件、天气、Home Assistant 和智能体代理，替你办事。

除了调用云端模型和各家开放接口，所有数据都存在本机。

## 能做什么

- **语音对话**：唤醒词、语音活动检测、判断你说完没有、说话时可以打断。声纹认人：没认出是你时，只开放公共能力。
- **记忆**：对话里提到的事会抽取进长期记忆，可以说"记住""忘掉"。也能检索你自己写的资料（`self/` 目录下的 Markdown）。
- **提醒**：闹钟、按时刻的提醒、"到家提醒我……"这类按地点的提醒（手机上报位置，地理围栏判断）。
- **主动提醒**：每条候选按价值和紧迫度打分，决定是出声、推通知、攒进早晚简报还是丢掉。有每日配额、睡眠时段和免打扰；家里有客人时，私事不在音箱上念。
- **日程和邮件**：CalDAV 日历、待办；IMAP 读信、存草稿，SMTP 发信要先确认。
- **出行和信息**：和风天气及天气预警、高德周边搜索和路线、联网搜索。
- **家居**：通过 Home Assistant 开关空调、调温度，查室内温湿度。
- **派活给智能体代理**：用 Claude Agent SDK 在白名单目录里开工作区改代码，跑完把结论念给你、详情推到手机；写回原目录前要你确认。
- **旁听 Claude Code**：你自己在这台电脑上用 Claude Code 时，它答完了、或者在等你批准，ARGUS 会提醒你（配置见下）。
- **权限分级**：工具按风险分级。高风险操作在 App 上弹确认卡片，用面容 ID 签名后才执行；全部操作写审计日志。
- **隐私和花费**：发往云端前，敏感词先换成占位符，回复里再换回原文。按月统计云端花费：到了预算上限就不再调用云端，下个月自动恢复，急用时可以用 `budget add` 追加。

## 目录

```
config/          配置：主配置、模型、规则意图、工具、主动提醒开关、人格提示词
src/argus/       主机端 Python 代码
migrations/      SQLite 迁移（argus、audit、docs 三个库）
ios/             iPhone App（SwiftUI，XcodeGen 生成工程）
third_party/     ZipVoice（本地语音合成，Apache-2.0）
```

`src/argus` 按层组织，只能依赖下层，由 import-linter 检查，从上到下是：

| 层 | 作用 |
| --- | --- |
| `app` | 组合根：读配置和密钥、组装各模块、命令行、HTTP/WebSocket 服务 |
| `infra` | 和外界打交道的实现：音频、语音识别和合成、唤醒词、SQLite、各家接口 |
| `session` / `proactive` | 一轮对话的流程 / 主动提醒的触发、打分、配额、简报 |
| `routing` / `cognition` / `knowledge` | 规则意图和模型的分流 / 拼上下文、调模型 / 个人资料检索 |
| `memory` / `delivery` / `codeagent` | 长期记忆 / 出声和推送的出口选择 / 代码代理 |
| `speech` / `situation` | 语音流水线 / 位置、地理围栏、在不在家、有没有客人 |
| `action` | 工具注册和执行 |
| `governance` | 权限分级、确认单、审计 |
| `llm` / `channels` / `scheduler` | 模型网关 / 端点协议 / 定时任务 |
| `kernel` | 事件、时钟、工具描述等最底层的类型 |

业务模块不直接做 IO：网络、数据库、音频都通过 `infra` 的实现注入；时间和 ID 也通过注入的 `Clock` 和 `IdGen` 获取。

## 运行环境

- Windows 10/11，Python 3.12，依赖由 [uv](https://docs.astral.sh/uv/) 管理
- 云端模型：主力模型和代码代理默认用 DeepSeek，备用模型、语音识别和合成用阿里云百炼（DashScope）。改 `config/models.yaml` 可以换成其他 OpenAI 兼容接口
- 可选：USB 麦克风阵列（配置里是 reSpeaker XVF3800）。不配 `audio` 段就没有家里的语音入口，只能用 App
- 可选：NVIDIA 显卡，用本地 ZipVoice 合成语音。用云端合成（`tts.engine: cosyvoice`）就不需要
- 可选：iPhone（iOS 18+）、装了 Xcode 和 [XcodeGen](https://github.com/yonaskolb/XcodeGen) 的 Mac，用来装 App
- 用 App 时需要 [Tailscale](https://tailscale.com/)：主机只监听本机地址，App 通过 Tailscale 的 HTTPS 地址连上来

## 快速开始

### 1. 安装依赖

```powershell
uv sync
```

默认会装 `audio`（本地语音模型）和 `gpu`（CUDA 版 PyTorch、ZipVoice）两个依赖组。没有显卡时，用 `uv sync --no-group gpu`。

### 2. 准备数据目录

数据都放在仓库外的数据根目录，默认是 `D:/ARGUS-data`，改 `config/argus.yaml` 的 `data_root` 可以换位置：

```
D:/ARGUS-data/
  secrets/.env          密钥（见下）
  secrets/关于我.md      可选：你的基本情况，用 import-profile 导入成常驻记忆
  data/                 数据库；hotwords.txt（识别热词）、sensitive_words.txt（发往云端前要替换的词），一行一个
  logs/                 日志
  models/               本地模型（见下）
  self/                 可选：你自己写的资料（Markdown），用 sync-notes 建索引
```

`secrets/.env` 里必填的只有这几项：

```
LLM_API_KEY=            # 主力模型的 key
QWEATHER_API_HOST=      # 和风天气：API Host、开发者 ID、项目 ID、凭据 ID、Ed25519 私钥
QWEATHER_DEVELOPER_ID=
QWEATHER_PROJECT_ID=
QWEATHER_KEY_ID=
QWEATHER_PRIVATE_KEY=
```

其余都是选填，没配就不提供对应功能：

| 变量 | 用途 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 阿里云百炼：备用模型、云端语音识别和合成、识别热词 |
| `APP_TOKEN` | App 连主机用的 token，用 `cli new-app-token` 生成 |
| `BARK_DEVICE_KEY`、`BARK_ENCRYPTION_KEY` | 用 [Bark](https://github.com/Finb/Bark) 往手机推加密通知 |
| `HA_TOKEN` | Home Assistant 长期访问令牌 |
| `CALDAV_USERNAME`、`CALDAV_PASSWORD` | CalDAV 日历和待办 |
| `MAIL_USERNAME`、`MAIL_PASSWORD` | 邮箱（IMAP/SMTP，填授权码） |
| `IQS_API_KEY` | 联网搜索 |
| `AMAP_KEY` | 高德 Web 服务：周边搜索、路线 |
| `TENCENT_MAP_KEY` | 腾讯位置服务：把坐标反查成地名 |

### 3. 下载本地模型

放在 `models/` 下，目录名要和下面一致。用不到的功能可以不放：

| 目录 | 模型 |
| --- | --- |
| `kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01` | 唤醒词 |
| `vad/silero-vad`、`vad/turnsense-1.0` | 语音活动检测、判断说完没有 |
| `speaker/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced` | 声纹 |
| `asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09` | 本地语音识别（`asr.engine: sensevoice` 时） |
| `embedding/bge-small-zh-v1.5` | 记忆和资料检索的向量模型 |
| `tts/zipvoice_distill`、`tts/vocos-mel-24khz`、`tts/voice/` | 本地语音合成（`tts.engine: zipvoice` 时） |

### 4. 改配置

| 文件 | 内容 |
| --- | --- |
| `config/argus.yaml` | 主配置：数据目录、家的大致坐标、音频设备、语音引擎、主动提醒阈值、Home Assistant、日历、邮箱。示例值要换成你自己的；音频设备名写错时，启动会报错并列出现有的设备 |
| `config/models.yaml` | 用哪个模型、单价、每月预算 |
| `config/intents.yaml` | 规则意图：这些说法不经过模型，直接调工具回答 |
| `config/tools.yaml` | 工具的权限覆盖，以及代码代理的轮数、时长、禁止的命令 |
| `config/triggers.yaml` | 各类主动提醒的开关 |
| `config/persona.md` | 人格提示词 |
| `config/code_projects.example.yaml` | 复制成 `code_projects.yaml`，列出允许代码代理工作的目录 |

### 5. 启动

```powershell
uv run python -m argus.app.main
```

服务监听 `http://127.0.0.1:8765`，`/healthz` 返回健康状态。

### 6. 命令行工具

```powershell
uv run python -m argus.app.cli --help
```

常用的有：

- 录音、放音：`record`、`play`
- 声纹：`enroll-voice`、`score-voice`、`calibrate-voice`。另录一份用 `enroll-voice --as 名字`：名字以 `array` 开头的（在家里麦克风阵列前录的）家里和 App 都比，别的（手机、耳机录的）只给 App 比
- 不开主进程试对话：`ask`、`chat`
- 调唤醒词和判停：`wake`、`vad`
- App 和推送：`new-app-token`、`new-bark-key`、`push`
- 录常去的地点：`place set|list|remove|where`
- 云端花费：`budget`
- 导入和同步个人资料：`import-profile`、`sync-notes`

## 旁听 Claude Code（可选）

在 `~/.claude/settings.json` 里加上两个 http 类 hook，指向主机的 `/hooks/claude-code`（只接受本机调用）：

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "http", "url": "http://127.0.0.1:8765/hooks/claude-code", "timeout": 3 }] }
    ],
    "Notification": [
      {
        "matcher": "permission_prompt",
        "hooks": [{ "type": "http", "url": "http://127.0.0.1:8765/hooks/claude-code", "timeout": 3 }]
      }
    ]
  }
}
```

提醒的开关是 `config/triggers.yaml` 里的 `code_task_finished` 和 `code_task_needs_attention`，派活的汇报也用这两个开关。

## iPhone App

```bash
cd ios
xcodegen generate
open Argus.xcodeproj
```

在 Xcode 里选上自己的开发者账号签名，装到手机上。

主机和手机都登录同一个 Tailscale 网络。在主机上把 App 用的 WebSocket 发布成 HTTPS：

```powershell
tailscale serve --bg --set-path /app/ws http://127.0.0.1:8765/app/ws
```

打开 App 后填：

- **主机地址**：主机在 Tailscale 里的名字，如 `argus.xxxx.ts.net`
- **访问 Token**：`.env` 里的 `APP_TOKEN`

App 有语音页和文字页，可以弹出确认卡片；会在后台上报位置，供地点提醒使用。

## 许可证

[MIT](LICENSE)。`third_party/zipvoice` 是 [ZipVoice](https://github.com/k2-fsa/ZipVoice) 的代码，遵循它自己的 Apache-2.0 许可证。
