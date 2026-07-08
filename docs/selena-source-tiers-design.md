# 设计：三档 Selena 来源场景（cluster 服务化）

> 状态：T3 核心已实现（server 端 cluster 执行器）。T2（Selena 上传）待实现。T1 已实现（模式 B）。
>
> 起因：用户明确 Linux 提供 cluster 仿真服务时，不同用户具备的条件不同，需按三档支持，降低每档用户的依赖。

## 背景

Selena 是 Windows PE 二进制，仿真执行在 SZHRADAR 集群节点（Windows worker，预装 MATLAB/Qt/Boost）。用户机器的依赖取决于"Selena 从哪来"和"谁打包提交 job"。当前架构（模式 A）让 Windows agent 承担打包+提交，因此即使无本地仿真环境，仍需一台 Windows 机器跑 agent。

目标：按用户具备的条件分三档，每档只要求最小依赖。

## 三档用户场景

| 档位 | 用户具备 | 缺失 | 需要的能力 |
|------|---------|------|-----------|
| **T1** | Windows + 代码仓 + 编译能力 + 本地仿真环境（MATLAB/Qt/Boost/VS） | 无 | 本地编译 + 本地仿真 + cluster 仿真 |
| **T2** | 电脑 + 代码仓 + 编译能力（MSVC/MATLAB/Qt/Boost） | 本地仿真环境（不跑 selena.exe） | 自动编译 Selena → 上传集群共享 → cluster 仿真 |
| **T3** | 电脑（浏览器/curl） | 代码仓、编译、仿真环境 | 配置/选择 Selena 路径 → 全链路 cluster 仿真 |

### 关键区分

- **T1 vs T2**：T1 本机能跑 selena.exe（有完整运行时 DLL + 工具链），T2 只能编译产出 selena.exe 但不在本机跑仿真。
- **T2 vs T3**：T2 有编译能力（能产出新 Selena），T3 完全不编译（用现成 Selena）。
- **T1 是现有模式 B**（已实现），T2/T3 是新模式（待实现）。

## 各档链路拆解

### T1：完整本地环境（已实现 = 模式 B）

```
Windows 用户机（代码仓+编译+仿真环境）
  ├─ rsim build selena              → 本机编译产出 selena.exe
  ├─ rsim run <MF4> --profile local-build   → 本机 selena.exe 跑仿真（本地仿真）
  └─ rsim cluster run ... --profile cloud-build  → 打包本机 selena + 提交集群（cluster 仿真）
```
- 本地仿真：selena.exe 在本机跑，读本机 MF4。
- cluster 仿真：`source=build`，`--copy-selena` 把本机 selena.exe+DLL 复制进 job 包，提交集群。
- **依赖**：MATLAB/Qt/Boost/VS（编译+运行时）。
- **状态**：✅ 已实现（`scripts/bootstrap.ps1` + `rsim doctor` + 现有 build/run/cluster 命令）。

### T2：有代码仓有编译能力，无本地仿真环境（待实现）

```
用户机（代码仓+MSVC/MATLAB/Qt/Boost 编译工具链，但不跑 selena.exe）
  └─ rsim build selena              → 本机编译产出 selena.exe
     + 自动上传 selena.exe+DLL 到集群共享路径（新增）
     + 生成/更新一个指向该共享路径的 cluster profile（新增）

Linux server（或任意能访问集群共享的机器）
  └─ 接收 cluster.run job（payload 指向上传后的共享 Selena profile）
     → server 端打包+提交（新增：server 端跑 prepare_cluster_job + submit_cluster_job）
     → 集群节点用共享 Selena 跑仿真
```

- **编译**：用户机有 MSVC+MATLAB+Qt+Boost，能跑 `rsim build selena`（这步需要完整工具链，和 T1 一样）。
- **不上传代码到集群**：只上传 selena.exe + 依赖 DLL 到集群共享路径（如 `\\abtvdfs2\...\\Cluster\BYD_OVRS\<user>\<branch>\`）。
- **仿真不在本机跑**：用户机不调 selena.exe 跑 MF4（区别于 T1 的本地仿真）。
- **cluster 提交可在 Linux server 做**：因为 Selena 已在共享路径，server 端能直接打包+提交，不需要 Windows agent。
- **需要新增**：
  1. `rsim build selena --upload-to-cluster`（或单独 `rsim selena upload`）——编译后上传 selena.exe+DLL 到集群共享，返回共享路径。
  2. 上传后自动生成/更新 cluster profile（`source=path`，`exe=<共享路径>`），或在 config 里记录"最近上传的 Selena"。
  3. server 端 cluster 提交能力（见下"server 端打包提交"）。
- **依赖**：编译工具链（MSVC/MATLAB/Qt/Boost），但**不需要本机跑 selena.exe 的运行时**（编译产物直接上传）。实际编译工具链和运行时 DLL 是同一套，所以 T2 的工具链依赖 ≈ T1，只是用法不同（编译后上传而非本机跑）。

### T3：无代码仓无编译无仿真环境（待实现）

```
用户（浏览器/curl，只有电脑）
  ├─ web 前端：选择 Selena 来源
  │   ├─ 从预置共享 Selena 列表选（如 byd-ovrs-bl01v7-er-shared）
  │   └─ 或上传自己已有的 selena.exe（新增：上传到集群共享/server）
  ├─ 选择/提供数据（dataset 或 MF4 UNC 路径）
  └─ 提交 cluster.run job

Linux server
  └─ 打包+提交（server 端 prepare_cluster_job + submit_cluster_job）
     → 集群节点用选定的共享 Selena 跑仿真
     → 结果回传 server，用户在 web 查看
