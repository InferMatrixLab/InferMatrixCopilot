---
title: "vLLM-Router 发布规范"
created: 2026-11-06
updated: 2026-11-09
type: guide
tags: [vllm-router, ci]
sources: [.buildkite/release-pipeline.yml, .buildkite/scripts/build-wheel.sh, .buildkite/scripts/publish-release-images.sh, .buildkite/scripts/cleanup-nightly-router-builds.sh, .buildkite/README.md, docker/Dockerfile.router, pyproject.toml, Cargo.toml, py_src/vllm_router/version.py, docs/benchmarks/router_overhead.md, tests/python/e2e/perf/grpc/README.md, tests/python/e2e/perf/l1_encode/README.md, tests/python/e2e/pd_disagg_vllm/test_lm_eval_accuracy.py, .github/ISSUE_TEMPLATE/200-bug-report.yml, "vllm-project/router@ddf78218cffdb6361579d3babd69a90ba6d63aaa", "https://github.com/vllm-project/router/releases/tag/v0.1.17"]
---

# vLLM-Router 发布规范

发布由 tag 驱动：`.buildkite/release-pipeline.yml`（Buildkite `router-release`）只负责
tag→产物的机械门禁，测试矩阵、质量门槛和 GitHub Release 正文都要维护者举证或手写。
两者必须分开报告，不能把 `release` 流水线通过当成整轮质量验证。

相关入口：[CI 入口](_index.md)、[仓库入口](../_index.md)、[通用 CI](../../../general/ci/_index.md)、
[DI 计算说明](../../../general/bug/guides/di-calculation.md)、[benchmark 合同](../../../general/benchmark/guides/benchmark-contract.md)。

## 版本号与 tag

| 项 | 规定 |
|---|---|
| tag 形态 | `vX.Y.Z`（如 `v0.1.17`）；流水线用 `build.tag =~ /^v.*/` 触发 |
| 版本唯一来源 | `pyproject.toml` 的 `project.version`；`version-check` 只把 tag 去掉 `v` 后与它比较 |
| 必须同步的文件 | `pyproject.toml`、`Cargo.toml`、`Cargo.lock`、`py_src/vllm_router/version.py`；`v0.1.17` 的 bump commit 同时改这四份，tag 指向该 commit |
| 包名与入口 | PyPI 包 `vllm-router`，模块 `vllm_router`，console script 与 Rust 二进制都叫 `vllm-router` |
| 产物范围 | 仅 Linux：`cp38-abi3` 的 manylinux_2_28 x86_64 与 aarch64 wheel，加 sdist；`requires-python = ">=3.8"`，一个 abi3 wheel 覆盖 3.8+，没有 macOS/Windows wheel |
| tag 类型 | 历史上 annotated（`v0.1.16`、`v0.1.16.1`）和轻量（`v0.1.17`）都出现过；类型不影响触发，但 tag 不能指向 bump 之前的 commit |
| 四段版本 | `v0.1.16.1` 的 `pyproject.toml` 仍是 `0.1.16`，而 `version-check` 在 `v0.1.16` 时就已在 release pipeline 里，所以这个 tag 的普通构建过不了该门禁；它只出现在手动 Docker 通道，PyPI 与 GitHub Release 都没有这一版 |

### tag、PyPI、镜像、Release 要分别核对

`v0.1.11`、`v0.1.13`、`v0.1.14` 只有 tag；`v0.1.16.1` 只有 Docker 镜像；GitHub Release 只有
`v0.1.16` 与 `v0.1.17`。2026-11-09 的实测状态：

| 渠道 | 存在的版本 |
|---|---|
| git tag | `v0.1.9` … `v0.1.17`（含 `v0.1.16.1`） |
| PyPI | `0.1.9`、`0.1.10`、`0.1.12`、`0.1.15`、`0.1.16`、`0.1.17` |
| Docker Hub `vllm/vllm-router` | `v0.1.16`、`v0.1.16.1`、`v0.1.17`，加 `latest`、`nightly`（另有 `v0.0.0` 测试 tag） |
| GitHub Release | `v0.1.16`、`v0.1.17` |

结论：tag 列表不能当发布清单用，验收要分别去 PyPI、Docker Hub 和 Releases 页核对。

## 流水线实际执行的门禁（硬）

