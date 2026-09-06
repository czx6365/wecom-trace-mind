# 企业微信本地数据读取：Windows + macOS 开源调研与落地方案

> 调研目标：基于现有开源项目，评估企业微信桌面端本地数据读取能力，并设计一套同时支持 Windows 与 macOS 的可长期维护实现方案。  
> 调研时间：2026-08-28  
> 定位：技术调研 / 架构设计，不直接等同于生产可用结论；不同企业微信版本仍需实机验证。

---

## 1. 背景与目标

目标不是单纯“解密一个数据库”，而是做一层稳定的 **WeCom Local Data Runtime / Collector**，让上层 Agent 或业务系统能够读取当前用户本机企业微信已经可见的数据，例如：

- 会话列表
- 群聊 / 单聊
- 历史消息
- 群成员
- 联系人
- 关键词搜索
- 后续增量更新
- Agent / MCP / Local API 调用

同时要求：

1. **Windows 与 macOS 同时支持**
2. 平台差异尽量收敛在底层适配层
3. 数据库解密、WAL、Schema、消息解析、查询能力尽量共用
4. 源企业微信数据只读
5. 不直接依赖官方会话存档 API
6. 对企业微信版本升级具备可维护性
7. 最终适合 Agent 稳定调用，而不是一次性的逆向脚本

---

# 2. 核心结论

## 2.1 Windows 与 macOS 都已有开源实现证明可行

现有项目已经分别证明：

### Windows

可以通过本机 `WXWork.exe` + 本地数据库实现：

```text
WXWork.exe
   ↓
本地数据目录
   ↓
message.db / session.db / user.db / ...
   ↓
wxSQLite3 AES-128-CBC
   ↓
明文 SQLite Snapshot
   ↓
Conversation / Message / Contact / Member
```

### macOS

也已经有项目完整实现：

```text
企业微信.app
   ↓
Containers / Group Containers
   ↓
message.db / session.db / user.db
   ↓
wxSQLite3 AES-128-CBC
   ↓
WAL 合并
   ↓
明文 Snapshot
   ↓
sessions / contacts / history / search / export
```

因此双平台的关键并不是“能不能做”，而是：

> 如何把 Windows/macOS 的差异限制在平台采集层，避免维护两套完全独立实现。

---

# 3. 开源项目对比

## 3.1 总览

| 项目                                             | Windows  |    macOS     |      会话      |  历史消息  | 群成员 |   联系人   |   WAL    | Agent 友好度 | 主要价值                 |
| ------------------------------------------------ | :------: | :----------: | :------------: | :--------: | :----: | :--------: | :------: | :----------: | ------------------------ |
| WecomTeam/wecom-cli                              |    ✅     |      ✅       | ⚠️ Bot 最近会话 | ❌ 桌面历史 |   ❌    | ✅ 官方能力 |    -     |    ★★★★★     | 官方 API 动作层          |
| wangk-ask/wecom-reader                           |    ✅     |      ❌       |       ✅        |     ✅      |   ⚠️    |     ✅      |    ★★    |     ★★★★     | Windows 快速 POC         |
| Luchioxy/wxwork-cli                              | ✅ 主平台 |    🚧 计划    |       ✅        |     ✅      |   ✅    |     ✅      |    ★★    |    ★★★★★     | CLI / Agent Tool 设计    |
| kizzhang/wecomcracker                            |    ✅     |      ❌       |       ✅        |     ✅      |   ⚠️    |    基础    | 保守处理 |    ★★★★★     | 只读 Snapshot / 安全边界 |
| NOBB2333/wechat-analysis-action                  |    ✅     | 未见明确支持 |       ✅        |     ✅      | 可扩展 |     ✅      |    ★★    |     ★★★      | Windows 底层研究         |
| lycosa9527/MindGraph WeCom Reader                |    ✅     | 未见明确支持 |       ✅        |     ✅      | 可扩展 |     ✅      |   ★★★    |     ★★★★     | 模块化 Windows Reader    |
| mcncarl/yichen-skills / yichen-wecom-local-vault |    ❌     |      ✅       |       ✅        |     ✅      |   ✅    |     ✅      |  ★★★★★   |    ★★★★★     | macOS 最完整参考         |
| BobbyCats/wecom-local                            |    ❌     |      ✅       |       ✅        |     ✅      |   ✅    |     🚧      |  非核心  |    ★★★★★     | macOS Agent 查询层       |

