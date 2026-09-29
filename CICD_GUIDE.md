# CI/CD 实施指南

> 更新日期：2026-09-29。流水线及部署代码已在本地 `main` 工作区实现；尚未提交、推送或在 GitHub/生产环境执行。下文保留原始估算，并补充实际使用方式。源码完成不等于线上已启用。

## 0. 当前实现与使用方法

### 版本与分支策略

- 直接使用 `main`，不自动创建分支、PR 或依赖更新提交。
- Python 限定 3.13 系列，镜像使用 `python:3.13-slim-bookworm`，跟随最新补丁。
- Node 使用 `node:lts-bookworm-slim` 跟随最新 LTS；uv、pnpm 使用最新稳定版本。
- 每轮 CI 运行 `uv lock --upgrade --python 3.13`，解析满足依赖约束的最新稳定组合；不承诺互相冲突的依赖都能采用各自最高版本。
- 本轮解析结果用于镜像构建和容器测试，CI 显式传入 `UV_SYNC_FLAGS=--locked`；发布直接转存已经测试的镜像，不再次构建或升级依赖。
- 仓库锁文件保留当前快照；CI 的新快照保存为 artifact，不自动写回 `main`。依赖解析或回归失败即阻止发布，不暗中降级绕过失败。
- GitHub Actions 使用当前主版本标签以接收兼容更新；新主版本仍需人工评估。此前规划中的固定构建工具版本要求，由本节用户指定的滚动策略替代。
- Action 标签须通过官方仓库的实际 Git refs 核实，不能仅凭网页内容推断。首次线上运行因不存在的 `astral-sh/setup-uv@v10` 在任务准备阶段失败，现已改为核实存在的 `@v7`；未指定 uv 的具体版本，仍默认安装最新 uv。`actionlint` 只做静态检查，不能证明远端标签可下载。
- 镜像 digest 只标识已构建的不可变产物，便于追溯与回退，不限制下一次发布使用新依赖。

### 三个 workflow

| 文件 | 触发 | 行为 |
| --- | --- | --- |
| `.github/workflows/ci.yml` | PR、手动、可复用调用 | 原生 ARM64 构建；运行两个服务测试及部署事务测试；保存镜像和依赖快照 |
| `.github/workflows/release.yml` | 推送 `main` 或 `vMAJOR.MINOR.PATCH` tag、手动、每周一 02:23 UTC | 调用 CI；通过后将同一批镜像推送 GHCR，保存 `release` artifact |
| `.github/workflows/deploy.yml` | `main` 手动触发 | 验证成功 Release 的来源，下载同批部署脚本和 Compose，通过 SSH 更新生产 |

每周触发时间是维护策略，非业务时限。生产不会随推送或定时发布自动更新。CI 测试用临时配置和 mock；MCP 另有本地真实 HTTP 握手测试，不调用模型或发送飞书消息。

镜像命名为 `ghcr.io/<owner>/<repo>/fersk-codex`、`ghcr.io/<owner>/<repo>/fersk-mcp`，仓库路径统一小写。标签包含 commit SHA、workflow run ID 和 run attempt，避免同一提交的依赖更新混淆。发布依据为 `release.json` 中的两个 digest 和 revision label；包内 `0.0.0` 后备版本不作为部署依据。

### 版本号发布

版本号由发布者通过 Git tag 指定；例如 `v1.0.0` 对应两个镜像的 `1.0.0` 标签。只接受无前导零的 `vMAJOR.MINOR.PATCH` 正式版本，不接受预发布或构建元数据。匹配 `v*` 但格式不合法的 tag 会在构建前失败。普通 `main` 推送、在 `main` 手动运行和定时运行仍仅生成追溯标签。

先将 workflow 修改合入 `main`，在需要发布的提交上执行（`v1.0.0` 为示例，请按实际发布版本填写）：

```bash
git tag v1.0.0
git push origin v1.0.0
```

Release 会重新调用 CI，并给同一批通过测试的镜像同时推送版本号标签和追溯标签，无需为两个标签分别构建。成功后可执行：

```bash
docker pull ghcr.io/monoacetylmorphine/fersk/fersk-codex:1.0.0
docker pull ghcr.io/monoacetylmorphine/fersk/fersk-mcp:1.0.0
```