```

- **完全不编译**：用户从 server 维护的"共享 Selena 列表"选一个，或上传自己的 selena.exe。
- **server 端打包提交**：T3 用户没有 agent，server 必须自己完成打包+提交。
- **需要新增**：
  1. server 端 cluster 提交能力（同 T2）。
  2. server 端维护"可用共享 Selena 列表"（扫描集群共享路径 + config 里预置 + 用户上传的）。
  3. web 前端 Selena 选择/上传 UI。
  4. （可选）selena.exe 上传 API + 存储。

## 核心新增能力（T2/T3 共需）

### 1. server 端 cluster 打包+提交

当前 `cluster.run` task 由 agent 端执行（子进程调 `rsim cluster run`）。T2/T3 没有 Windows agent，需让 **server 端直接跑** `prepare_cluster_job` + `submit_cluster_job`。

两种实现路径（待 review 选定）：
- **A. server 内置 cluster 执行器**：server 进程内直接 import `core.cluster.prepare_cluster_job`，认领 cluster.run task 后本机执行（不派发给 agent）。需要 server 能访问集群共享路径 + XML-RPC。
- **B. server 起 Linux 内置 agent**：server 启动时同机起一个 agent（Linux），agent 跑 `rsim cluster run`。复用现有 agent 机制，但 Linux 上需要 cluster 链路代码 + PyYAML + 集群共享访问。

### 2. Selena 上传到集群共享（T2）

`rsim build selena` 完成后，把 `build_output/selena.exe` + 依赖 DLL 复制到集群共享路径（如 `\\abtvdfs2\...\\Cluster\BYD_OVRS\<user>\<branch>\<timestamp>\`），返回 UNC 路径。可复用现有 `_copy_selena_runtime` 逻辑（目标改成集群共享而非 job 包）。

### 3. 共享 Selena 目录管理（T3）

server 扫描集群共享路径下的 Selena 包，维护一个列表（name → UNC exe 路径 + runtime_xml + 元信息），供 T3 用户选择。config 里 `cluster.profiles` 已有历史共享包（`byd-ovrs-bl01v7-er-shared` 等），可作为初始列表。

### 4. 配置精简

- T2/T3 不需要 `local.yaml`（无本机工具链路径）。
- T2 的编译工具链路径仍需配置（MSVC/MATLAB/Qt/Boost）——可用 `bootstrap.ps1` + `doctor` 辅助。
- T3 完全只需 server 端 config（集群共享 + Selena 列表 + dataset），用户侧零配置。

## 各档依赖矩阵

| 依赖 | T1 | T2 | T3 |
|------|:--:|:--:|:--:|
| Windows 机器 | ✅（本机仿真要） | ⚠️（编译要 MSVC，可 Windows） | ❌（浏览器即可） |
| 代码仓 | ✅ | ✅ | ❌ |
| MSVC 编译工具链 | ✅ | ✅ | ❌ |
| MATLAB/Qt/Boost（编译） | ✅ | ✅ | ❌ |
| 本机 selena 运行时（跑 MF4） | ✅ | ❌ | ❌ |
| 集群共享访问 | ✅（cluster 时） | ✅（上传+提交） | ✅（server 端） |
| Windows agent | ✅（本地+cluster） | ❌（server 端提交） | ❌（server 端提交） |
| Linux server | 可选 | ✅（提交） | ✅（提交+web） |

## 待 review 的关键决策点

1. **server 端 cluster 执行路径**：内置执行器（A）还是 Linux 内置 agent（B）？A 更直接但要改 control_service；B 复用 agent 但 Linux 上跑 cluster 链路需验证（XML-RPC、共享路径写权限）。
2. **T2 编译工具链**：MSVC/MATLAB/Qt/Boost 是否必须 Windows？能否在 Linux 交叉编译 Selena？（Selena 是 Windows PE，交叉编译难度大，倾向 Windows 编译机）
3. **Selena 上传格式**：只传 selena.exe，还是 exe+DLL 整个 runtime 目录？runtime_xml/matfilefilter/adapter 是否一起传？（倾向整个 runtime 目录 + 配套 assets）
4. **共享 Selena 列表来源**：扫描集群共享目录（动态）+ config 预置（静态）+ 用户上传（动态），三者合并？
5. **T3 用户上传 selena.exe**：上传到 server 再转存集群共享，还是直接上传到集群共享（需要 server 有写权限）？
6. **T2/T3 的 server 端提交**：server 进程要能访问 `\\abtvdfs2\...`（集群共享）和 `szhradar01:8123`（XML-RPC）。Linux server 上 SMB 挂载 + 网络可达需确认。
7. **配置有效性校验**：T2 校验编译工具链 + 上传路径；T3 校验所选 Selena 路径可达 + dataset 在共享上。doctor/check 需扩展。

## 现有可复用资产

- `core/cluster.py:prepare_cluster_job` / `submit_cluster_job` —— 打包+提交核心逻辑。
- `core/cluster.py:_copy_selena_runtime` —— 复制 selena runtime（T2 上传可复用）。
- `core/profiles.py` —— profile 机制（source=path/exe 字段已支持共享 Selena）。
- `config.yaml:cluster.profiles` —— 历史共享 Selena 包列表（T3 初始列表）。
- `rsim doctor --backend cluster` —— cluster 环境校验（已实现，可扩展校验 server 端提交能力）。
- `core/control_service.py` —— job/task 队列（server 端执行器可挂在 task 认领后）。

## 下一步

1. Review 本文档，确认三档场景和依赖矩阵准确。
2. 决策上述 7 个关键点。
3. 制定分档实现方案（T3 最简先做？T2 次之？T1 已完成）。
4. 用实际数据验证各档端到端。
