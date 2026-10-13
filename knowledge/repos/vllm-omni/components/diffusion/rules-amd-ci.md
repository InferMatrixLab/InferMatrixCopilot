---
title: "vLLM-Omni Diffusion AMD CI 规则"
created: 2026-10-13
updated: 2026-10-13
type: rule
tags: [vllm-omni, components, diffusion]
sources: ["PR #8849"]
---

# vLLM-Omni Diffusion AMD CI 规则

## VLLM-OMNI-PR8849-AMD-SLOW-CPU — 慢速 diffusion CPU 用例只属于专用 slow job，不回填 hash 分片

- 触发：修改 `.buildkite/amd/test-amd-merge.yml` 或 `.buildkite/amd/test-amd-ready.yml` 中五路 diffusion CPU hash 分片的命令或 `--deselect` 列表、`Simple · Diffusion Slow CPU Test` job 定义，或 `tests/buildkite/test_amd_pipeline.py` 的 `DIFFUSION_SLOW_CPU_*` 常量；向该慢速集合增删用例。
- 强制：六个慢速 node ID 由 blocking job `Simple · Diffusion Slow CPU Test`（mi300_1、timeout 40 分钟、JUnit `artifacts/amd-diffusion-slow-cpu/pytest.xml`、exit 134 自动重试一次、`VLLM_ROCM_USE_AITER=0`、沿用 `core_model and cpu` marker 过滤）唯一运行：`tests/diffusion/distributed/test_wan_vae_fastpath_install.py::test_omni_wan_vae_decode_override_is_bitwise_exact[dtype0]`/`[dtype1]`、`tests/diffusion/distributed/test_wan_decoder_fast_path.py::test_sharded_fast_path_on_every_rank[True-exact]`、`tests/diffusion/models/lingbot_world/test_pipeline_lingbot_world.py::test_condition_continues_real_causal_vae_beyond_initial_horizon[realtime]`/`[stepwise]`，以及同文件的 `test_condition_encoder_stops_at_its_fixed_point_and_restarts_with_the_session`。merge 与 ready 两份 yml 的五路分片必须 `--deselect` 同一组 node ID；该集合在两份 yml 与 pipeline 测试三处同步维护。
- 禁止：把这组 node ID 留在或加回五路 hash 分片（重复执行并拖慢关键路径，实测曾占 shard 2/5 约 15–23 分钟）；用删用例替代迁移；改集合时只同步 merge/ready 之一或漏改 `test_amd_pipeline.py`；用改 marker、单卡池或 Blocking 等级规避上述同步。
- 验收：`pytest tests/buildkite/test_amd_pipeline.py` 通过；`pytest --collect-only` 能收集全部六个 node ID；AMD READY/MERGE 中 slow job 执行全部六例且 hash 分片最慢用例不再包含它们；slow job 超时保持在实测耗时（约 23 分钟）之上。 ^[PR #8849]

<!-- kb:rule status=active since=v0.30.0 -->