以上镜像路径来源于当前仓库的 GHCR 命名。私有镜像须先完成 GHCR 登录。版本号不会自动递增，也不更新 `latest`。同一个 tag 的 Release 重跑会重新解析依赖，成功推送后版本号标签可能指向新镜像；精确复用产物应使用 digest。

Deploy 的来源限制保持不变：仅接受来自 `main` 的成功 Release run，不能使用 tag 触发的 run ID。tag 发布用于 GHCR 版本号拉取；生产部署继续选择 `main` 的 Release，两个独立 run 的依赖及 digest 不保证相同。

### 目标主机准备

已确认目标是当前 macOS 27 / Apple Silicon，使用 SSH、允许短暂停机；容器架构为 `linux/arm64`。

需要目标主机具备 Python 3.9+（部署脚本只使用标准库）、Docker 与支持 `up --wait` 的 Compose，以及已存在的 `ai-infra` 网络。应用容器内始终使用 Python 3.13。SSH 登录用户必须能访问该 Docker context、绑定目录，并已完成 GHCR 拉取授权。GitHub runner 必须能通过所配置的 SSH 地址访问目标主机。

在仓库之外准备一个独立 `DEPLOY_ROOT` 目录，并创建 `host.json`。以下为当前用户路径的示例，使用前确认目录实际存在；不应将生产配置复制进 Git 仓库：

```json
{
  "project": "fersk-app",
  "host_codex_dir": "/Users/fersk/.codex",
  "host_fersk_dir": "/Users/fersk/.fersk",
  "mcp_port": 8000
}
```

`project` 是新部署项目的稳定名称；`mcp_port` 是宿主机回环地址的端口，容器内为 8000。示例值来自现有应用目录约定和 MCP 默认端口，不是自动探测结果。`DEPLOY_ROOT` 必须为绝对路径，workflow 当前要求路径不含空格。脚本不生成配置、网络或凭据；缺少 `host.json`、挂载目录、`config.json`、`.env` 或必需飞书变量时停止。

`.fersk/config.json` 中数据库路径必须解析到容器 `/home/app/.fersk` 内，以支持一致性备份。发布镜像默认用户为 UID/GID 1000，预检会检查挂载目录可写；若主机权限不匹配，先解决目录访问，不自动递归修改所有权。Codex 沙箱的实际模型任务能力仍需在目标环境验证。

### GitHub 配置

在仓库创建 `production` Environment，配置以下值，必要时启用人工审批：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Secret | `DEPLOY_HOST` | GitHub runner 可访问的 SSH 主机名或 IPv4 地址 |
| Secret | `DEPLOY_USER` | SSH 用户名 |
| Secret | `DEPLOY_SSH_KEY` | 部署专用 SSH 私钥 |
| Secret | `DEPLOY_KNOWN_HOSTS` | 通过可信渠道核验的主机公钥记录 |
| Variable | `DEPLOY_ROOT` | 目标 Mac 上已准备好的部署状态目录绝对路径 |

SSH 当前采用标准端口 22，严格验证主机公钥，不自动信任现场 `ssh-keyscan` 结果。凭据只通过 Secrets 或目标主机环境提供，不提交真实值。GHCR 发布使用 workflow 的 `GITHUB_TOKEN`；目标 Mac 的私有镜像拉取登录独立配置，不从 workflow 自动写入凭据。

### 首次切换与后续发布

1. 将本次代码提交、推送至 `main`，等待 Release 成功。
2. 记录 Release run ID。`release` artifact 包含 `release.json`、对应版本的生产 Compose 和部署脚本；artifact 过期后需重新执行 Release。三个文件在发布包根目录并列存放：`release.json`、`compose.production.yaml`、`deploy.sh`。目录调整后需使用新结构的 Release artifact；旧结构的 artifact 不能交给当前 Deploy workflow。
3. 初次切换时，确认旧 Compose 项目名称和挂载目录，安排短暂停机，仅停止旧项目的 `fersk-codex`、`fersk-mcp`；保留旧容器和原有基础设施。若其他项目的应用仍运行，新脚本会拒绝接管。不要执行全栈 `down` 或删除数据卷。
4. 在 Actions 的 Deploy 中选择 `main`，填入成功的 Release run ID。只有确认数据和配置向后兼容，才勾选 `rollback_compatible`。
5. 部署脚本依次检查、拉取、停止已有受管应用、备份、启动并等待两个服务健康，成功后原子更新 `current.json`。

