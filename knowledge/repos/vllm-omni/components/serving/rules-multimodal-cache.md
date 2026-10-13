---
title: "多模态缓存身份与存储合同"
created: 2026-10-09
updated: 2026-10-09
type: rule
tags: [vllm-omni, components]
sources: ["PR #8062", "PR #7781", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/entrypoints/openai/serving_speech.py#L1225-L1273", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/vllm_omni/entrypoints/openai/serving_speech.py#L1362-L1396", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/entrypoints/openai_api/test_reference_waveform_cache.py#L18-L57", "https://github.com/vllm-project/vllm-omni/blob/2e3c7fe2c171cd3429298094d175a64eacbdb341/tests/entrypoints/openai_api/test_ref_audio_cache.py#L47-L88"]
confidence: high
---

# 多模态缓存身份与存储合同

## SERV-MM-UUID-1a — replica scoped UUID 写入返回副本

- 触发：修改 stage-0 replica 预选、多模态 UUID prefix 或 add-request prompt 处理。
- 强制：只对实际需要 scoped cache 的 stage-0 AR 多 replica 路径预选 receiver；保留 caller prompt，返回 shallow copy 写入 scoped UUID，并把该返回 prompt 一路传给 processor/add request。list prompt 保留相同结构；无 multimodal data、单 replica、diffusion stage 或预选不可用保留原 prompt。
- 禁止：把 scoped UUID 回写 caller dict，导致复用 prompt 时新媒体沿用旧内容 hash 或重复 prefix；选中 receiver 后仍处理未 scoped 原 prompt。
- 验收：复用同一 prompt dict 并更换媒体，跨两个 replica 检查不同正确 key 与 payload 接收；断言 caller UUID/media 不变，list 与 bypass 路径保持返回合同。 ^[PR #8062]

## SERV-3e — resolved waveform 必须拥有独立且紧凑的缓存存储

- 触发：修改 reference-audio finalize、waveform representation 或 resolve cache 的插入边界。
- 强制：finalize 生成自有、C-contiguous 的 float32 buffer；缓存 payload byte 计数对应实际保留的紧凑数组。
- 禁止：缓存仍引用大型 decoded backing allocation 的 view；把 ndarray.nbytes 当作包含其外部 backing allocation 或进程总内存的保证。
- 验收：mono view 与 stereo 输入都断言 dtype、C-contiguous、owndata、与 decoded 输入不共享存储及数值正确。^[PR #7781]

## SERV-3f — array cache 复用必须同时保留 caller 的不修改合同

- 触发：修改 array/list reference-audio resolver、cache hit 或 finalized ndarray insertion。
- 强制：array resolver 与 cache 复用已 finalize 的 buffer；array consumer 进行原地处理前复制。legacy list resolver 为每次调用生成独立 list。
- 禁止：为缓存插入再次无条件复制 finalized ndarray 而破坏 resolver/cache identity；把共享 ndarray 当作可修改的 caller-owned array，或把 list 隔离证明外推到 ndarray。
- 验收：miss→hit 只 fetch 一次，array 返回值与缓存对象 identity 一致；修改 legacy 返回 list 不影响缓存，list insertion 后修改源 list 也不影响缓存。^[PR #7781]