| # | 门禁 | 证据 job / 脚本 | 失败后果 |
|---|---|---|---|
| 1 | tag 版本 == `pyproject.toml` 版本 | `version-check`（仅 tag 构建） | 后续 build/publish 全部不执行 |
| 2 | 两架构产物构建成功：x86_64 出二进制 + wheel + sdist，aarch64 出 wheel；在 manylinux_2_28 镜像里 `cargo build --release` + `python -m build` + `auditwheel repair`，Rust 用 nightly 工具链 | `build_wheels_x86_64`、`build_wheels_aarch64` | PyPI 发布阻塞 |
| 3 | 二进制 smoke：`vllm-router --version`、`--help` 在 manylinux 容器内可执行 | `smoke-test-binary-x86_64` | PyPI 发布阻塞 |
| 4 | wheel smoke：两个架构各自 `pip install` 后 `import vllm_router` 并打印 `__version__` | `test-python-wheel-x86_64`、`test-python-wheel-aarch64` | PyPI 发布阻塞 |
| 5 | PyPI 上传用 `twine upload dist/*`（`PYPI_TOKEN`），依赖 2+3+4 全部通过，`soft_fail: false`，只在 tag 构建执行 | `publish-pypi` | 无 PyPI 产物 |
| 6 | Docker 正式版：先填 `X.Y.Z`（required），再依次解除 build 与 publish 两个 block；两个架构分别用 `docker/Dockerfile.router` 构建并推 `vX.Y.Z-{x86_64,aarch64}`，最后合并 manifest | `input-release-version`、两个 block、`publish-release-images.sh release` | 只缺镜像，PyPI 不受影响 |
| 7 | nightly 镜像：`NIGHTLY=1` 才构建 `nightly-{x86_64,aarch64}`，发布 `nightly` 与 `nightly-YYYYMMDD-<sha7>`，随后清理旧 tag 只保留最近 14 个 | nightly 组、`publish-release-images.sh nightly`、`cleanup-nightly-router-builds.sh` | 只影响 nightly 通道 |

门禁**不覆盖**的四处，举证时必须自己说明：

- `release-pipeline.yml` 不跑 L1/L2/L3 测试矩阵（`.buildkite/README.md`：wheels and images only），
  只有上面 3、4 两条 smoke。
- `vllm_router.__version__` 只被打印，不与 tag 比较；四份版本文件漏改一份，CI 不会报错。
- wheel 用 nightly Rust 工具链构建，测试流水线固定 `rust:1.95.0`；CI 绿测不等于 wheel 里那份
  二进制已验证。
- Docker 通道不校验 tag 与 `pyproject.toml`（`version-check` 只对 tag 构建生效），所以可以只发镜像；
  `v0.1.16.1` 就是这么来的。

Docker 通道还有两条运维前提：secret `ROUTER_DOCKERHUB_TOKEN` 只在 Docker job 上映射为
`RELEASE_DOCKERHUB_TOKEN`，需要 Buildkite agent 3.106.0 以上支持 step-level secrets；secret 的
访问策略限定在 `router-release` 流水线、main 或批准的版本 ref，以及 CPU/ARM release 队列。

## 发布前质量门槛（政策，需人工举证）

发布前的质量门槛按本仓库现有的通道收敛：NVIDIA 与 ROCm 的平台覆盖、精度回退都包含在 L2/L3
两行里，不另立单项；性能没有统一百分比，按场景口径举证；覆盖率目标在开工会确定。
类型列中 `Gate` 指不分迭代的硬性门槛，`Guide` 指按当期迭代指南解释并举证。

| # | 要求 | 类型 | 说明 |
|---|---|---|---|
| 1 | 目标 commit 的 L2 merge 全绿，最近一轮 L3 nightly 无失败 | Gate | L2 有 xGMI smoke（GSM8K limit 20，基线 0.38 允许 −0.03）与 RDMA `SMOKE_ONLY`；L3 有 P/D 全量 GSM8K（HTTP 与 gRPC）、ROCm xGMI/RDMA 的 read true/false 矩阵、gRPC prefix-KV perf、L1 encode perf。证据给 Buildkite build 链接，L1 只证明 PR 自身，不能替代 |
| 2 | 单元测试覆盖率达到开工会确定的当期目标 | Guide | 覆盖率先在迭代开工会定成具体目标（数值 + 统计范围），发布时用 CI 产出的 `coverage.xml` 对照；CI 不设固定阈值，没定目标按 N/A 记录 |
| 3 | 性能场景无回退，按各场景自己的口径举证 | Guide | 不设统一百分比，见下面“性能与覆盖率怎么举证” |
| 4 | 需求完成率 > 85% | Gate | 按当期需求清单核对，本仓库无自动化 |
| 5 | 剩余 DI < 30 | Gate | 按 [DI 计算说明](../../../general/bug/guides/di-calculation.md) 逐条定级后累加；定级口径见下方说明 |
| 6 | 无未关闭 critical issue，剩余 bug 均有 assignee | Gate | critical 由同一套 issue 分级口径判定；本仓库无自动化 |

### 性能与覆盖率怎么举证