---

# 4. Windows 开源项目

## 4.1 wangk-ask/wecom-reader

仓库：

- https://github.com/wangk-ask/wecom-reader

定位：

> 企业微信本地聊天记录读取工具，从 `WXWork.exe` 进程环境获取当前登录用户本地数据库访问材料，解密本地 SQLite 数据库。

已提供：

- CLI
- Python Library
- Web UI

主要能力：

```text
sessions
messages
search
contacts
```

README 中明确列出的数据库包括：

```text
message.db
session.db
user.db
file.db
calendar_r7.db
crm.db
```

会话 ID 规则：

```text
R: 群聊
S: 单聊
M: 微信联系人
O: 应用/公众号
Y: 系统会话
```

### 优点

- Windows 路线直接
- 代码量小
- 适合快速验证真实数据
- CLI / SDK / Web UI 都已经有
- 解密逻辑相对清晰

### 问题

- WAL 处理证据不足
- 群成员能力不是重点
- 项目较新
- 企业微信多版本兼容证据不足
- README 中“单全局 key”结论需要与其他项目交叉验证

### 推荐用途

```text
Windows POC
Windows 路径探测
DB 结构验证
基础查询参考
```

---

## 4.2 Luchioxy/wxwork-cli

仓库：

- https://github.com/Luchioxy/wxwork-cli

定位：

> 面向 LLM / Agent 的企业微信本地数据查询 CLI。

官方 README 明确：

```text
Windows 11: primary
macOS/Linux: planned
```

已注册 20+ 条命令，包括：

```text
sessions
history
search
contacts
departments
members
groups
tags

apps
approval
schedule
checkin
reports

stats
export
favorites
unread
new-messages
```

### 优点

最大的价值不是底层解密，而是：

> **CLI / Agent Tool 的产品接口设计非常值得参考。**

默认 JSON 输出，适合：

```text
LLM
Agent
MCP
脚本
Local API
```

### 需要注意

该项目文档和底层代码存在部分历史描述不一致。

例如部分技术文档写过 SQLCipher / AES-256，但实际 `crypto.py` 已实现：

```text
wxSQLite3
AES-128-CBC
16 byte raw key
4096 page
MD5(raw_key + page_no + "sAlT")
```

说明项目演进速度快，不能只相信 README，需要以代码为准。

### WAL 风险

当前 WAL 实现不建议直接作为最终标准，需要重新验证 WAL frame header、page number、commit、salt 等处理逻辑。

### 推荐用途

```text
CLI 设计
Agent Tool 设计
领域接口定义
命令模型
JSON 输出规范
```

---

## 4.3 kizzhang/wecomcracker

仓库：

- https://github.com/kizzhang/wecomcracker

定位：

> Windows 企业微信本地工作回溯 / Agent Skill。

重点能力：

```text
sessions
messages
search
```

上层用于：

```text
工作时间线
任务
承诺
截止时间
阻塞
未闭环事项
```

### 最大价值：安全与 Snapshot 设计

它明确采用：

- 源数据库只读
- 明文数据库输出到独立目录
- 查询使用 readonly / immutable
- `PRAGMA quick_check`
- 不直接写回企业微信数据
- WAL 不支持时不假装支持

如果检测到非空 WAL，默认中止；只有明确接受不完整数据时才允许 base-only。

这类设计比“错误处理 WAL 但看起来能跑”更可靠。

### 推荐用途

```text
Snapshot Layer
安全边界
只读模型
完整性校验
错误处理策略
```

---

## 4.4 NOBB2333/wechat-analysis-action

仓库：

- https://github.com/NOBB2333/wechat-analysis-action

企业微信部分：

```text
core-wecom
```

明确研究了 Windows 企业微信：

```text
%USERPROFILE%\Documents\WXWork\...
```

核心数据库：

```text
message.db
session.db
user.db
company.db
```

确认加密模型：

```text
wxSQLite3
AES-128-CBC
16-byte raw key
4096-byte page
无 HMAC
```

### 推荐用途

```text
Windows Crypto 交叉验证
Schema 研究
数据目录参考
底层实现参考
```

---

## 4.5 lycosa9527/MindGraph - WeCom Reader