首次部署失败且无旧发布记录时，新应用会停止，需人工使用原部署配置恢复旧服务；不会自动接管未知容器。后续部署在兼容回退获准时可恢复旧发布；回退后 workflow 仍以失败结束，避免把新版本发布失败误报成功。回退也失败时保持失败状态并保留记录，需人工介入。

备份包含 SQLite 的一致性副本及 `config.json`，不复制凭据、不备份整个用户工作区，不自动恢复数据库。`DEPLOY_ROOT` 下保存发布目录、备份及 `current.json`，需按实际容量安排人工保留策略；脚本不自动删除历史记录。更新 host.json 会阻止自动发布，以免隐式迁移数据或端口。进程异常中断可能遗留 `.deploy-lock`，先确认没有部署进程、检查容器状态后再人工处理锁。

### 健康检查与停机边界

- Codex：主循环每秒写入无凭据的心跳，检查 SDK WebSocket 是否连接；十秒未更新即不健康。SDK 连接属性发生不兼容变化时失败关闭，不误报成功。
- MCP：八秒内完成本地协议握手和工具列表检查，不执行工具。
- SIGTERM：停止接收新消息，取消已排队的入站与缓冲，进入原有任务中断清理。SDK 线程由进程退出回收，不承诺所有外部请求都能优雅结束。
- Compose 提供 60 秒退出宽限和 180 秒部署就绪等待；这些是单机初始运维预算。检测到旧容器被 SIGKILL（exit 137）后停止新版本更新并尝试恢复旧容器。
- 不提供零停机、自动数据库迁移或完整外部服务端到端验证。

### 本轮本地验证记录

- 最新兼容依赖已在 Python 3.13.15 下重新解析，并更新两个仓库锁文件。
- 两个 Linux ARM64 Docker 镜像构建成功，实际构建中的 Node LTS 为 24.21.0、pnpm 为 12.6.0；这些仅是本轮观测版本，不是固定版本要求。
- Codex 全量回归 388 项通过；MCP 16 项通过，包含真实本地 HTTP 健康探测；部署事务 11 项通过，使用 Docker 替身验证失败和回退分支。
- 镜像内实际安装的 MCP 包在无源码挂载、无业务凭据、禁用外部网络的条件下启动，协议健康检查通过。
- workflow 的 actionlint、生产 Compose 配置解析、Shell/Python 语法及空白检查通过。
- 本轮实际涉及 21 个文件（含此前创建的指南）：新增 9、修改 12；较原估算多出的文件用于两项目的 Python 版本范围、锁快照和说明文档，MCP server 无需修改。
- 尚未验证 GitHub 托管运行、GHCR 推送、远端 SSH 和真实生产切换，也未运行真实飞书或模型调用。测试通过不代表这些外部步骤已完成。

以下章节保留分阶段设计和原始估算；实际文件及状态以上述实现说明和 Git diff 为准。

## 1. 推荐方案与收益

采用 **GitHub Actions + GHCR + Docker Compose**，按以下顺序推进：

1. CI：自动测试和构建，尽早发现回归。
2. 镜像发布：生成与源码提交对应的可追溯镜像。
3. CD：手动触发生产更新，验证就绪状态，并在兼容条件下回退。

对当前项目的主要收益：

| 项目场景 | 目标能力 | 收益 |
| --- | --- | --- |
| 修改会话、停止、重试或消息处理逻辑 | 自动运行现有离线测试 | 提前发现测试覆盖范围内的回归 |
| MCP 引用 Codex 共享源码 | 联动测试和构建两个项目 | 避免共享修改只验证一个服务 |
| 本地正常、容器构建失败 | CI 实际构建镜像 | 提前发现依赖、打包与符号链接问题 |
| 生产主机现场构建 | 拉取已经验证的镜像 | 减少发布耗时和构建环境差异 |
| 线上版本不明确 | 关联 commit SHA、镜像 digest 和发布记录 | 支持定位、审计和版本回退 |
| 容器启动但服务不可用 | 应用就绪检查 | 避免将进程存活误判为发布成功 |

