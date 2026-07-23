# 用户无感的 Selena 识别边界

Web、YAML 和 Python SDK 都不接受 `project`、内部适配器名或 Windows
执行组件 ID。识别只发生在能读取用户路径的执行节点上，识别结果属于调度
实现，不能成为用户配置。

## 本地编译

用户提供代码仓、Selena 编译脚本和软件包编译脚本。系统优先使用已验证的
脚本/工作区证据匹配内置适配器；没有匹配时使用
`WorkspaceRecognizer` 生成稳定的 `workspace-<hash>` 内部命名空间和
`generic:selena-script` 构建合同。这个命名空间不含用户路径或产品名称，
也不要求管理员登记新产品。

软件包脚本用于发现构建依赖和非交互准备步骤，不代表系统可以仅凭路径名称
推导产品。

## 已有 Selena

用户必须提供 Selena 文件夹、Runtime XML、MatFilter，以及产品需要时的
Adapter；还可以提供代码仓路径、Selena 编译脚本和软件包编译脚本作为产品
识别证据。选择“已有 Selena”时这些脚本只用于识别和交叉校验，绝不会触发
代码切换或编译。系统必须先验证：

- 文件夹内只有一个可选中的 `Selena.exe`；
- `Selena.exe` 同目录存在 DLL 依赖；
- Runtime 是有效 XML；
- MatFilter 和 Adapter 由统一配置资产流程校验、上传。

系统复用本地编译使用的 `WorkspaceRecognizer` 解析代码仓和两个脚本，不
另建一套产品规则；同时独立检查 Selena 文件夹、Runtime 和 DLL 的明确产品
标记。只有 `bydod25`、`byd_od25`、`g3n_fvg3_od25`、`ovrs25`、
`byd_ovs`、`ovrs` 这类明确证据可以选择现有的 BYD/OVRS 内置适配器。
`od25`、`ovs`、`Xpeng`、`GAC` 或任意父目录名都不是充分证据。

代码/脚本证据和 Selena/Runtime 证据识别为同一产品时采用该内置适配；
只有一侧能识别时采用可证明的一侧；两侧识别为不同产品、脚本位于代码仓
之外或多个适配器同时匹配时立即阻止任务，并提示用户确认所有路径属于同一
产品。不得选择其中一个继续执行。

所有证据都不足时，系统才根据 Selena、Runtime 和 DLL 的内容指纹生成稳定
的 `workspace-<hash>`，内部合同为 `generic:existing-selena`。同一套产物
移动到另一个目录后身份不变；内容变化后身份变化。系统不会拼接 `od25`，
也不会伪造不存在的 `project:<name>`。

## Generic Cluster 路由的发布边界

`workspace-<hash>` 表示“未登记但已验证的工作区/Runtime Bundle”，不表示
系统已经识别出任意产品的业务语义。Cluster 路由仍必须满足以下管理员侧
条件：

- `core.cluster` 的默认 Cluster manager、软件共享、workspace、group 和
  subgroup 适用于该部署；
- `$RSIM_HOME/config/deployment.yaml` 提供 Linux 可访问的共享盘挂载映射；
- Runtime、MatFilter、Adapter 和数据全部来自本次用户配置并通过资产流程
  解析；
- preflight 对 Runtime/配置/数据兼容性给出最终结论。

当前测试服务器的 `job_0be20501b3a8` 使用
`workspace-179115fb770c9c5175ea5974` 完成了构建、Bundle 上传和 Cluster
提交。服务器没有同名 `config/projects/<workspace>/config.yaml`；它实际
使用通用 Cluster 默认值与 `deployment.yaml` 的挂载映射，并在 preflight
前用本任务的 Runtime、MatFilter、Adapter、数据和 Bundle 覆盖运行配置。
这证明通用命名空间可走当前 Cluster 基础设施，但不等于所有未知产品都已
完成仿真验收。

如果目标服务器的通用 Cluster 默认值或挂载映射不可用，任务应在 Cluster
环境检查或 preflight 阶段返回缺失的软件共享、workspace、配置资产或兼容性
错误；不得回退到猜测的产品适配器。

## Windows 一键连接的用户边界

普通用户只看到“等待连接本机”和“一键连接本机”。已有 Selena 表单会把
代码仓和两个编译脚本显示为可选的“产品识别证据”，但仍不显示或要求用户
选择项目；导入 YAML 后这些非空证据会原样保留。Web 根据任务需要在内部
选择本机编译/上传能力或本地仿真能力，下载文件也自动绑定当前 Linux 服务。
`light`、`full`、服务地址、节点 ID 和认证信息只允许出现在内部请求、安装
模板或管理员文档中，不进入任务表单或 YAML。当前一键连接用户界面符合此
边界；`bootstrap.ps1` 仍只作为安装器内部和管理员恢复入口。
