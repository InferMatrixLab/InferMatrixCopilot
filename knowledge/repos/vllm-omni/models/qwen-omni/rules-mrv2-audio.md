---
title: "Qwen3-Omni MRv2 audio handoff 规则"
created: 2026-10-06
updated: 2026-11-24
type: rule
tags: [vllm-omni, models, model-executor]
sources: ["PR #8223", "PR #10966", "PR #11031"]
confidence: high
---

# Qwen3-Omni MRv2 audio handoff 规则

## QOMNI-MRV2-1a — sampled embedding 和 codec frame 必须对应当前 request 的有效 sample

- 触发：修改 Thinker→Talker early handoff、sampled embeddings、MTP eager frames 或 Talker→Code2Wav payload。
- 强制：只有 staged Thinker 且无 speculative config 时发布 early sampled embedding；speculative 路径保留原 capture-stream handoff。完成 prefill 的 sample 可补齐 Talker prompt 的首 generated-token embedding；随后按 sampled-text-stream 状态发送每步新 sample。partial/resumed prefill 显式发布 empty sample marker，保留 mode 并跳过已经发过的 capture text；非终态 decode 缺 sample 应失败。MRv2 eager MTP 以 token-major codes 最后一 row 和显式 frame-valid 决定是否 append，该 row 必须 clone 拥有 storage；V1/deferred frame 仍保留 zero-placeholder/stop-token 分支。codec vocabulary 和 quantizer 数从模型 config 读取。
- 禁止：用 speculative 变宽 sample 套用单 token early handoff；把 empty marker 当成丢失 output 或重新发送 prefill text；将 graph-owned batch output 的 view 留给下一步；把 EOS/special CB0 作为有效 codec frame。
- 验收：覆盖首 prefill sample、全 batch partial prefill、preemption resume、缺 sample、EOS 和 V1/deferred 对照；batch reorder 后每 request codes/embeddings 保持对应，下一次 replay 不覆盖已提交 payload。^[PR #8223]

## QOMNI-MRV2-1b — Talker first audio 只在一帧无上下文的显式配置下启用

- 触发：修改 `talker_first_audio_enabled`、first-frame decoder、chunk ramp 或 first-audio trimming。
- 强制：要求 connector `talker_first_audio=true`、initial chunk 恰好一 frame、环境开关允许、CUDA MRv2 async-chunk、本地 TP=PP=1、local executor 且 prefix cache 关闭。Talker copy 与 Code2Wav 使用同 class/config/dtype/device；side-stream first-frame graph 每 bucket 独立 pool，输出必须 copy 为新 FP32 tensor。只在第一 codec chunk 携带 first-audio marker，并由最终音频路径裁掉 Code2Wav 对该 prefix 的重复样本；其他 chunks 继续按 ramp、steady size 和左上下文发送。
- 禁止：以通用 capability guard 代替模型 opt-in；将多帧、有历史上下文、跨 rank 或 prefix replay 视为同一首帧路径；让两个 process 的可选 cuDNN autotune 推导出 bitwise PCM 保证；第一音频和 Code2Wav copy 重复输出。
- 验收：逐项关闭 eligibility 后回到 regular codec path；核对 singleton/batched first-frame prefix 和 owned output，marker 只到 chunk 0；首帧音频加 trimmed 后续 chunks 与固定 decoder reference 核对时序/长度/容差。^[PR #8223]

## QOMNI-MRV2-1c — predictor 与 length-grouped decoder 优化必须保留 opt-in 和逐 row trim

- 触发：修改 residual-codebook predictor sampling、incremental/short-KV、Code2Wav length grouping、fused Snake 或 cuDNN warmup。
- 强制：deploy 的 `subtalker_sampling_params` 写入 predictor 的 stored top-k/top-p/do_sample 设置；不可假设 per-call参数会覆盖 stored sampler。incremental KV 使用显式 connector opt-in 和平台编译能力；fp16 或不支持 Inductor 时保留 re-prefill，short-KV 还须显式请求、CUDA BF16。decoder grouping 根据实际 lengths、graph buckets 和 captured row counts 规划，恢复原 row 顺序；crop 仍以本 group decoded window 的真实 tail 与每 row 的 left context/有效 frames 计算，遵循 [mixed-length trim rule](rules.md)。fused Snake 仅在显式 CUDA opt-in 下替换；cuDNN benchmark 仅在 eager warmup/非 capture 生效并通过 context 恢复进程 settings。
- 禁止：把 group padding 当作短 row 有效未来 context；取消平台/dtype fallback；在 capture 内做算法搜索；用 historical concurrent benchmark 或单个 kernel A/B 宣称 speech-quality parity、普遍吞吐或 latency 保证。
- 验收：覆盖 stored sampling/greedy、fp16与平台 fallback、short/steady mixed lengths、bucket miss 与 row reorder；分别核对裁剪样本、settings在异常后恢复，以及固定 input/seed 下优化路径的真实数值与 replay证据。^[PR #8223]

## QOMNI-MRV2-1d — seeded residual MTP 批处理、graph replay 与 eager RNG 流必须分别验证

- 触发：修改 Qwen3-Omni MRv2 Talker 的 residual-codebook seed、`get_mtp_seed`、per-row generator capability、whole-MTP graph 或预填 uniform buffer。默认 deploy 的 Talker `seed: 42` 也命中 seeded 路径；该规则不涉及 Codebook 0。
- 强制：以合入 [#11031](https://github.com/vllm-project/vllm-omni/pull/11031) 的实现为准：MRv2 读取 canonical `mtp_accepts_per_row_generators`（Qwen3-Omni 转发 Talker capability），让 seeded 多 row 保留单次 predictor 调用；在已有 whole-MTP graph runner 且满足 replay 条件时，使用静态 `mtp_sample_uniforms` 输入并启用 `mtp_sample_uniforms_preserve_eager_stream`。按 eager 顺序逐 residual step 抽样：seeded/mixed batch 逐 row 抽 `[1, vocab]`（无 generator 的 row 走默认流），全 unseeded batch 每 step 抽一个连续的 `[batch, vocab]`，再拷入 graph buffer；padding 固定为 `0.5`，不消耗 RNG。直接 unseeded replay 也走同一准备逻辑。
- 禁止：把正确的 batched dispatch 当作 CUDA graph 已实际 replay；假设 CUDA/ROCm 有 NPU 的 predictor 内层 graph；用一次 whole-span 抽样代替这个模型的逐 step 调用；将 [#10966 的历史 H200 A/B](https://github.com/vllm-project/vllm-omni/pull/10966#issuecomment-7086691994) 当作 #11031 修复后的性能数据，或由串行 seed 复现推断并发音频必然相同。
- 验收：从 [#11031 合入点的 MRv2 测试](https://github.com/vllm-project/vllm-omni/blob/2280fd690cb7b6d890f6b3b2f89cb0148f3ee14d/tests/worker_v2/test_omni_model_state.py#L757-L872) 出发，覆盖 seeded/mixed/fully-unseeded、row reorder 和 padding；相同 eager 输入同时核对逐元素 uniforms 与抽样后 RNG state，独立核对一次 batched 调用及 graph-runner dispatch。若主张 CUDA/ROCm 性能或语音质量恢复，另附同 workload 的 exact-head 设备证据（实际 graph replay、单流与并发 TTFP/RTF、匹配 Thinker 文本的音频对照及 Seed-TTS WER）；结构测试、pipeline green 或历史 H200 A/B 不代替这些结果。^[PR #11031]