CI/CD 不直接提升模型回答质量或运行速度。模拟测试不能证明真实飞书和模型接口始终可用；镜像回退也不能撤销数据库或配置变化。

## 2. 已检查的项目基础

- Git remote 指向 GitHub；规划检查时未发现已有 `.github` workflow。
- `fersk_codex`、`fersk_mcp` 均有 Dockerfile、`pyproject.toml` 和 `uv.lock`，要求 Python >= 3.13。
- 两个项目已有离线测试入口，使用临时配置和模拟客户端。
- MCP 通过相对符号链接引用 Codex 的共享配置及飞书请求模块；两个镜像以仓库根目录作为构建上下文。
- 根 `docker-compose.yaml` 同时管理应用与 Langfuse 等基础设施，两个应用尚未配置 `healthcheck`。
- 应用通过绑定挂载使用持久化配置、SQLite 和用户工作区，并依赖外部 `ai-infra` 网络。
- Codex 网关已有退出清理逻辑，但尚未在本次工作中验证容器 SIGTERM、WebSocket 线程退出和在途任务收尾。
- Docker 构建输入排除了 `.git`；项目文档说明缺少版本元数据时可能回退为 `0.0.0`。

以上为最初规划阶段的源码及配置检查记录，当时未执行测试或构建。开发阶段的实际验证结果见第 0 节；其他测试文档中的历史通过记录不计入本轮结果。

## 3. 估算口径与范围

- 行数包括 Python、Shell、YAML、测试和文档；已有文件仅计算预计新增或修改行数，不计算整个文件长度。
- 文件数跨阶段去重；行数为各阶段预计工作量，不是最终文件总行数或精确 diff 统计。
- 所有行数区间来自对当前结构的工程估算，未经实现验证，不是承诺。
- 第一阶段预计复用现有测试与 Dockerfile；若发现既有失败，修复范围另行列出。
- 第三阶段原按单台 Linux 主机估算；现已确认 macOS 27 / Apple Silicon 的 Linux ARM64 Docker、SSH 和短暂停机。
- 不包含 Kubernetes、零停机、数据库迁移、全仓格式化、全量类型标注和无关重构。
- 原规划的 `docs/CICD.md` 由根目录本文档 `CICD_GUIDE.md` 承担，不再额外创建重复指南。
- 本文档持续更新；下表保留完整方案的基线估算，后续不新增重复指南。

## 4. 第一阶段：自动测试与构建

### 文件清单

| 文件 | 基线操作 | 内容 | 预计行数 |
| --- | --- | --- | ---: |
| `.github/workflows/ci.yml` | 新增 | 安装锁定依赖、两个项目测试、两个镜像构建 | 90～130 |
| `CICD_GUIDE.md` | 新增，本文已完成 | CI 说明、实施及排查指南 | 50～80 |
| `README.md` | 修改 | 增加 CI 说明和指南入口 | 10～20 |
| **合计** | **新增 2，修改 1** | | **150～230** |

表中文档行数为原始估算；本文同时覆盖后续阶段，实际长度可能超出第一阶段文档预算。

### 实施要求

1. Pull Request 和默认主分支提交触发；默认分支名称在实施时确认，不硬编码猜测。
2. 使用 Python 3.13，分别安装两个项目的锁定依赖，保持依赖环境独立。
3. 优先复用现有测试入口，不新增重复测试框架。以下为计划采用的入口，本次未执行：

   ```bash
   # 工作目录：fersk_codex；使用该项目依赖环境
   python -B tests/run_tests.py

   # 工作目录：仓库根目录；使用 fersk_mcp 依赖环境
   python -B fersk_mcp/tests/test_runtime.py
   ```

4. 检查共享符号链接及配置回归，以仓库根目录为上下文构建两个镜像。
5. 初期每次验证两个项目，暂不加入复杂的路径过滤；后续如优化，必须覆盖共享依赖和 workflow 自身变更。
6. PR 不获取生产凭据，不推送发布镜像，不执行部署；测试日志不得包含真实凭据。
7. 测试或构建失败应阻止对应检查通过，不以忽略失败或无条件重试掩盖问题。

### 验收标准