仓库：

- https://github.com/lycosa9527/MindGraph

模块：

```text
clients/file-reader/file_reader/wecom/
```

其最大的价值是模块拆分清晰：

```text
discovery.py
crypto.py
key_extract.py
key_store.py
local.py
db_cache.py
db_reader.py
probe.py
```

同时记录了 Windows 企业微信可能存在多种数据目录结构，例如：

```text
%UserProfile%\Documents\WXWork\<account>\Data
```

或：

```text
%UserProfile%\Documents\WXWork\<corp>\<user>\Data
```

它还提出：

```text
不同 .db 可能对应不同 16-byte key
```

这一点和 `wecom-reader` 的“全局 key”结论不同。

### 这是必须实机验证的 P0

自己的架构不能把 key 写死成单值。

### 推荐用途

```text
Windows 模块化设计
Discovery
KeyStore
DB Cache
多目录兼容
多 Key 模型
```

---

# 5. macOS 开源项目

## 5.1 yichen-wecom-local-vault

仓库：

- https://github.com/mcncarl/yichen-skills
- 目录：`yichen-wecom-local-vault`

这是目前 macOS 侧研究最完整的参考之一。

明确支持：

```text
macOS
企业微信 5.x
```

主要能力：

```text
sessions
contacts
history
search
export
```

核心数据库：

```text
message.db
session.db
user.db
```

已解析的核心表包括：

```text
conversation_table
conversation_user_table
user_table
message_table
message_small_table
kf_message_tableV1
```

### 群成员

通过：

```text
conversation_table
        ↓ conversation_id
conversation_user_table
        ↓ user_id
user_table
```

可以恢复：

```text
群
├── 群名
├── conversation id
└── members
    ├── user
    └── nickname
```

### Crypto

确认：

```text
wxSQLite3 AES-128-CBC
raw key = 16 bytes
page size = 4096
per-page key = MD5(raw_key + page_no_le32 + "sAlT")
```

### WAL

这是目前最值得借鉴的部分。

它会：

```text
读取 WAL header
↓
解析 24-byte frame header
↓
校验 salt
↓
读取 page number
↓
识别 commit frame
↓
只保留最后完整 commit 前的 frame
↓
逐页解密
↓
合并到新的 SQLite Snapshot
```

### 推荐用途

```text
macOS Adapter
Crypto 交叉验证
WAL 实现
群成员解析
Snapshot 设计
Schema Reader
```

---

## 5.2 BobbyCats/wecom-local

仓库：

- https://github.com/BobbyCats/wecom-local

定位：

> 面向 Agent 的 macOS 本地企业微信只读查询层。

支持：

```text
conversations
history
members
search
stats
export
```

项目本身采用 Rust 单二进制设计。

### 最大价值

不是底层 DB 解密，而是：

```text
Agent-oriented Local Query
```

适合作为：

```text
CLI
Agent Skill
JSON Interface
查询语义
```

的参考。

---

# 6. 官方 WecomTeam/wecom-cli

仓库：

- https://github.com/WecomTeam/wecom-cli

官方支持：

```text
Windows x64
macOS x64 / arm64
Linux x64 / arm64
```

但它解决的是另一类问题。

主要面向：

```text
消息发送
邮件
文档
表格
待办
日程
会议
微盘
通讯录
```

例如机器人消息能力主要围绕：

```text
message aibot sessions list
message aibot send
```

它不等于：

```text
读取当前桌面客户端全部历史聊天
```

因此建议未来组合：

```text
Local Reader
    ↓
看懂发生了什么

Official wecom-cli
    ↓
执行动作
```

例如：

```text
Local Reader 读取项目群
↓
Agent 总结未闭环事项
↓
wecom-cli 创建待办 / 文档 / 会议 / 发送确认消息
```

---

# 7. 不建议直接 Fork 一个项目做双平台

现有项目大多偏单平台：

```text
Windows:
wecom-reader
wxwork-cli
wecomcracker
MindGraph WeCom

macOS:
yichen-wecom-local-vault
wecom-local
```

如果直接 fork 某个 Windows 项目再硬加 macOS，很容易形成：

```text
Windows 逻辑
macOS 逻辑
Crypto 重复
Schema 重复
Query 重复
Message Parser 重复
```

