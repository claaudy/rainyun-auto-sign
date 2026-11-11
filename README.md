# 雨云每日签到：纯 HTTP 宿主机入口

默认入口 `rainyun_signin.py` 只转交给 `rainyun_http.main()`；导入时不执行签到，命令行参数和退出码保持原样。不启动浏览器，不调用模型 API。旧浏览器实现仅保留在 Git 历史中，不再提供旧容器执行方式或自动回退。

## 运行边界与依赖

- **控制器必须由 root 在 Linux 宿主机运行**，使用 Python **3.10+ 标准库**；无需 `pip install`、`uv sync` 或安装浏览器。
- 宿主机 PID1 必须是 systemd，并提供可用的 `/usr/bin/systemd-run`、`/usr/bin/systemctl`、`/usr/bin/docker` 和 Docker daemon。控制器会为每个 worker 注册 root 系统管理器持有的独立清理计时器；这些临时清理单元不是签到调度任务。
- Node/CV 是不持有账号的隔离 Docker worker，只有公开验证码素材和只读脚本挂载。**禁止把宿主机控制器塞进持有账号的容器，再授予 privileged、Docker socket、systemd/D-Bus 或宿主机运行目录挂载。**
- 安装目录、源码及 `.runtime/` 必须归 root 所有，不可由其他用户修改。账号文件仅留在宿主机私有目录（目录 0700、文件 0600），不得加入仓库、构建上下文白名单、镜像或 worker 挂载。
- `pyproject.toml` / `uv.lock` 是保留的旧版依赖元数据，不用于新的宿主机标准库路径，也不用于 worker 构建；本次适配不修改它们。

安装时将以下运行文件放在同一目录：

```text
rainyun_signin.py
rainyun_http.py
tdc_runner.js
tenvision_raw.py
LICENSE-TenVision
```

镜像只提供运行时；脚本不会烘焙进去，控制器按次将必要脚本只读挂入隔离 worker。

## 构建无浏览器 worker 并固定镜像 ID

以下命令是部署者手动执行示例；此文档本身不会安装或启动任何服务。`/ABSOLUTE/INSTALL`、`/ABSOLUTE/PRIVATE_ACCOUNTS` 均为占位路径，不是本机路径，请替换成实际 root 私有路径。

```sh
# root 宿主机 shell，位于本仓库根目录
cd /ABSOLUTE/INSTALL

docker compose --profile build-only build rainyun-worker
# 不使用 Compose 时的等价构建：
# docker build -f Dockerfile -t signin-rainyun-http-worker:takeover .
# Dockerfile.worker 与默认 Dockerfile 完全相同。

WORKER_IMAGE="$(docker image inspect --format '{{.Id}}' signin-rainyun-http-worker:takeover)"
test -n "$WORKER_IMAGE"
printf '%s\n' "$WORKER_IMAGE"
```

把上述**完整 `sha256:...` 镜像 ID**固定在宿主机调用参数 / Hermes 调度配置中。不要在每次签到时重新解析可变标签；代码中的默认标签仅为兼容默认值，不是生产固定版本的替代品。运行使用 `--pull=never`，不会隐式拉取镜像。基础镜像标签仍可变，不能据此宣称跨次重建字节级可复现；每次重建应重新核验并固定实际 ID。

`Dockerfile` 与 `Dockerfile.worker` 只包含 Python、独立 Node 二进制及固定版本 `numpy==2.2.6` / `opencv-python-headless==4.12.0.88`，默认用户为 65534:65534，无浏览器或模型客户端。`.dockerignore` 默认排除全部文件，仅放行 `Dockerfile.worker`；Docker 会另外发送所选构建配方及忽略规则，不会因此发送账号或源码。配方也没有账号、源码的 `COPY` / `ADD`。

Compose **仅用于构建 worker 镜像**：唯一服务 `rainyun-worker` 位于 `build-only` profile，默认没有自动启用的服务，无账号、环境文件、socket 或宿主机卷挂载。即使显式运行该服务，默认命令也只会报错退出并提示改用宿主机入口；它不执行签到。不要使用旧的 `docker compose run --rm rainyun-signin`，也不要用 Compose 管理签到常驻服务。

