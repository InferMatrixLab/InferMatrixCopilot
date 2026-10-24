---
title: "共享 GPU Runner 的路由、采样与生命周期合同"
created: 2026-10-09
updated: 2026-10-09
type: architecture
tags: [vllm-omni, components, model-executor]
sources: [vllm_omni/worker/gpu_ar_model_runner.py, vllm_omni/worker/gpu_generation_model_runner.py, vllm_omni/worker/output/payload_build.py, vllm_omni/worker/sparse_audio.py, vllm_omni/worker/sampling_utils.py]
---

# 共享 GPU Runner 的路由、采样与生命周期合同

本页是人工选定范围的七方面源码深度，固定于 [PR #5452 的源码快照](https://github.com/vllm-project/vllm-omni/tree/0249b6cde574460ae0b8df12f4e337e3bda1a824)。它解释共享 GPU runner 的输入、控制流和失败边界；不扩展 feature catalog，也不表示仓库整体 depth 验收已通过。

共享硬门禁沿用 [runner 规则入口](rules.md)、[payload ownership](rules-bridge-batch.md) 与 [sparse/sampler 合同](rules-runtime-hot-paths.md)。模型如何产生音频列表归 [VoxCPM2 producer owner](../../models/voxcpm2/rules.md) 等模型页，本页只解释共享消费实现。


<!-- kb:depth feature=runner-correctness facet=flow pin=0249b6cde574460ae0b8df12f4e337e3bda1a824 sha256=59225e12f3735f0d8487daed6d8a08bfe22cfabc519c1f09b594d3367583f458 -->
**执行流程（flow）**
AR output builder 先按输出类型和终点 stage 筛选 payload 接收请求，再调用 `resolve_sparse_mm_routing` 得到请求列表、稀疏索引及 audio-sparse 标志。逐请求 builder 在 audio-sparse 路径跳过 hidden payload，把多模态取值交给 `build_omni_mm_payload`。

可直接验证的 helper 调用链是 `build_omni_mm_payload` → `build_combined_prefix_cache_mm_payload` → `unwrap_combined_payload_value`：有 prefix-cache 合并数据时，第一层把 batch index 或 sparse index 作为 `list_idx` 传入，第二层跳过协议键并按 `rid` 取缓存项，第三层递归展开字典、选择列表元素。无合并数据时走 `mm_cpu` 分支；audio-sparse 列表直接按请求的 sparse index 取值，tensor 元素 clone 后进入该请求的 payload。

调用路径：`vllm_omni/worker/output/payload_build.py`（`build_omni_mm_payload`） → `vllm_omni/worker/output/payload_build.py`（`build_combined_prefix_cache_mm_payload`） → `vllm_omni/worker/output/payload_build.py`（`unwrap_combined_payload_value`）

来源：[vllm_omni/worker/gpu_ar_model_runner.py:L1799–L1829](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_ar_model_runner.py#L1799-L1829), [vllm_omni/worker/gpu_ar_model_runner.py:L828–L873](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_ar_model_runner.py#L828-L873), [vllm_omni/worker/output/payload_build.py:L103–L162](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/output/payload_build.py#L103-L162), [vllm_omni/worker/output/payload_build.py:L25–L100](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/output/payload_build.py#L25-L100)

<!-- kb:depth-proof {"basis":"supported","evidence":[{"end":1829,"path":"vllm_omni/worker/gpu_ar_model_runner.py","sha256":"f57bba9ccd10d0c92422bfa66e44abbf16808aa32b39ba928648b1051c492fa5","start":1799},{"end":873,"path":"vllm_omni/worker/gpu_ar_model_runner.py","sha256":"f037aee9ae15b389bc8431dc389b80a3769bc710e94356148081a741359bce76","start":828},{"end":162,"path":"vllm_omni/worker/output/payload_build.py","sha256":"2dabfec37e96b105a20036aef50ce8018b61f57216319b427cc3cb674dc74b77","start":103},{"end":100,"path":"vllm_omni/worker/output/payload_build.py","sha256":"b1273e70b5160ce50004480bad21e574fce05133be7fd09af90922c616d0ae2a","start":25}],"trace":[{"end":126,"path":"vllm_omni/worker/output/payload_build.py","start":103,"symbol":"build_omni_mm_payload"},{"end":100,"path":"vllm_omni/worker/output/payload_build.py","start":76,"symbol":"build_combined_prefix_cache_mm_payload"},{"end":73,"path":"vllm_omni/worker/output/payload_build.py","start":25,"symbol":"unwrap_combined_payload_value"}]} -->
<!-- /kb:depth -->

<!-- kb:depth feature=runner-correctness facet=api pin=0249b6cde574460ae0b8df12f4e337e3bda1a824 sha256=8e9381804694a9e125e2b894171a41ba40be3357eabe349e2eb1cb2d068b32fd -->
**输入与返回合同（api）**
`meta.sparse_audio` 可位于 nested `meta` 或 flattened 键；合法 carrier 是字符串或仅含字符串的 list，忽略大小写后以 `1/true/yes/on` 判真，`None` 表示缺省。两侧都存在时必须判真一致；非法 carrier 即使遇到另一侧合法声明也会拒绝。marker 为真时 `meta.req_id` 必须是无重复的字符串 list；双编码请求列表必须相同，空 list 合法。

公共入口 `resolve_sparse_mm_routing` 返回 `(downstream_req_ids, sparse_mm_index, audio_sparse_output)`，并在模块内部吸收协议异常。有效 audio-sparse 声明按当前输出请求顺序筛选接收方，元素索引仍来自声明顺序。非 audio 输出保持 dense 路由。

合并 payload 的长度为 1 的 list 一律按共享 singleton 展开；这是形状约定，没有类型标记能把合法 invariant singleton 与错误缩短至 1 的 per-request list 区分开，因此不能据此保证所有错位输入都被拒绝。

Generation `sample_tokens` 的 tensor leading dimension 或 list 长度必须等于 `input_batch.num_reqs`；逐 row 生成 CPU contiguous `model_outputs`，list 中的 `None` 保留请求位置，长度不符抛出同时带实际值与期望值的 `ValueError`。

来源：[vllm_omni/worker/sparse_audio.py:L101–L232](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/sparse_audio.py#L101-L232), [vllm_omni/worker/output/payload_build.py:L25–L65](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/output/payload_build.py#L25-L65), [vllm_omni/worker/gpu_generation_model_runner.py:L437–L467](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_generation_model_runner.py#L437-L467)

<!-- kb:depth-proof {"basis":"supported","evidence":[{"end":232,"path":"vllm_omni/worker/sparse_audio.py","sha256":"d70d1c75ade3eb7d76cf1aab112523e170bdad77fd2e43e8fddfb752bf970f78","start":101},{"end":65,"path":"vllm_omni/worker/output/payload_build.py","sha256":"8a72731443e9f6f5edee4292b1e330e7f95d3484465c6b82b1d45d9ca3f43078","start":25},{"end":467,"path":"vllm_omni/worker/gpu_generation_model_runner.py","sha256":"60bb79768e12734681800afeb5e6d34611031270b7bf57aeabb7d234580967e0","start":437}],"trace":[]} -->
<!-- /kb:depth -->

<!-- kb:depth feature=runner-correctness facet=configuration pin=0249b6cde574460ae0b8df12f4e337e3bda1a824 sha256=df5db5b698d7c15b1c8c0fa0f05e181996e2c73051bb2158291c61733dfd37c1 -->
**开关与进入条件（configuration）**
这些入口主要由 model capability 属性与已有 runner 配置控制。自定义采样仅在 `spec_decode_metadata is None`、logits 非 `None`、`model.sample` callable 且 `prefer_model_sampler` 为真时进入；先应用可用的 logit-bias state、准备 metadata，再调用模型 sampler。`skips_model_sampler_output_token_history=True` 会跳过 runner 的 history 重建。模型返回非 `None` 则直接采用，返回 `None` 则 warning-once 后调用默认 sampler；有 speculative metadata 时委托父类。

请求的 `omni_final_stage_id` 缺省时保留 payload 且不记忆这个缺省判断，已知时才缓存 `final_stage_id > 0`；`engine_output_type` 归一化为小写，audio 终点没有 downstream 请求时仍保留输出 payload。

Generation runner 在状态更新、finished-request 模型回调和 EC producer 分支之后检查 `total_num_scheduled_tokens <= 0`。普通 no-forward 分支只有 `external_launcher` 且 `data_parallel_size > 1` 才先 `_dummy_run(1)`，随后按有无 KV group 返回 connector no-forward 或空输出；EC producer 已在此前返回 encoder 输出。

来源：[vllm_omni/worker/gpu_ar_model_runner.py:L431–L437](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_ar_model_runner.py#L431-L437), [vllm_omni/worker/gpu_ar_model_runner.py:L457–L480](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_ar_model_runner.py#L457-L480), [vllm_omni/worker/gpu_ar_model_runner.py:L1394–L1427](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_ar_model_runner.py#L1394-L1427), [vllm_omni/worker/gpu_generation_model_runner.py:L160–L198](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_generation_model_runner.py#L160-L198)

<!-- kb:depth-proof {"basis":"supported","evidence":[{"end":437,"path":"vllm_omni/worker/gpu_ar_model_runner.py","sha256":"74a7bbec9d21a08346de4e435783b98cb02b45787aab83ac234a3222a790ad51","start":431},{"end":480,"path":"vllm_omni/worker/gpu_ar_model_runner.py","sha256":"09d8bfc2e35e91eda378bbef46e16e8117d0ef53524e5811991c547c7d13060f","start":457},{"end":1427,"path":"vllm_omni/worker/gpu_ar_model_runner.py","sha256":"958123601c003eadf4224b5eb8a14b5c275038282d4b911b27d49025b104a7e0","start":1394},{"end":198,"path":"vllm_omni/worker/gpu_generation_model_runner.py","sha256":"123d39b67bddc339f9a0fd079fd37ca5f8971065aff71910bcf0fc68fd820c70","start":160}],"trace":[]} -->
<!-- /kb:depth -->

<!-- kb:depth feature=runner-correctness facet=dependencies pin=0249b6cde574460ae0b8df12f4e337e3bda1a824 sha256=1b0184d71e81f11ff6ed1f4cd1d84df860f0716aab4fab27ed6274b249903a0b -->
**模块与上游依赖（dependencies）**
Generation `_dummy_run` 的 `has_preprocess` 分支与共享 `_preprocess` 使用同一 runner 预分配的 `input_ids.gpu` 和 `inputs_embeds.gpu` 切片。capture 所绑定的 storage 与 replay 输入准备因此共享 buffer 合同；这里仅说明该分支的耦合，不证明所有 multimodal、prompt-embeds 或平台路径一致。

采样前防护依赖 vLLM 的 penalty 布局。prompt padding helper 把大 ID clamp 到 `logits_vocab`，该值是上游 `vocab_size + 1` penalty bin 中最后被丢弃的 padding bin；改成 `logits_vocab - 1` 会把 padding 计入真实末尾 token。runner 仅在有 logits、需要 penalties、存在 prompt IDs 且 batch vocabulary 更宽时调用它。

来源：[vllm_omni/worker/gpu_generation_model_runner.py:L797–L816](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_generation_model_runner.py#L797-L816), [vllm_omni/worker/gpu_model_runner.py:L1623–L1638](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_model_runner.py#L1623-L1638), [vllm_omni/worker/sampling_utils.py:L14–L25](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/sampling_utils.py#L14-L25), [vllm_omni/worker/gpu_ar_model_runner.py:L2024–L2030](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_ar_model_runner.py#L2024-L2030)

<!-- kb:depth-proof {"basis":"supported","evidence":[{"end":816,"path":"vllm_omni/worker/gpu_generation_model_runner.py","sha256":"d52ff9e51bc7b0e7389038a41c30862436cb8c37582116298b1ca33fa7de990f","start":797},{"end":1638,"path":"vllm_omni/worker/gpu_model_runner.py","sha256":"65fd331e06ff1723559623af880b27759c6c7a74e749bda32011889b96ab7003","start":1623},{"end":25,"path":"vllm_omni/worker/sampling_utils.py","sha256":"493dd24289c33d934a9ed35f8340a600da7fc0514a1b39a99472c95065779f6e","start":14},{"end":2030,"path":"vllm_omni/worker/gpu_ar_model_runner.py","sha256":"2ef2fa1791497ced172c98e3a3454fd400de1de043270aad33b9081606c22ea3","start":2024}],"trace":[]} -->
<!-- /kb:depth -->

<!-- kb:depth feature=runner-correctness facet=failure_modes pin=0249b6cde574460ae0b8df12f4e337e3bda1a824 sha256=ac77d70f9f781c3674b0d71e7671312494dc69994489f769298df6c6f3b4f61d -->
**异常与降级边界（failure_modes）**
非法 sparse 声明在公共解析入口内被吸收：audio 输出返回空接收方、空索引及 `audio_sparse_output=True`，受影响 step 不路由多模态 payload；classifier 按 offender 去重；audio routing 后果首次立即记录，在后续错误调用跨过 60 秒窗口时再次记录 suppressed 次数，共享锁保护计数且日志在锁外执行。非 audio 路径保留原接收方与 dense 模式。这个策略针对被检测出的声明错误，不能替代 payload 内容的完整一致性检查。

直接 sparse payload 列表的索引超出长度时记录 error 并丢弃该 key，其余 key 仍可保留；没有回退到首个请求元素。合并路径对非 singleton 越界列表也丢 key，singleton 则遵循上述共享约定。

`_merge_model_kv_transfer_metadata` 调用模型 hook 失败时记录 exception 并重新抛出，阻止缺失 custom metadata 的 KV transfer 继续。外层 mapping 总是新建；仅模型返回非空 metadata 的 request dict 与 `custom_metadata` mapping 被浅复制，模型值覆盖同名旧值。无模型 metadata 的 request dict 原样透传；其他字段及 metadata 的嵌套值仍可共享引用，不能称为深度隔离。

来源：[vllm_omni/worker/sparse_audio.py:L44–L98](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/sparse_audio.py#L44-L98), [vllm_omni/worker/sparse_audio.py:L199–L232](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/sparse_audio.py#L199-L232), [vllm_omni/worker/output/payload_build.py:L25–L152](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/output/payload_build.py#L25-L152), [vllm_omni/worker/gpu_ar_model_runner.py:L875–L914](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_ar_model_runner.py#L875-L914)

<!-- kb:depth-proof {"basis":"supported","evidence":[{"end":98,"path":"vllm_omni/worker/sparse_audio.py","sha256":"acf04cc513541a3b91a09ff23578bfc05c46479eaf8d683e2ea2e1610eeacb38","start":44},{"end":232,"path":"vllm_omni/worker/sparse_audio.py","sha256":"ae609c072c8f62cb2b4be079eeef9c872d6e6aab050e5469102c089ef3570a37","start":199},{"end":152,"path":"vllm_omni/worker/output/payload_build.py","sha256":"f8031cefefea26c547019a61cb4d3057adab00794e51c286cf534e220eed0376","start":25},{"end":914,"path":"vllm_omni/worker/gpu_ar_model_runner.py","sha256":"c6c7a90379804df04df44a8667dc21ef45cec927b153e31e468bf4646ab3d878","start":875}],"trace":[]} -->
<!-- /kb:depth -->

<!-- kb:depth feature=runner-correctness facet=tradeoffs pin=0249b6cde574460ae0b8df12f4e337e3bda1a824 sha256=b8f81325ee8979cd81bdf3edd96c68c0307b3e5696dfc0b24057505b6247a8d9 -->
**设计收益与代价（tradeoffs）**
设计推断（非作者历史意图）：

基于实现可作以下推断，未进行性能测量，也不代表作者历史意图。

- sparse 声明 fail-closed 的收益是避免检测出错误后继续按 dense batch index 分配他人的 payload；代价是受影响 audio step 形成输出缺口，需要日志定位 producer。
- singleton broadcast 的收益是继续支持请求无关共享值；代价是长度为 1 的错误 per-request list 与合法共享值存在歧义，形状检查无法证明所有输入都不会串请求。
- KV metadata 浅复制的收益是当前 merge 不改写 scheduler 的映射、且不必复制所有嵌套对象；代价是传给 manager 的未修改 entry 与嵌套值仍可能 alias 原对象，后续消费者若原地修改仍需自己的所有权约束。
- model sampler 的 `None` fallback 使声明模型可拒绝某一步并继续默认采样；代价是意外的 `None` 同样触发默认 token 语义，warning-once 提供可见性但不能证明这种拒绝符合模型意图。

来源：[vllm_omni/worker/sparse_audio.py:L199–L232](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/sparse_audio.py#L199-L232), [vllm_omni/worker/output/payload_build.py:L25–L65](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/output/payload_build.py#L25-L65), [vllm_omni/worker/gpu_ar_model_runner.py:L875–L914](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_ar_model_runner.py#L875-L914), [vllm_omni/worker/gpu_ar_model_runner.py:L1394–L1427](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/vllm_omni/worker/gpu_ar_model_runner.py#L1394-L1427)

<!-- kb:depth-proof {"basis":"supported","evidence":[{"end":232,"path":"vllm_omni/worker/sparse_audio.py","sha256":"ae609c072c8f62cb2b4be079eeef9c872d6e6aab050e5469102c089ef3570a37","start":199},{"end":65,"path":"vllm_omni/worker/output/payload_build.py","sha256":"8a72731443e9f6f5edee4292b1e330e7f95d3484465c6b82b1d45d9ca3f43078","start":25},{"end":914,"path":"vllm_omni/worker/gpu_ar_model_runner.py","sha256":"c6c7a90379804df04df44a8667dc21ef45cec927b153e31e468bf4646ab3d878","start":875},{"end":1427,"path":"vllm_omni/worker/gpu_ar_model_runner.py","sha256":"958123601c003eadf4224b5eb8a14b5c275038282d4b911b27d49025b104a7e0","start":1394}],"trace":[]} -->
<!-- /kb:depth -->

<!-- kb:depth feature=runner-correctness facet=validation pin=0249b6cde574460ae0b8df12f4e337e3bda1a824 sha256=3e64b5f9996a6808cb03bc8335e22e8986fa6bede9924951dbfd5e8b4d3adc3a -->
**已有验收与未验证范围（validation）**
本次提取复核的是 pinned 测试源码；以下是现有测试所执行的断言，不表示本轮已运行或通过这些上游测试。

运行型 helper/unit 断言：padding 测试直接调用生产 clamp helper 与 vLLM bin-count 函数，比较 padding 不影响真实 token 的 mask，并展示 clamp 到末尾真实 token 的反例；payload 测试调用生产 builder 检查 sparse 顺序 `[r1,r2]` 与 batch `[r0,r1,r2]` 分离、singleton broadcast、协议键移除和错位丢 key。Generation guard 测试用 stub runner 与 monkeypatch 调用实际 `execute_model`，检查 `-1/0`、DP dummy、KV no-forward 与 EC producer 分支；它们没有启动真实 DP 集群。

静态 AST/source 断言：capture/replay 测试读取方法源码，只遍历 `has_preprocess` 分支 body 的 attribute chain，要求 base capture、base preprocess 及 generation capture override 均读取 `self.input_ids.gpu` 和 `self.inputs_embeds.gpu`；它能发现缓冲区引用的一侧改动，不执行 CUDA graph，也不能证明 replay 数值正确。

真实 GPU capture/replay、分布式同步、模型音频生成和端到端路由仍需对应运行环境验收；此页没有仓库全覆盖或全模型通过的结论。

来源：[tests/worker/test_gpu_ar_model_runner.py:L1212–L1264](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/tests/worker/test_gpu_ar_model_runner.py#L1212-L1264), [tests/worker/test_gpu_ar_model_runner.py:L1513–L1617](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/tests/worker/test_gpu_ar_model_runner.py#L1513-L1617), [tests/worker/test_gpu_generation_model_runner.py:L127–L201](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/tests/worker/test_gpu_generation_model_runner.py#L127-L201), [tests/worker/test_capture_replay_contract.py:L19–L93](https://github.com/vllm-project/vllm-omni/blob/0249b6cde574460ae0b8df12f4e337e3bda1a824/tests/worker/test_capture_replay_contract.py#L19-L93)

<!-- kb:depth-proof {"basis":"supported","evidence":[{"end":1264,"path":"tests/worker/test_gpu_ar_model_runner.py","sha256":"62b20e7a12a3194d356e023d633c415b6229c9010234eef77d46906b22ecd827","start":1212},{"end":1617,"path":"tests/worker/test_gpu_ar_model_runner.py","sha256":"66b4dd43e1c80cdd5030efa79f3f11c86a242abeb075640dd6469fdefd05c159","start":1513},{"end":201,"path":"tests/worker/test_gpu_generation_model_runner.py","sha256":"dde8d89fe468781cd23dfbeb854dd732be03fc9ebdebb3cfa0103872f3be54b6","start":127},{"end":93,"path":"tests/worker/test_capture_replay_contract.py","sha256":"d13d277dcc9c86c5d30b2f86e886bc735889c1b37f2339f5f78305724e51d19f","start":19}],"trace":[]} -->
<!-- /kb:depth -->