最终就是两套产品。

更合理的方式是：

> **新建一套跨平台 Core，把 OS 差异限制在 Platform Adapter。**

---

# 8. 推荐的最终架构

```text
                    Agent / Application
                           │
                           ▼
                  WeCom Local Service
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    Conversation API   Search API      Member API
          │                │                │
          └────────────────┼────────────────┘
                           ▼
                    Query / Model Layer
                           │
              ┌────────────┴────────────┐
              │                         │
        Schema Adapter             Message Parser
              │                         │
              └────────────┬────────────┘
                           ▼
                     Snapshot Layer
                           │
                    WAL Merge Layer
                           │
                     Crypto Layer
                           │
                    Key Provider
                     ▲           ▲
                     │           │
              Windows Adapter   macOS Adapter
                     │           │
               WXWork.exe     企业微信.app
```

---

# 9. 技术栈建议

## 推荐：Go

建议最终实现：

```text
Go
```

主要原因：

- 文件系统
- 进程
- 系统 API
- 二进制数据
- AES / MD5
- SQLite
- WAL
- 跨平台
- 长期运行服务
- 单文件发布

最终可以发布：

```text
wecom-local-windows-amd64.exe
wecom-local-darwin-arm64
wecom-local-darwin-amd64
```

不要求用户额外安装：

```text
Python
Node.js
Bun
虚拟环境
第三方 runtime
```

Rust 同样适合，但如果重点是：

```text
开发效率
跨平台系统访问
后续 Local Service
Agent API
```

Go 会更平衡。

---

# 10. 项目目录设计

推荐：

```text
wecom-local/
├── cmd/
│   └── wecom-local/
│
├── internal/
│   ├── platform/
│   │   ├── windows/
│   │   │   ├── discover.go
│   │   │   ├── process.go
│   │   │   ├── permission.go
│   │   │   └── key_provider.go
│   │   │
│   │   └── darwin/
│   │       ├── discover.go
│   │       ├── process.go
│   │       ├── permission.go
│   │       └── key_provider.go
│   │
│   ├── crypto/
│   │   ├── wxsqlite.go
│   │   └── verify.go
│   │
│   ├── wal/
│   │   ├── header.go
│   │   ├── frame.go
│   │   ├── parser.go
│   │   └── merge.go
│   │
│   ├── snapshot/
│   │   ├── builder.go
│   │   └── manifest.go
│   │
│   ├── schema/
│   │   ├── probe.go
│   │   ├── fingerprint.go
│   │   └── capabilities.go
│   │
│   ├── message/
│   │   ├── parser.go
│   │   ├── text.go
│   │   └── types.go
│   │
│   ├── model/
│   │   ├── conversation.go
│   │   ├── member.go
│   │   ├── contact.go
│   │   └── message.go
│   │
│   ├── query/
│   │   ├── conversations.go
│   │   ├── history.go
│   │   ├── members.go
│   │   └── search.go
│   │
│   └── api/
│       ├── cli/
│       └── http/
│
├── fixtures/
├── tests/
└── docs/
```

---

# 11. Platform Adapter

统一接口：

```go
type PlatformAdapter interface {
    DiscoverAccounts(ctx context.Context) ([]AccountSource, error)

    DiscoverDatabases(
        ctx context.Context,
        account AccountSource,
    ) ([]DatabaseSource, error)

    KeyProvider() KeyProvider
}
```

Windows：

```text
platform/windows
```

macOS：

```text
platform/darwin
```

上层不关心：

```text
WXWork.exe
企业微信.app
Documents\WXWork
Library/Containers
Group Containers
```

---

# 12. Key Provider 设计

不能假设只有一个 key。

建议：

```go
type KeySet struct {
    Global    []byte
    Databases map[string][]byte
}
```

解析顺序：

```text
Database-specific key
        ↓
不存在
        ↓
Global key
```

这样可以同时兼容：

```text
单 global key
每数据库独立 key
不同客户端版本变化
```

这是双平台架构非常重要的一点。

---

# 13. Crypto Layer

这一层应完全跨平台。

统一实现：

```go
func DecryptPage(
    rawKey []byte,
    pageNo uint32,
    encrypted []byte,
) ([]byte, error)
```

核心：