## 宿主机账号与手动调用

在宿主机私有账号目录为每个账号创建一个非隐藏的普通 `*.env` 文件，例如：

```text
username=你的账号
password="你的密码"
```

也接受 `field` 代替 `username`，以及 `:` 分隔。只去掉匹配的外层引号，不执行 shell 展开、变量替换或命令替换；不要 `source` 账号文件，也不要把密码放入命令行。读取拒绝符号链接。目录枚举忽略隐藏文件、非 `.env` 文件及符号链接；同名账号的精确重复配置会匿名失败，不重新分配验证码预算。

```sh
# root 宿主机 shell；WORKER_IMAGE 为构建/核验时固定的完整镜像 ID
/usr/bin/python3 /ABSOLUTE/INSTALL/rainyun_signin.py \
  --env-dir /ABSOLUTE/PRIVATE_ACCOUNTS --worker-image "$WORKER_IMAGE"

# 单账号只读任务状态；仍可能触发登录验证码，但不会提交签到奖励
/usr/bin/python3 /ABSOLUTE/INSTALL/rainyun_signin.py \
  --env /ABSOLUTE/PRIVATE_ACCOUNTS/account1.env \
  --worker-image "$WORKER_IMAGE" --status-only

# 不读取账号、不启动浏览器、Docker 或 systemd 的帮助入口
/usr/bin/python3 /ABSOLUTE/INSTALL/rainyun_signin.py --help
```

不传 `--env` / `--env-dir` 时读取脚本同级 `accounts/` 下的非隐藏普通 `*.env`；生产推荐显式使用宿主机私有绝对路径。`--env` 与 `--env-dir` 互斥。

**项目不创建 cron、systemd 签到定时器、远程工作流或自动调度。签到日程由 Hermes 统一持有。** 旧执行器及旧调度的停用、入口替换、已安装路径验收和 Hermes 切换应由部署者协调完成，避免两套执行器并行；本仓库不会自动执行接管。

## 成功判定、预算与失败策略

- 宿主机 `urllib` 持有账号及每账号独立的内存 CookieJar，API / 验证码 CookieJar 分离，忽略环境代理。仅允许精确 HTTPS 来源，拒绝所有重定向；公开 `preload.js` 指纹变化时在提交密码之前失败。
- 每日任务 `Status` 必须是原始 JSON 整数 1 或 2，字符串、浮点数或布尔值不算有效状态。2 表示已签到，跳过奖励验证码和奖励 POST；1 才允许正常模式提交奖励。
- 提交后必须 GET 回读确认 `Status=2`。提交超时也先回读，**不盲目重发**；只有明确业务 `CommonCaptchaError=10004` 且回读仍为 1，才可在剩余预算内换新证明。普通错误不重试业务 POST。
- 登录与奖励共用每账号最多 **5 次新验证码**的本地预算，而非每阶段各 5 次；重复账号不获得新预算。每次换题间隔至少 2 秒，成功立即停止；provider code 50、受限本地匹配/TDC 失败才可在预算内换题。
- HTTP 429 / 明确 code 429 立即停止该账号并退避 60 秒；未知 provider code、未知 schema、未知指纹和不支持的证明策略均失败关闭，不猜测错误码，不调用模型或浏览器回退。
- 仅支持已实现的直接背景 `img_url`、原始解码尺寸及 1 个三联提示裁片或 3 个提示裁片。真实验证码匹配及服务端策略可能变化；离线通过不代表稳定成功率。
- Worker 网络关闭、只读根文件系统、UID/GID 65534、无 capabilities、no-new-privileges、资源限制、私有 tmpfs、无 Docker 日志，不挂载账号、宿主机环境、凭据描述符或 Docker socket。Node `vm` 不是安全边界，Docker 隔离才是。
- 每个 worker 启动前先确认 PID1 持有的 40 秒清理计时器。正常 / 超时后精确删除并确认容器不存在，再确认清理单元已停止或被回收。任何生命周期不确定性会保留 reaper、设置 `aborted=true` 并中止后续账号。
- HTTP 超时 30 秒、worker 超时 25 秒；PoW 最多两百万 nonce / 五秒。`.runtime/` 中目录 0700、文件 0600，正常及错误路径清理；公开 worker 输入可改属 sandbox UID。SIGKILL / 宿主机崩溃仍可能残留临时数据，应在 root 私有路径下人工审计，不能依赖应用 finally 处理所有崩溃。