- 干净 checkout 可以安装锁定依赖、运行两个测试入口并构建两个镜像。
- 测试或构建失败返回失败状态，日志能够定位失败步骤。
- 不依赖开发机 `.env`、生产目录或真实外部服务凭据。
- 核实 `fersk_codex/tests/README.md` 记录过的 SQLite WAL 锁竞争问题是否仍存在；不得将历史失败直接设为豁免。

## 5. 第二阶段：镜像发布

### 文件清单

| 文件 | 操作 | 内容 | 预计行数 |
| --- | --- | --- | ---: |
| `.github/workflows/release.yml` | 新增 | 验证后推送两个镜像，记录版本及 digest | 70～110 |
| `fersk_codex/Dockerfile` | 修改 | 滚动版本策略、CI 快照参数和镜像元数据 | 8～18 |
| `fersk_mcp/Dockerfile` | 修改 | 滚动版本策略、CI 快照参数和镜像元数据 | 8～18 |
| `CICD_GUIDE.md` | 追加 | 发布方式、镜像命名、版本追溯 | 20～40 |
| **本阶段合计** | **新增 1，修改 3** | | **约 110～190** |

### 实施要求与验收

- 仅发布通过同一源码提交验证的镜像；发布任务必须明确依赖验证成功，不能与验证结果脱节。
- 两个服务关联同一 commit SHA，并记录各自不可变 digest；生产部署按 digest 选择镜像。
- 镜像标签、源码 revision 和 digest 分别记录。Codex 镜像直接运行源码，包版本后备值不作为发布依据；不能假定设置构建参数就会自动产生可查询的包版本。
- 按用户要求保留滚动的 uv、Node LTS 和 pnpm；通过每轮测试以及记录镜像 digest 控制发布风险。
- 不承诺完整可复现构建；系统包和用户工作区首次解析的依赖仍需单独考虑。
- PR 保持只读权限；只有受信任的发布任务获得 GHCR 写入权限。Actions 当前跟随兼容主版本标签。
- 发布结束能查到源码提交、两个镜像 digest 和验证结果；失败不进入部署。

## 6. 第三阶段：受控部署与回退

### 文件清单

| 文件 | 操作 | 内容 | 预计行数 |
| --- | --- | --- | ---: |
| `compose.production.yaml` | 新增 | 应用镜像、挂载、网络及健康检查 | 60～90 |
| `.github/workflows/deploy.yml` | 新增 | 手动选版本、生产环境串行部署 | 60～100 |
| `deploy.sh` | 新增 | 预检、拉取、备份、更新、检查和受限回退 | 120～200 |
| `fersk_codex/utils/health.py` | 新增 | 网关本地就绪状态和探测 | 60～100 |
| `fersk_codex/main.py` | 修改 | 就绪状态、SIGTERM、停止接收新任务 | 50～100 |
| `fersk_mcp/utils/health.py` | 新增 | MCP 协议初始化及工具注册检查 | 30～60 |
| `fersk_mcp/server.py` | 按需要修改 | 配合探测的生命周期或状态处理 | 10～30 |
| `fersk_codex/tests/test_gateway_startup.py` | 修改 | 就绪、退出信号及任务清理回归 | 60～100 |
| `fersk_mcp/tests/test_runtime.py` | 修改 | 探测成功、失败和超时回归 | 40～80 |
| `test_deploy.py` | 新增 | 缺少配置、部署失败和回退失败分支 | 60～100 |
| `CICD_GUIDE.md` | 追加 | 实际部署、备份恢复和回退限制 | 40～60 |
| **本阶段合计** | **新增 6，修改 5** | | **约 690～1,120** |

### 部署流程

```text
选择已经验证的发布版本
  → 校验必需变量、挂载路径、权限、网络和宿主机能力
  → 拉取两个镜像并记录当前版本
  → 停止接收新任务，等待或有界终止在途任务
  → 创建一致的必要状态备份
  → 更新两个应用服务
  → 执行有超时的就绪检查
  → 成功：记录发布完成
  → 失败：判断数据兼容性，按记录的旧 digest 回退
  → 回退失败或不兼容：停止自动操作并明确报告
```

### 关键约束