```text
raw key = 16 bytes
page = 4096 bytes

page_key =
MD5(
    raw_key
    + little_endian(page_number)
    + "sAlT"
)

AES-128-CBC
```

同时必须实现：

```go
func VerifyKey(key []byte, pageOne []byte) bool
```

验证：

```text
SQLite format 3
+
合法 B-Tree page type
```

不能简单地：

```text
抓到 16 byte
=
认为是 key
```

---

# 14. WAL Layer

必须从第一版设计。

正确链路：

```text
Encrypted DB
+
Encrypted WAL
    ↓
解析 WAL header
    ↓
解析 frame header
    ↓
page number / salt / commit
    ↓
逐页解密
    ↓
只合并完整 commit
    ↓
Plain SQLite Snapshot
```

不要直接把 WAL header 后面的内容按 4096 bytes 切片。

建议以 `yichen-wecom-local-vault` 的 WAL 处理设计作为主要参考，再用 Windows 实机数据验证。

---

# 15. Snapshot Layer

不要让 Agent 直接查询企业微信正在使用的源数据库。

推荐：

```text
企业微信源目录
    ↓
只读复制 DB / WAL 字节快照
    ↓
解密
    ↓
WAL Merge
    ↓
新建 plaintext snapshot
    ↓
PRAGMA quick_check
    ↓
readonly query
```

Snapshot 目录建议：

### Windows

```text
%LOCALAPPDATA%\wecom-local\
```

### macOS

```text
~/Library/Application Support/wecom-local/
```

统一内部：

```text
wecom-local/
├── state/
├── snapshots/
├── index/
├── logs/
└── tmp/
```

敏感信息注意：

```text
raw key 不写日志
聊天正文不进入普通 trace
明文数据库不进入 Git
```

---

# 16. Schema Layer

这是长期维护的关键。

不能把企业微信版本号写死成：

```text
if version == 5.0.3
```

应该采用：

```text
Schema Introspection
+
Capability Detection
```

启动后检查：

```sql
SELECT * FROM sqlite_master;
PRAGMA table_info(...);
```

然后生成：

```text
Schema Fingerprint
```

例如：

```text
tables:
  conversation_table
  conversation_user_table
  message_table
  message_small_table

columns:
  conversation_id
  sender_id
  content
  send_time
```

得到：

```text
schema fingerprint = xxx
```

再构建能力：

```go
type Capabilities struct {
    HasConversationTable bool
    HasConversationUsers bool
    HasExternalUsers     bool
    HasFileDB            bool

    MessageTables []string
}
```

Parser 根据 capability 工作，而不是版本号。

---

# 17. 统一领域模型

不要把 SQLite 表直接暴露给 Agent。

## Conversation

```go
type Conversation struct {
    ID          string
    Type        ConversationType
    Name        string
    LastMessage time.Time
}
```

## Member

```go
type Member struct {
    ID          string
    DisplayName string
    Alias       string
}
```

## Contact

```go
type Contact struct {
    ID          string
    DisplayName string
    Company     string
}
```

## Message

```go
type Message struct {
    ID             string
    ConversationID string
    SenderID       string

    Timestamp time.Time
    Type      MessageType

    Text       string
    Attachment *Attachment
}
```

---

# 18. Message Parser

第一期不要追求支持所有消息类型。

## P0

```text
文本
系统消息
图片占位
文件占位
语音占位
视频占位
```

统一输出：

```json
{
  "type": "file",
  "text": "",
  "attachment": {
    "name": "xxx.pdf"
  }
}
```

## 后续再扩展

```text
引用消息
富文本
应用卡片
审批
会议
位置
小程序
视频号
其他复杂消息
```

---

# 19. 对外接口

## 19.1 第一阶段：CLI

优先实现：

```bash
wecom-local doctor

wecom-local accounts

wecom-local conversations

wecom-local conversations --query "项目"

wecom-local history "研发群" --limit 100

wecom-local search "延期"

wecom-local members "研发群"

wecom-local contacts --query "张"
```

默认输出：

```text
JSON
```

CLI 可以参考：

```text
Luchioxy/wxwork-cli
BobbyCats/wecom-local
```

---

## 19.2 第二阶段：Local Service

增加：

```bash
wecom-local serve
```

只监听：

```text
127.0.0.1
```

例如：

