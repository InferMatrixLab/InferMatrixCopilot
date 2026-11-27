---
title: "Diffusion paged cache 与系统运行时规则"
created: 2026-09-03
updated: 2026-11-27
type: rule
tags: [vllm-omni, components, diffusion]
sources: ["PR #5255", "PR #5344", "PR #5543", "PR #5838", "PR #6094", "PR #6102", "PR #6385", "PR #6340", "PR #6714", "PR #6814", "PR #6563", "PR #5716", "PR #6786", vllm_omni/diffusion/attention/, vllm_omni/diffusion/attention/parallel/ulysses.py, vllm_omni/diffusion/attention/parallel/ring_kernels.py, vllm_omni/diffusion/diffusion_kv/, vllm_omni/diffusion/distributed/cfg_parallel.py, vllm_omni/diffusion/distributed/parallel_state.py, vllm_omni/diffusion/worker/diffusion_model_runner.py, vllm_omni/platforms/interface.py, vllm_omni/platforms/npu/platform.py, tests/diffusion/diffusion_kv/, tests/diffusion/distributed/test_cfg_parallel.py, tests/diffusion/attention/test_piecewise_attn.py, tests/diffusion/attention/test_ulysses_uaa.py, "PR #5491", "PR #5194", "vllm_omni/diffusion/data.py", "vllm_omni/diffusion/utils/hf_utils.py", "PR #7041", "PR #6463", "PR #6844", "PR #7548", "PR #11109"]
confidence: high
---

# Diffusion paged cache 与系统运行时规则

`system-runtime` 审查组的 `DIFF-4a`–`DIFF-4i`。触发条件、覆盖范围和其余审查组见
[Diffusion 共享规则](rules.md) 的 Direct 代码快速入口；上游兼容侧的 `DIFF-4k`–`DIFF-4l`
留在 [upstream 兼容规则](rules-upstream-compat.md)。

## DIFF-4a — 容量预算必须在影响峰值的初始化完成后测量

- 触发：新增或修改 KV/cache block 数、GPU/CPU memory budget、auto-fit、warmup/profile、
  dummy run 或物理 cache 分配。
- 强制：写出并核对 `model load → activation warmup/profile → available-memory measurement
  → logical config → physical allocation → first real request` 的真实顺序；预算必须包含真实
  activation/graph/lazy-kernel 峰值及明确 headroom。若当前阶段故意只建逻辑控制面，必须使用
  显式固定预算或把最终容量决定推迟到数据面 profile 之后。
- 禁止：用 warmup 前的当前 free/process residency 推导可用 cache，再在之后执行会常驻或抬高
  峰值的 dummy/profile；也不能用 native auto-fit 或 allocator 成功证明预算来源正确。