- 初期手动触发，同一生产环境串行部署；不要中途自动取消正在修改生产状态的任务。
- 新 Compose 仅管理两个应用，明确项目名与服务所有权。与现有根 Compose 的首次衔接须制定一次性切换步骤，避免两个网关同时消费消息或争用端口。
- 不因应用发布而升级 Langfuse、PostgreSQL、Redis、ClickHouse 或其他基础设施。
- 网关探测应反映初始化、连接和接收任务状态；MCP 探测验证协议及工具注册。探测不调用收费模型、不发送真实消息。
- 不把进程存活、端口开放或历史日志当作完整就绪依据；健康检查不自动等于自动恢复机制。
- 网关当前在线程中运行 WebSocket。若实现可靠退出需要修改客户端、任务调度或其他模块，必须先更新变更清单并获得相应范围批准。
- 按目标架构构建和验证镜像；不能用历史 arm64 验证推断 amd64 也可用。
- 宿主机需满足非 root 运行、目录 UID/GID、挂载、外部网络和 Codex 沙箱要求，不能通过流水线擅自修改内核安全策略。
- SQLite 备份使用一致性备份机制或在确认写入停止后处理，不能在 WAL 写入期间只复制主数据库文件。
- 镜像回退不回退数据。数据库或配置格式不向后兼容时，不自动切回旧镜像，不自动覆盖恢复用户数据。
- 根 Compose 及 `compose_initial.sh` 初期保持不变；缺少必需环境变量时停止并报告，不调用初始化脚本生成替代凭据。

### 验收标准

- 成功路径完成指定版本更新，能验证两个服务就绪并记录 digest。
- 缺少配置、挂载或权限时，在修改生产状态前失败。
- 在途任务退出行为、部署超时、兼容版本回退和回退失败均有可核验结果。
- 生产配置、用户工作区及基础设施数据不被隐式覆盖或删除。
- 在隔离环境验证部署流程后，再根据批准范围操作生产环境。

## 7. 累计工作量与仓库外配置

| 完成范围 | 累计文件数，去重 | 累计预计变动行数 |
| --- | ---: | ---: |
| 自动测试与构建 | 3 | 150～230 |
| 加上镜像发布 | 6 | 约 260～420 |
| 加上受控部署与回退 | 16 | 约 950～1,550 |

完整基线包括新增 9 个文件、修改 7 个已有文件；新增文件数包含本文。第三阶段为设计估算，尤其是网关退出和健康状态的实际涉及文件仍可能调整。

不计入代码量的工作：

- GHCR 包权限、仓库 workflow 权限和镜像保留规则。
- GitHub Secrets、Environment、分支保护与必需检查；保护能力取决于仓库可见性和账户方案。
- 部署账户、服务器连接方式、镜像拉取授权及宿主机准备。
- 凭据扫描：仓库跟踪了名为 `configs/observability/prometheus/openclaw-gateway-token` 的文件。本次未读取内容，不能判断是否包含有效凭据；实施前须核实，真实凭据不得写入代码或日志。

生产主机系统、架构、SSH 和短暂停机已确认。SSH 地址、密钥、主机公钥及部署状态目录尚需按第 0 节配置，本次未连接或变更生产服务。

## 8. 执行边界与推荐下一步

本轮已根据后续授权开发三个阶段，并按用户要求调整 Python/Node 的滚动版本策略。另更新了两个项目的 `pyproject.toml`、`uv.lock` 和服务 README；MCP 协议探测可以独立实现，因此不需要修改 `fersk_mcp/server.py`。

尚需完成的线上步骤是：提交并推送 `main`、配置 GitHub `production` 的 Secrets/Variable、准备目标主机状态目录、验证 GitHub runner 的 SSH 连通性，以及安排首次切换。缺少实际部署配置时不尝试发布到生产。

本地开发授权不等于已执行生产切换。后续仍须复核工作区和文件范围；涉及删除、配置覆盖、公共接口变化、跨模块行为变化或超出批准文件范围时，先列明方案并取得明确批准。凭据只能由环境或 Secret 管理提供，缺少必需变量时立即停止。

## 9. 官方参考

- [GitHub Actions 发布 Docker 镜像](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)
- [GitHub 部署环境与保护规则](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
- [Docker Compose 生产部署](https://docs.docker.com/compose/how-tos/production/)
- [uv 依赖锁定、同步与升级](https://docs.astral.sh/uv/concepts/projects/sync/)