```text
GET /v1/conversations
GET /v1/conversations/:id/messages
GET /v1/conversations/:id/members
GET /v1/contacts
GET /v1/search?q=xxx
```

最终：

```text
企业微信
    ↓
Collector
    ↓
Snapshot
    ↓
Local Runtime
    ↓
Agent Tool / MCP / Application
```

---

# 20. 增量读取

第一版不要直接做实时 Runtime Hook。

先做：

```text
DB mtime
WAL mtime
```

变化时：

```text
重新生成 Snapshot
或
增量读取 WAL
↓
更新索引
```

先做到：

```text
分钟级 / 秒级轮询
```

足够稳定后，再评估真正的：

```text
实时事件 / Runtime Hook
```

---

# 21. 搜索架构

## MVP

直接：

```text
SQLite Query
LIKE
time range
conversation filter
sender filter
```

## 第二阶段

使用：

```text
SQLite FTS5
```

## Agent 场景

后面再加：

```text
FTS
+
Vector Index
```

支持：

```text
keyword
semantic
time range
conversation
sender
```

---

# 22. 推荐开发顺序

## Phase 1：Shared Core

完全脱离真实企业微信客户端。

先做：

```text
wxSQLite3 crypto
key verify
WAL parser
snapshot builder
SQLite reader
schema introspection
message model
```

使用 synthetic fixture 测试。

目标：

```text
Windows / macOS 100% 共用
```

---

## Phase 2：Windows Adapter

接入：

```text
Account Discovery
Database Discovery
Permission Check
KeyProvider
Snapshot Source
```

验收：

```text
accounts
conversations
history
members
contacts
search
```

Windows 开源参考：

```text
wecom-reader
MindGraph WeCom
wecomcracker
wxwork-cli
```

---

## Phase 3：macOS Adapter

只新增：

```text
platform/darwin
```

核心层不改。

验收同 Windows：

```text
accounts
conversations
history
members
contacts
search
```

macOS 开源参考：

```text
yichen-wecom-local-vault
wecom-local
```

---

## Phase 4：Version Compatibility

至少准备：

```text
Windows 企业微信不同 5.x 版本
macOS 企业微信不同 5.x 版本

Windows x64
macOS Apple Silicon
macOS Intel（如果需要）
```

每个环境采集：

```text
client version
schema fingerprint
database list
crypto probe
WAL probe
message type sample
```

不要只记录“企业微信 5.0.x”。

---

## Phase 5：Agent Runtime

最后增加：

```text
Local HTTP
MCP
Agent Skill
Search Index
Incremental Update
```

---

# 23. MVP 验收标准

必须同时在 Windows 与 macOS 成功。

| 能力              | MVP  |
| ----------------- | :--: |
| 找到当前账号      |  ✅   |
| 找到数据库集      |  ✅   |
| 判断 DB 格式      |  ✅   |
| Key 验证          |  ✅   |
| 创建只读 Snapshot |  ✅   |
| WAL 最新数据      |  ✅   |
| 会话列表          |  ✅   |
| 群聊              |  ✅   |
| 单聊              |  ✅   |
| 历史文本消息      |  ✅   |
| 群成员            |  ✅   |
| 联系人            |  ✅   |
| 关键词搜索        |  ✅   |
| JSON CLI          |  ✅   |
| Local API         |  ✅   |
| Agent Tool        |  ✅   |

第一期不做：

```text
发送消息
自动回复
修改聊天
附件全量解密
语音转写
OCR
完整 CRM
审批
日程
```

---

# 24. P0 风险

## 24.1 单 Key / 多 Key

不同开源项目结论不完全一致。

必须实机验证：

```text
一个账号是否只有一个 global key
还是
不同数据库可能独立 key
```

架构提前支持两种。

---

## 24.2 WAL

这是最容易出现：

```text
“能查历史，但刚发的消息没有”
```

的地方。

必须单独做：

```text
WAL fixture
Windows real WAL
macOS real WAL
commit boundary
salt change
stale frame
```

测试。

---

## 24.3 Schema 演进

不能依赖固定列名。

必须使用：

```text
PRAGMA
sqlite_master
capability detection
```

---

## 24.4 消息二进制格式

AES 解密成功不等于消息解析完整。

真正长期工作量可能在：

```text
content
extra_content
local_extra_content
protobuf-like payload
不同 message type
```