## stdout 与退出码

除 `--help` 输出用法文本外，正常执行及受控错误的 stdout 为**单个 JSON 对象**：

- 固定元数据：`mode="multi_account"`、`transport="http"`、`browser_started=false`、`model_api_used=false`。
- 汇总：`total`、`success`、`failed`、`results`。结果仅以 `account` 序号标识，不输出账号或文件名。
- 结果状态为 `signed`、`already_done` 或 `error`；仅包含经过筛选的整数 `task.Status` / `task_after.Status`，以及固定 `error_stage`。不输出密码、cookie、CSRF、sess、ticket、TDC collect 或服务端响应体。
- 普通账号失败可继续后续账号；生命周期不确定时中止，`total` 仅计入已报告的结果行。
- 全部已确认签到 / 已签到退出 0；有失败、无账号或 `--status-only` 发现待签到均退出 1。只读待签到结果是 `error_stage="status_pending"`，不伪称成功。

## 离线测试与发布验收边界

在仓库根目录运行纯本地测试，明确关闭集成开关：

```sh
RUN_WORKER_INTEGRATION=0 /usr/bin/python3 -B -m unittest discover -v
```

根目录保留候选的 `test_rainyun.py`、`test_worker_integration.py`、`test_worker_lifecycle.py`，不搬入 `tests/`，因为相互导入和相邻素材路径依赖此布局。删除旧 `tests/test_rys_header.py`（该测试会真正启动浏览器），由 `test_entrypoint.py` / `test_release_recipe.py` 覆盖默认入口、main-only 参数转交、无第三方依赖的 import / `--help` 及构建配方边界。RYS 的纯本地覆盖仍保留在候选测试内。

候选代码已由父代理确认**本机全量 47 通过**（耗时 46.591 秒，无跳过），包含真实 Docker/systemd 的正常退出、超时和 SIGKILL 独立清理验证。核心 `.py`、worker 及辅助脚本在本次发布适配中逐字保持已审版本。

本次发布目录另行只做离线验证：**52 个发现用例，44 个通过，8 个真实 Docker/systemd 集成用例按要求跳过**，零失败；不把父代理的真实集成结果冒充为本次子任务执行。入口测试用隔离标准库 Python，阻断浏览器/模型库导入、网络、外部命令及账号读取。配方测试为静态检查和安全退出命令的本地执行，不等同于重新构建镜像或启动真实 worker。`.gitignore` 排除宿主机账号、`.env`、`.runtime/`、缓存、测试日志与验证码证明文件；它不能自动移除历史已追踪文件，发布者仍需审计待发布文件清单。

已安装的新入口、重建镜像及生产接管验收仍由部署者负责。注意 `test_worker_lifecycle.py` 的真实集成用例使用固定 `PINNED_IMAGE`，再次验收前必须核对它与目标镜像 ID 一致，不能用旧镜像通过冒充新镜像通过。离线合成素材不能作为雨云 API 证据，已签到账号的 status-only 也不能证明一次新的奖励提交。本次适配子任务未登录、未调用线上 API、未构建或运行 Docker、未操作 systemd、未替换生产入口、未修改任何日程。

## 许可与致谢

本项目的原有 MIT 许可及版权声明应继续保留。`tenvision_raw.py` 适配自 [TenVision](https://github.com/AmethystDev-Labs/TenVision) 的本地形状匹配代码，版权 **Copyright (c) 2026 Starry-Sky-World**；原始完整 MIT 授权文本逐字保存在根目录 `LICENSE-TenVision`，随代码一起分发，不得删除。感谢 TenVision 作者及 [linux.do](https://linux.do) 社区。