- **性能不用统一阈值。** prefix-KV 用例按 `tests/python/e2e/perf/grpc/README.md` 的验收口径
  分场景比较 rust gRPC（G）、rust HTTP（R）、python HTTP（P）的 `ttft_ms.avg` 与 `e2e_ms.avg`：
  hit 0% 时 G 在 ttft 和 e2e 上不得回退于 R 与 P；hit 99% 时 G 在 ttft 和 e2e 上不得回退于 R，
  e2e 不得回退于 P，且 G 的 ttft 要比 P 好 20% 以上（`ttft_G <= 0.8 * ttft_P`）。CI 的 L3 job
  只跑 `rust_grpc` 与 `python_http` 的 `c16-r32`、`hit99-c4`、`hit99-c64`，`rust_http` 需要本地补齐。
  L1 encode 用例按 `tests/python/e2e/perf/l1_encode/README.md` 比较 `wave.summary.encode_ms.p50`
  与 `encode_reuse.p50` 在 L1 on/off 之间的差异。CPU overhead 用同机 `cargo bench` 的 baseline
  对比。pytest 只校验每个 wave 的统计完整，跨后端验收要人工判读 `results/` 里的 `wave.summary`，
  不能声称 CI 已经断言 SLO。
- **覆盖率目标来自开工会，不来自 CI。** 先把当期目标写进迭代记录（Python 单测范围是
  `-m "ready and cpu"` 的 `coverage.xml`；如果当期还要求 Rust 覆盖，需另行说明工具和范围），
  发布时逐项对照；目标缺失时该项只能记 N/A 并注明原因。
- **DI 需要先有分级。** 公式要求每条未关闭 issue 先定 critical / high / medium / low；
  本仓库 bug 模板只打 `bug` label，没有 severity 字段，因此发布前要先确认 router 与 agent-infer
  的 issue 分级策略并对齐到同一口径（用 label 或发布记录逐条定级）。口径未确认时 DI 记 N/A，
  不能拿 issue 数量代替。

## 发布步骤

1. 确认目标 commit 满足上面 1–6，把证据链接记进验收记录。
2. 提一次版本 bump，四份文件同改，走正常 PR 合并；不要只改 `pyproject.toml`。
3. 在 bump commit 上打 `vX.Y.Z` tag 并推送。
4. 盯 `router-release`：`version-check` → 两架构 build → smoke → `publish-pypi`。
5. 需要镜像时手动走 Docker 通道：填 `X.Y.Z`，依次解除 build 与 publish 两个 block，产出
   `vllm/vllm-router:vX.Y.Z`、`:latest` 与两个 per-arch tag。
6. 在 GitHub Releases 补发布说明：现有 `v0.1.16`、`v0.1.17` 的结构是 Highlights / Key Improvements /
   Note / Artifacts / Full Changelog / New Contributors，Artifacts 段列出 PyPI 三个文件的 SHA-256、
   镜像多架构大小和单架构 tag。流水线里没有 `gh release` 或等价步骤，这段必须手写。
7. `NIGHTLY=1` 只发 nightly：`vllm/vllm-router:nightly` 与 `nightly-YYYYMMDD-<sha7>`，不碰 PyPI，
   不能当正式版本；nightly 构建只保留最近 14 个。
8. 发布后把 tag、PyPI、Docker Hub、GitHub Release 四处状态并排核对一遍，缺哪一处就写明原因。

## 常见失败模式

- tag 与 `pyproject.toml` 不一致：`version-check` 直接失败，这是唯一能拦住后续所有产物的门禁。
- 只改 `pyproject.toml`：wheel 装得上，但 `vllm_router.__version__` 还是旧值；smoke 只打印不比较。
- 把 README 的 “CI/CD Pipeline” 当流程依据：该段仍写 `cibuildwheel` 和“`pyproject` 版本变化即发布”，
  与实际的 tag 驱动、四文件同步不一致；Dockerfile 路径 `/docker/Dockerfile.router` 倒是对的。
  流程以 `.buildkite/release-pipeline.yml` 为准。
- 用 tag 列表判断发布状态：`v0.1.11`、`v0.1.13`、`v0.1.14` 没有任何产物，`v0.1.16.1` 只有镜像。
- 用 nightly 的绿测替代发版证据：nightly 覆盖面最广，但它不阻塞 PR、也不校验 tag 与版本文件，
  过期一轮的 nightly 结果不能当本次 tag 的门禁证据。
- 只验证 x86_64：PyPI 与镜像都要求两个架构各自构建并 smoke。
- 声明 macOS/Windows wheel：构建只在 manylinux_2_28 上做，一个 abi3 wheel 覆盖多个 Python 版本，
  不是跨平台承诺。

## 验收记录

发布说明或 checklist 逐项给出，缺一项按未通过处理：

- 是否通过（Pass / Fail / N/A），硬门禁与政策门禁分开列。
- 证据链接（Buildkite build、`coverage.xml`、各场景性能 `wave.summary`、开工会记录的覆盖率目标、DI 定级记录、issue 列表）。
- 发布产物核对表（tag / PyPI 版本 / Docker tag / GitHub Release 是否都存在）。
- Guide 项写明当期 Guide 版本或链接；Docker 与 GitHub Release 是否发布单独列出。