---

## 24.5 平台权限

Windows 与 macOS 的主要差别，很可能最终集中在：

```text
process access
permission
sandbox
data directory
runtime access
```

因此必须隔离在 Platform Adapter。

---

# 25. 开源项目如何组合参考

不建议直接 fork 某一个。

建议各取所长：

```text
Windows Discovery / Reader
    → wecom-reader
    → MindGraph WeCom

Windows Snapshot / 安全边界
    → wecomcracker

CLI / Agent Interface
    → wxwork-cli
    → wecom-local

macOS Platform / DB / WAL
    → yichen-wecom-local-vault

Crypto
    → Windows + macOS 多项目交叉验证
    → 自己重写一套 cross-platform core

最终
    → Go Cross-platform WeCom Local Runtime
```

---

# 26. 最终推荐定位

不建议把项目理解成：

```text
WeCom Decrypt Tool
```

建议定位为：

```text
WeCom Local Data Runtime
```

最终结构：

```text
WeCom Local Runtime

Platform
├── Windows
└── macOS

Storage
├── Discovery
├── Crypto
├── WAL
└── Snapshot

Domain
├── Conversation
├── Message
├── Member
└── Contact

Query
├── History
├── Search
└── Incremental

Interface
├── CLI
├── Local API
├── MCP
└── Agent Tool
```

这样未来企业微信升级导致：

```text
数据目录变化
DB 加密变化
Schema 变化
```

也主要影响：

```text
Platform / Storage / Schema Adapter
```

不会影响 Agent、Query、上层业务。

---

# 27. 建议的第一版研发任务拆分

## Milestone 1：Shared Core

- [ ] wxSQLite3 page decrypt
- [ ] key verify
- [ ] SQLite page fixture
- [ ] WAL parser
- [ ] WAL merge
- [ ] Snapshot manifest
- [ ] SQLite readonly open
- [ ] Schema probe
- [ ] Capability model

## Milestone 2：Windows

- [ ] account discovery
- [ ] db discovery
- [ ] permission doctor
- [ ] key provider
- [ ] snapshot
- [ ] conversations
- [ ] history
- [ ] members
- [ ] contacts
- [ ] search

## Milestone 3：macOS

- [ ] account discovery
- [ ] db discovery
- [ ] permission doctor
- [ ] key provider
- [ ] snapshot
- [ ] conversations
- [ ] history
- [ ] members
- [ ] contacts
- [ ] search

## Milestone 4：兼容性测试

- [ ] Windows 多版本
- [ ] macOS 多版本
- [ ] Schema fingerprint
- [ ] WAL completeness
- [ ] Message fixtures
- [ ] regression tests

## Milestone 5：Agent Runtime

- [ ] JSON CLI
- [ ] Local HTTP
- [ ] MCP
- [ ] Agent Skill
- [ ] FTS
- [ ] incremental refresh

---

# 28. 参考仓库

## 官方

- WeCom CLI  
  https://github.com/WecomTeam/wecom-cli

## Windows

- wecom-reader  
  https://github.com/wangk-ask/wecom-reader

- wxwork-cli  
  https://github.com/Luchioxy/wxwork-cli

- WeComCracker  
  https://github.com/kizzhang/wecomcracker

- wechat-analysis-action  
  https://github.com/NOBB2333/wechat-analysis-action

- MindGraph  
  https://github.com/lycosa9527/MindGraph

## macOS

- yichen-skills / yichen-wecom-local-vault  
  https://github.com/mcncarl/yichen-skills

- wecom-local  
  https://github.com/BobbyCats/wecom-local

---

# 29. 下一步建议

如果正式开始落地，下一步不要继续做大范围调研，而应该进入：

```text
源码级拆解
+
真实版本验证
```

优先完成：

1. Windows 选 2 个真实企业微信版本
2. macOS 选 2 个真实企业微信版本
3. 记录真实数据目录
4. 记录 DB 列表
5. 生成 Schema Fingerprint
6. 验证单 Key / 多 Key
7. 验证 WAL 是否存在最新消息
8. 验证群成员表
9. 验证消息类型样本
10. 固化第一批 cross-platform fixture

完成这一步以后，再开始写 Go Core，会比直接照着某一个开源项目抄实现稳定很多。