- 验收：enabled 路径在真实初始化顺序下记录测量前后峰值，低显存/最大合法请求不会因漏算
  activation 在首次或后续执行 OOM；显式预算分支和自动预算分支各有测试。 ^[PR #6094]

## DIFF-4b — 复用 native 组件仍须审计 caller/adapter 契约

- 触发：diffusion adapter 调用 vLLM 的 cache spec/config/manager、scheduler 或其他版本化 API。
- 强制：按仓库 pin 的精确 vLLM 版本核对 property/method、签名、sentinel、block-size 单位、
  max-length/capacity 前提、返回失败语义和 free/rollback 责任；分别证明 adapter 输入、调用时序、
  输出解释和 native 内部算法。
- 禁止：把“委托给 native vLLM”当作端到端正确性证明；native manager 不负责验证 Omni 在何时
  测量预算、是否漏算其他显存、是否正确传播失败，也不保证 adapter 模拟对象符合真实属性合同。
- 验收：真实 pin 的 contract test 覆盖正常值、边界/sentinel、容量不足、property 访问和清理；
  fake 不能把真实 property 改成同名 method 或跳过 native precondition。 ^[PR #6094]

## DIFF-4c — Feature gate 只限制影响范围，enabled 路径必须独立闭环

- 触发：新增或修改 `dense_legacy`/paged、eager/optimized 或其他默认关闭的 diffusion 路径。
- 强制：分别审计 disabled/default 与 enabled；enabled 至少覆盖单请求、并发/多 rank、低资源、
  partial allocation、执行异常、取消、timeout、shutdown 和重复释放。异常必须到达终端请求或
  engine 信号并唤醒 waiter，所有已分配资源由明确 owner 回收。
- 禁止：用“默认路径有 gate”“dense 未回归”或单请求 smoke 推导新路径正确；也不能让 scheduler/
  busy-loop 异常逃逸后留下永远等待的 stream。
- 验收：path/lifecycle matrix 每行有 production-path 测试或明确 `MISSING_EVIDENCE`；enabled
  关键行缺失时审查只能是 partial，不能输出 clean/no findings。 ^[PR #6094]

## DIFF-4d — 异常清理覆盖每种错误传递形态

- 触发：worker/RPC 在异常后清 traceback、释放 device tensor/cache，或把异常转换成
  reply、rank-status envelope、`DiffusionOutput`。
- 强制：先保存对外所需的字符串化错误，再对 raise、普通错误回复和 collect-status 正常返回
  三条路径逐一执行同一清理；调用方把异常折叠成成功返回形态时仍必须释放原异常引用。
- 禁止：只在最外层 `except` 清理，却让内部状态封装路径正常返回；清理后继续从异常对象读取
  message/traceback；用单一路径 mock 证明所有 RPC mode 都不会保留 device tensor。
- 验收：测试分别覆盖 `collect_rank_status=true` 与抛出/错误回复路径，断言错误文本仍可见、
  `exc.__traceback__ is None`、cache cleanup 被调用，后续健康 RPC 仍成功。 ^[PR #6385]

## DIFF-4e — paged backend 在启动时按平台和逐层能力闭环

- 触发：启用 paged scheduler、增加 diffusion attention backend 或平台 override。
- 强制：启动阶段解析并验证每层实际 backend 与当前平台；走 Omni platform hook，保留
  piecewise/SP 等能力。HunyuanImage3 paged requests 已有 GPU/NPU production caller；NPU SDPA 只可作
  startup memory-profile 的 dense fallback，正式 paged request 必须走 native backend；不能因 dense
  FA4 缺失拒绝 Blackwell formal paged path。
- 禁止：未知平台继承 GPU 默认；分配 cache 后到首次 dispatch 才失败；静默占用 paged KV
  却继续 dense；直接换用 upstream backend 丢失 Omni 能力。
- 验收：unsupported platform/backend fail-fast；CUDA/NPU 对应 lane 和正式模型 E2E 证明
  resolved backend；mixed-mask piecewise/SP 回归通过。 ^[PR #5543] ^[PR #6102]
- SenseNova-U1 paged AR decode 是 model-local, single-sequence CUDA fallback/capture，不是本规则的
  scheduler-owned paged backend；其 eligibility、growth、sleep 与 dynamic-PEFT veto 见
  [SENSENOVA-1b](../../models/sensenova-u1/rules.md#sensenova-1b-paged-ar-decode-是单序列模型本地-cuda-opt-in)。^[PR #6516]

## DIFF-4f — paged attention metadata 不从 padding 猜逻辑布局

- 触发：paged adapter 修改 GQA、prefix/current-token packing、LSE layout 或二维特例。
- 强制：显式传递 Q/KV head 数、每请求 prefix/current span 和 layout；LSE producer 必须声明 `bhs`
  或 `bsh` 后规范化，绝不按 shape（含 `sequence == heads`）推断；保持压缩 GQA 与 packed
  current-token 语义，单 token/单 head 二维输入仍使用同一合同。
- 禁止：从 padding 长度推断逻辑跨度；在 `sequence == heads` 时猜 LSE 维度；fallback 静默换
  backend 或把 KV heads 扩成 Q heads。
- 验收：32Q/8KV、异构 prefix、单 token/单 head、非对称等维 LSE 和 batch compaction 精确
  对照 dense/reference 输出。 ^[PR #5543] ^[PR #6102]

## DIFF-4x — SymmMem Ulysses A2A transport 与 workspace 生命周期

- 触发：修改 `ulysses_a2a_backend`、旧版 `ulysses_a2a_permute`、strict Ulysses all-to-all、NCCL SymmetricMemory JIT、CUDA graph 或 diffusion worker shutdown。
- 选择：`v0.34.0` tag 的旧 bool 默认关闭。自 [#11109](https://github.com/vllm-project/vllm-omni/pull/11109) 起，CUDA `auto` 只有满足 strict pure Ulysses、非 causal、使用目标布局且无 CUDA-graphed denoising 时才尝试 copy engine；group 必须单机、具备 P2P 和 torch symmetric memory，超过两 rank 还要求全 NVLink；两 rank 可用 PCIe P2P。group collective 给所有 rank 同一结果，条件不满足则记录理由并走 `nccl`。显式 `nccl` 不做 CE 选择；在进入 `select_transport` 的 CUDA Ulysses 分支，显式 `copy_engine` 不合硬件或 graph 条件会报错；策略仍按调用的 mode/layout 决定实际交换。NPU 的 `auto` 不自动启用 SHMEM；显式 `copy_engine` 走平台自己的 eager、pure Ulysses、HCCS/SHMEM 校验。[选择器](https://github.com/vllm-project/vllm-omni/blob/6c30b6eb7f5e69407b5353be4f806e0bfc7a96fb/vllm_omni/diffusion/distributed/ulysses_transport.py) · [调用方](https://github.com/vllm-project/vllm-omni/blob/6c30b6eb7f5e69407b5353be4f806e0bfc7a96fb/vllm_omni/diffusion/attention/parallel/factory.py)
- 组合路径：纯 AllGather-KV 不执行 Ulysses exchange；Ulysses × AllGather-KV 在 transport selector 前返回，显式 `copy_engine` 的内部标志仍传给 Ulysses leg（实际 exchange 受 mode/layout 限制），不传给 AllGather leg。不能把 CUDA selector 的拓扑/graph 拒绝规则套到这一分支；显式 NPU 仍先经平台配置校验。
- Overlap：`v0.34.0` tag 没有 `--ulysses-overlap-chunks`。在 #11109 合入点，正数 chunks 与显式 `nccl`、非 strict、Ring 或 AllGather 不兼容，配置阶段拒绝；合法 `auto` 若随后解析为 `nccl` 则不启用 overlap。CUDA graph capture 另行禁用 head overlap。
- Workspace：CE 的正向布局为 `(scatter,gather)=(2,1)`，反向为 `(1,2)`；advanced-UAA 保留独立交换。每个 `(device, process-group)` 只有一个单 CUDA stream 的 grow-only byte workspace；首次分配及增长必须是 collective、在增长前同步，容量内以 typed byte-slice view 复用；graph capture 中禁止增长，须先 warm up 最大 shape；worker shutdown 同步并释放 workspace。Fast Ulysses QKV staging 只有 input/output 同一 CUDA device、同一 dtype、input rank-3、shape 相同、output contiguous，且 inner row `stride(2)==1`、`stride(1)==size(2)`、outer stride 不小于一整 row（无 overlap）时，才可在 `CUDAGuard(input.device())` 与 current CUDA stream 上用 `cudaMemcpy2DAsync`。 ^[PR #6714] ^[PR #6814]
- 禁止：把 `auto` 的按条件选择说成显式 `copy_engine` 的静默 fallback；把 strict Ulysses+Ring 可走的旧版/显式 Ulysses leg 当作新 `auto` 会选择的 pure Ulysses；跨 stream 复用 workspace、无界缓存或 capture 中扩容；把 CPU/mock 或单机 benchmark 外推为跨平台收益。
- 验收：覆盖显式策略、`auto` 的 topology/graph fallback 和 overlap gating，验证不合资格布局不走 fused path；只改配置投影时验证真实 consumer 收到值即可。改变交换、workspace 或生命周期时，再按受影响路径补实际硬件 forward/reverse parity、workspace collective growth/复用、capture growth fail 与 teardown。历史 B300 row-strided BF16、四 rank parity 和 H3 smoke 仅证明其对应配置。^[PR #6340] ^[PR #6814]

## DIFF-4y — CFG parallel state 未初始化时串行，初始化错误必须可见

- 触发：修改 `cfg_parallel`、CFG process-group 初始化/查询、正负 guidance branch 的 rank
  分派，或其 runner 调用链。
- 强制：CFG parallel state 未初始化时，所有 rank 必须走 serial-1 路径；state 已初始化后，
  group 查询、world-size/rank 或 collective 的错误必须原样传播，不能将错误降级成 serial。
  已初始化 group 的 two-branch 与 N-branch 路径都必须按该 group 的 rank/world-size 分派，不能
  以默认 global parallel state 猜测 CFG 拓扑。
- 禁止：把“group 不存在”和已初始化 group 的查询/通信失败混为同一 fallback；只实现 two-branch
  而让 N-branch 静默串行、重复或遗漏 branch；在调用方吞掉初始化后的错误。
- 验收：以 mock/distributed test 覆盖未初始化 → serial-1、已初始化 group 的错误传播、two-branch
  rank 分派与 N-branch rank 分派；测试只证明这些 CFG state/path 合同，不外推为多节点吞吐或模型质量。^[PR #6786]

## DIFF-4g — Ring GQA 只在通信后扩展，Hybrid Ulysses 不把 padding 当真 head

- 触发：修改 Ring SDPA、GQA/MQA head 数、LSE accumulation、Ulysses all-to-all 或
  `advanced_uaa` head padding。
- 强制：Ring 始终以原始 `H_kv` 通信 K/V；仅在本 rank 的 aten SDPA 调用前，当
  `H_q != H_kv` 时要求 `H_q % H_kv == 0`，再用 `repeat_interleave(H_q/H_kv)` 将 K/V 扩成
  Q head 数。该 local materialization 增加 kernel 输入计算/显存，但不增加 ring 通信量；不能
  提前到 send/recv 前。之所以不能直接用 `enable_gqa`，是该 path 需要 aten flash/efficient
  SDPA 返回 LSE 供 ring accumulation，而这些调用没有该参数。实现以 K head 数计算 factor 并
  同样 repeat V，却未显式检查 `H_v == H_k`；现有测试始终令两者相等，future caller/backend 必须
  保持或提前断言该 invariant，不能等 SDPA 在更深处 cryptic failure。
- 强制：advanced UAA 独立验证 `K==V`、`Q%KV==0`，main/joint 都适用。Ulysses `U`、ratio `R` 下先令
  `padded_KV=ceil(KV/U)*U`，再令 `padded_Q=padded_KV*R`，协调 pad Q/K/V 后 A2A；strict Ulysses
  保持 exact divisibility。Hybrid Ring 在既有 PyTorch ring path 本地 expand compact padded KV，不能 blanket reject；
  Q padding >=1.5x 时按 `(Q,KV,U,label)` warn once，说明 FLOP/temp VRAM。
- 禁止：用默认 backend 的绿测证明 SDPA 修复（安装 FA/FA3 时会绕过）；用 num_heads=3、
  Ulysses=2 的 padded case 当 Hybrid 正向测试；从通信量不变推断端到端性能不变。
- 禁止：独立 rounding（如 28/7/U2 成 32/8 以外的 Q/KV ratio）、把 padding 当真实 head、放松 malformed
  shape，或外推 broad performance。CPU/main-joint、multi-GPU parity 与 4×H800 microbench 仅为有界证据。^[PR #5716]
- 验收：CPU 覆盖 main/joint 的 valid coordinated padding 与 malformed shape；multi-GPU 以 no-SP
  baseline 对 SP2 `28/7`、`6/3`、SP4 `12/3` 及 Ulysses2×Ring2 的 MHA/GQA/joint。warning threshold
  与 dedup 需单独保护。PR #5716 的 4×H800 数据只是 attention microbenchmark：aligned case 接近
  baseline，而 uneven/MQA padding overhead 可显著增加；不构成模型 E2E、质量或通用性能证明。
  既有 pure-Ring/L4 gate 证据仍只覆盖其原始范围。^[PR #5255] ^[PR #5716]

## DIFF-4h — FlashInfer mixed-dtype plan 必须绑定完整 runtime shape 与 mask 内容

- 触发：修改 `FLASHINFER_ATTN`、`BatchPrefillWithRaggedKVCacheWrapper`、QK16/V8、backend
  selection、ragged indptr/plan cache 或 custom mask fallback。
- 强制：输入保持初始化时 CUDA device；Q/K 只允许 fp16/bf16 override，V 另允许
  fp8_e4m3，wrapper 输出恢复原 query dtype。auto backend 按 compute capability 选 major>=10
  `cute-dsl`、major>=9 `fa3`、否则 `fa2`；non-CuTe 才接受 custom mask，CuTe nontrivial mask、
  causal+explicit mask 或无法 pack 的 mask 必须回退原始输入的 Torch SDPA。
- 强制：`flashinfer_backend` 即使未设 dtype 也必须经 `AttentionSpec.backend_kwargs()` 序列化到 impl；
  platform availability 必须 probe 实际 `prefill.BatchPrefillWithRaggedKVCacheWrapper` symbol，而非仅
  顶层 import。每层 non-CuTe 私有 128MiB workspace 的累计 OOM 风险仍未解决，不得声称已共享/懒分配。
- 强制：plan key 至少绑定 batch、Q/KV length、Q/KV heads、QK/K/VO head dim、Q/K/V/output
  dtype、causal、scale 与是否有 mask；shape/dtype 改变重建 indptr/plan，unmasked 同 key 可复用。
  mask shape 相同也可能内容不同，所以每次 masked call 都必须重新 plan，不能只按 pointer/shape
  cache。Q/K/V cast 留在 compile-visible path，wrapper plan/run 边界才 disable compiler。目标 key
  虽记录 `head_dim_k`，plan 只接收 `head_dim_qk` 与 `head_dim_vo`，且未断言 Q/K head dim 相等；
  caller 必须保持 `D_q == D_k`，并补 mismatch fail-fast test，不能把 key 字段误当 backend 已消费。
- 禁止：把本 PR 早期 Sage/TRTLLM kernels 当成 merged scope（review 后删除并转交 TRTLLM
  backend）；把 activation 直接 cast 成 FP8 描述成带 scale 的 checkpoint quantization；用
  `supports_attention_mask=True` 推断 CuTe 支持任意 mask。
- 验收：mixed QK/V 仅 FlashInfer >=0.6.16rc1；按真实 plan dtype 检验可解析的旧版、缺失/非法版本
  都 fail-fast。覆盖 allowlist、auto、device、plan/mask replan、CuTe/causal、GQA/output dtype。
  PR #5344 的 pre-final B200/B300 smoke 不是 E2E latency、稳定 speedup 或通用质量证据。^[PR #5344]

## DIFF-4i — 模型能力 metadata 必须跨 architecture 与 pipeline 两个 key space 解析

- 触发：修改 diffusion registry、HF `architectures`、`model_class_name` 或共享
  `DiffusionModelMetadata`，尤其是多图数量、mixed-reference 与 attention-mask-free 能力。
- 强制：先接受 metadata 表中的 direct pipeline key；未命中时只通过 registry 的
  `architecture → pipeline class` 映射解析一次，再查 metadata。unknown architecture、`None`，以及
  registry 映射到没有 metadata 的 pipeline 都保持空 defaults；registry tuple 用解包读取，使布局
  变化显式失败。此 helper 返回整份 metadata，影响 config、AsyncOmni serving view、视频 mixed
  reference 和 attention backend，不得把修改只当作 image-count 局部修复。
- 禁止：因 architecture 已注册就假定其 pipeline 有 metadata；递归猜测别名；让 direct key 被
  registry 映射覆盖；或把 defaults 的 `supports_multimodal_inputs=false` 当成“数量未知”——image-edit
  admission 会把它解释为最多一张图，而 `supports=true, max=None` 才是不设上限。
- 验收：同时覆盖 direct pipeline key、architecture→metadata-bearing pipeline、unknown/`None`、
  以及 architecture→无 metadata pipeline；通过 `OmniDiffusionConfig.update_multimodal_support()`
  断言整份能力传播，并在 serving 边界验证 false→1、true+整数上限与无上限行为。PR #5838 只新增
  Qwen direct 与 Hunyuan architecture→pipeline 的手动 config 测试；未执行真实 architecture 解析、
  AsyncOmni 或 HTTP edit 路径，也没有 registry/metadata census 和 negative alias 回归。其 E2E 结果
  只有截图，缺命令文本日志、vLLM 版本、upstream SHA 与重复次数。helper 仍 function-local import
  私有 `_DIFFUSION_MODELS`，review 提议公开改名但 final 未采用。^[PR #5838]

相关执行流见 [Diffusion architecture](architecture.md)；paged attention 的 layout 细节见
[attention 规则](rules-attention.md)。

## DIFF-4o — AR-Diffusion 容量必须覆盖持久状态，分页图输入必须显式

- 触发：AR-Diffusion runner 修改 paged self-attention KV、resident session capacity、scratch/cross-attention reservation、model-owned CUDA state、commit pruning 或 CUDA Graph 输入。
- 强制：按有效 resident capacity 完整计算 `sink + recent window`、单个 in-flight block、scratch、cross-attention 和每 session 的 model-owned state；`gpu_memory_fraction` 只能控制额外 residency，至少一个能装入实际空闲显存的 session 必须先被接纳，并在分配 self-KV 前扣除所有持久 reservation。成功 commit 后立即释放 skipped paged blocks；paged attention 将可变 key/value pool 作为显式可变 graph input。worker-local state 仍须保持 `max_num_seqs=1`，多副本必须先具备 session affinity。
- 禁止：用只覆盖一个 session 的固定 floor 忽略声明 capacity；只为 self-KV 预算而漏算 cross-attention、scratch 或模型自有状态；使用 process-global KV pool registry；让各 TP rank 依据不同的本地 free-memory 计算不同 capacity 后继续 collective；把单次 TP replay 或能启动当作多 session/多 rank 预算正确性的证明。
- 验收：覆盖 LingBot 两个 resident window、DreamZero 声明 capacity 64 的预算封顶、LRU eviction、失败 forward 清理、commit 后 block 回收和显式 graph pool 输入；真实 TP 还要用统一的 min free-memory/all-reduce 或 rank-0 广播验证所有 rank 得到相同 capacity，并分别验证 paged/direct 数值与单副本路由。^[PR #5491]

## DIFF-4p — Scheduler-managed Diffusion KV 必须原子分配并绑定请求生命周期

- 触发：启用 `paged_scheduler`，或修改 Worker KV geometry discovery、显存 profile、Scheduler admission、CFG allocation、`NewRequestData`/DLO RPC envelope 和 request terminal cleanup。
- 强制：模型加载后从每个 Worker 的 cache-enabled attention 生成 rank-local native `FullAttentionSpec`，以最大 per-rank profile batch 测量 KV headroom，再通过 `get_kv_cache_configs()`、`generate_scheduler_kv_cache_config()` 和 `resolve_kv_cache_block_sizes()` 构造 Worker/Scheduler 配置。Scheduler 是 logical allocation/generation owner，Worker 是 physical tensors/BlockTables/slot mappings owner；每个 CFG sequence 按完整首步 `seq_len` 以 `full_sequence_must_fit=True` 分配，先按空池所需 block 总数做 never-fit 检查，并在所有 sequence 成功前保持原子性。生成的 `DiffusionKVMetadata` 必须与请求绑定并随完整 `NewRequestData` envelope 传到 Worker；完成、取消、错误、pop 和 close 都必须释放整组 allocation，preemption 只保留既有 allocation。^[PR #6563]
- 禁止：引入独立 block pool、KV spec、refcount 或 physical lifecycle；把部分 CFG 分配当作成功；把临时容量不足误报为永久失败，或让 never-fit 请求在 FIFO 头部无限阻塞；在 DLO DP wave 中拆开 request 与对应 metadata，或让 scheduler/busy-loop 异常逃逸而不唤醒请求流。
- 验收：覆盖 dense 默认旁路、rank/spec/memory mismatch、profile 顺序、CFG partial rollback、临时压力返回 `None` 后 FIFO 重试、空池也放不下时只终止该请求、native allocation error、preemption metadata 保留，以及 finish/cancel/error/pop/close 全部释放；Hunyuan first prefill 覆盖整 allocation，denoise 只写 prefix-offset target。^[PR #6094] ^[PR #6563]

## DIFF-4t — HiDream-O1 checkpoint signature 必须正向门禁

- 触发：diffusion registry、`resolve_model_class_name()`、`OmniDiffusionConfig` enrichment 或 HF checkpoint 自动识别逻辑需要区分 HiDream-O1 与普通 Qwen3-VL。
- 强制：仅当 `config.json` 的 `model_type` 为 `qwen3_vl`，且 `model.safetensors.index.json` 的原始 `weight_map` 含 `model.final_layer2.linear.weight` 或 `final_layer2.linear.weight` 时，才返回 `HiDreamO1ImagePipeline`；`resolve_model_class_name()` 与 config enrichment 必须复用同一正向 signature，registry 同时登记 pipeline 与 post-process。
- 禁止：把任意 `qwen3_vl` 配置归类为 HiDream-O1；给 raw index key 强加 `model.` 前缀；只增加显式 `model_class_name` 映射而让通用 text-to-image/server 自动解析仍落到未注册 architecture；缺失或格式异常的 index 不得误报命中。
- 验收：覆盖 HiDream signature 命中、普通 Qwen3-VL、缺失/错误 `weight_map`、显式类名与 signature 不一致，以及 resolver/enrichment 两条路径；再断言 registry pipeline 和 module-level post-process 均可解析。^[PR #5194]

## DIFF-4u — Worker 物理 paged KV 必须与 Scheduler 逻辑分配分层并原子安装

- 触发：启用或修改 diffusion Worker 的 native paged KV data plane、请求 row registry、paged attention adapter、BlockTables 更新或唤醒/终止清理。
- 强制：Scheduler 只拥有 logical block allocation、generation 和 `DiffusionKVMetadata`；每个 Worker rank 消费自己的 `KVCacheConfig`，拥有物理 KV tensors、native `BlockTables` 和 request/sequence/context rows。安装 metadata 前必须校验 group/count/range/capacity，并以 staged append、apply 和失败 rollback 原子发布；重复 generation 幂等，冲突或过期快照拒绝，BlockTables 变更使已准备的 attention batch 失效，终止时先清 Worker rows 而不释放 Scheduler blocks。request-mode runner 负责从 snapshot 构造并激活 prefill/denoise row metadata；model 只提供 Q/K/V layout 与 `full_attn_spans`。
- 禁止：Worker 自行分配或释放 Scheduler-owned logical blocks；把部分 row 安装、native append/apply 异常或 stale snapshot 当作成功；从 padding 猜异构 prefix 的逻辑布局；在清理或 wake refresh 后继续复用旧的 native metadata/buffer view。
- 验收：CPU/mock 覆盖 rank-local config、请求/上下文 row mapping、重复/过期/冲突 generation、非法 block 与容量、append/apply rollback、幂等清理和 wake refresh；GPU/NPU 另验证非连续 BlockTables、32Q/8KV GQA、异构 prefix 与 piecewise attention。Hunyuan paged request execution 不证明 `denoise_step`、cross-request prefix reuse、AR KV、negative CFG、Ring 或 AllGather。^[PR #6102] ^[PR #6563]

## DIFF-4ac — 声明为 str 的 diffusion cache_backend 必须在 kwargs 边界把显式 None 规范成 none

- 触发：修改 `normalize_omni_diffusion_kwargs`、`OmniDiffusionConfig.cache_backend`，或 pipeline/runner 对 cache backend 的 admission 校验。
- 强制：字段声明为 `str` 且 canonical “无 cache” sentinel 为 `"none"` 时，key 缺失才允许走 env/`"none"` 默认；调用方传入显式 `None`（如 CLI `default=None`）必须在 kwargs 边界规范成 `"none"`，且不得回落到 `DIFFUSION_CACHE_BACKEND` 查找。下游校验可把残留 `None` 当 `"none"` 作 defense-in-depth，但不能代替边界规范化。
- 禁止：只在 key 缺失时默认、让显式 `None` 泄漏成 typed `str` 字段上的 `None`；把 `"none"` 与 env 覆盖混为一谈；把某一 pipeline 的 `None` 特判当成唯一修复点。
- 验收：`from_kwargs`/`create_default_diffusion` 对 `cache_backend=None` 断言最终为 `"none"`；至少一个严格 `("none", …)` 校验路径覆盖 `None` 与 `"none"` 均可通过，未知 backend 仍拒绝。^[PR #7041]

## DIFF-4aa — AR-Diffusion paged K/V 必须分存储分配，禁止共享 `(2, …)` 视图

- 触发：修改 AR-Diffusion `allocate_kv_pool_with_views`、paged-write custom op 的 `mutates_args`、compiled graph 下的 key/value pool，或把 K/V 重新合并为单一 allocation。
- 强制：每层 K 与 V 各自 `torch.empty` 独立 storage，布局仍为 `(num_blocks, block_size, num_kv_heads, head_dim)`；`kv_pools[layer][0]/[1]` 与 flat slot view 继续寻址，但不共享 `untyped_storage`。paged-write 若同时 mutate K 与 V，共享 storage 会让 Inductor reinplace 只重写第一个参数，并使 `auto_functionalized_v2` 克隆整份 V pool。
- 禁止：恢复 `empty(2, num_blocks, …)` 再 `kv[0]`/`kv[1]` 切片；或把“eager 能跑”当成 compiled 路径无整池拷贝的证明。
- 验收：断言各层 K/V `data_ptr` 不同且写 V 不扰动 K；编译生成代码不得出现 pool-sized clone；有界硬件证明 compiled 路径可运行且无 per-step 整池拷贝。^[PR #6463]

## DIFF-4ab — AR-Diffusion stepwise 必须以一次 request 驱动整段 rollout

- 触发：修改 `SupportsStepExecution`、`ARDiffusionModelRunner` 的 stepwise 绑定、AR block/`post_decode` chunk 提交、`step_execution`+`streaming_output` deploy，或把外部 tick 循环重新当作 session 边界。
- 强制：一次 `generate()` 驱动整段 rollout：`prepare_encode` 只跑一次，随后多轮 `denoise_step`/`step_scheduler`，由 `post_decode` 产出每个 AR chunk。request-mode 与 stepwise 必须共用同一份 block/DMD math，禁止维护两套可漂移实现。runner 仅为实现该合同的 pipeline 打开 `step_execution`；绑定 runner-owned KV 时 `session_id == request_id`；错误路径 fail-closed 释放 session，完成或 scheduler abort 时退役。持有 AR-Diffusion paged KV 的 stage 必须保持 `max_num_seqs=1`。
- 禁止：用多次 `generate()`/tick 冒充同一 session；在 denoise step 内提交本应属于 `post_decode` 的 clean-x0 KV commit；或把逐步 camera/prompt mid-request interaction 写成已由本合同覆盖。
- 验收：覆盖 stepwise 与 request-mode 的共享 math 边界、session bind/release、abort/completion 退役，以及 `max_num_seqs=1` 拓扑；request-scoped camera script 等模型字段另由模型 owner 验收，不得外推为通用 mid-request interaction。^[PR #6844]

## DIFF-4ad — 无法合成观测的 pipeline 必须退出通用文本 warmup

- 触发：修改 `DiffusionEngine.run_startup_warmup`、`dummy_run_num_frames`，或新增必须携带非文本观测（如 `robot_obs`）的 diffusion pipeline。
- 强制：通用 startup warmup 是文本、两步请求。pipeline 无法从该请求合成所需观测时，必须声明 `dummy_run_num_frames: ClassVar[int] = 0`，使 `run_startup_warmup` 不提交请求、也不因此 `close`。模型自有 warmup（如 AR-Diffusion 的 `ar_diffusion_warmup_requests`）保持可用。能接受文本 dummy 的模型仍走两步通用 warmup。
- 禁止：把 DreamZero/LingBot 的 opt-out 扩成所有模型跳过 warmup；用一步 shortcut 让非法文本请求穿过观测检查；opt-out 后仍断言 engine 被关闭。
- 验收：经真实 registry 查找，DreamZero 与 LingBot 的 startup 不调用 `add_req_and_wait_for_response` 且不 `close`；其余模型的两步 dummy 合同不变。^[PR #7548]
