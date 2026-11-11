"""Source-grounded regressions for the Cosmos3 PR9304 knowledge correction.

These are content guards, not an execution of Cosmos3 or AMD CI. Evidence:
https://github.com/vllm-project/vllm-omni/pull/9304#issuecomment-6369776059
https://github.com/vllm-project/vllm-omni/pull/9304#issuecomment-6371457488
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / "knowledge/repos/vllm-omni/models/cosmos3"
RULE_ID = "VLLM-OMNI-PR9304-EDGE-PP-OWNERSHIP"


class Cosmos3KnowledgeEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        page = (OWNER / "rules.md").read_text(encoding="utf-8")
        heading = f"## {RULE_ID} — "
        if page.count(heading) != 1:
            raise AssertionError("Expected exactly one owner-scoped PR9304 rule")
        cls.rule = page.split(heading, 1)[1].split("\n## ", 1)[0]
        cls.index = (OWNER / "_index.md").read_text(encoding="utf-8")

    def test_stale_cache_causes_false_missing_weights_not_missed_error(self):
        self.assertIn("误报缺少必需权重", self.rule)
        self.assertIn("不属于本 rank", self.rule)
        self.assertNotIn("报为已加载", self.rule)
        self.assertNotIn("因此漏报", self.rule)

    def test_later_amd_evidence_keeps_attribution_and_limits(self):
        self.assertIn("维护者报告", self.rule)
        self.assertIn("AMD build #14506", self.rule)
        self.assertIn("目标测试所在 shard 通过", self.rule)
        self.assertIn("整体仍因无关 Wan 测试失败", self.rule)
        self.assertIn("issuecomment-6371457488", self.rule)
        self.assertIn("不能据此声称真实多 stage PP parity", self.rule)
        self.assertNotIn("AMD CI 未复跑", self.rule)

    def test_ownership_and_isolated_regression_contract_are_preserved(self):
        for contract in (
            "isinstance(self.language_model, PPMissingLayer)",
            "GEN slot 模块本身",
            "本 rank 拥有的 marker 缺失仍必须报错",
            'monkeypatch.setattr(model_utils, "get_pp_missing_layer_names", lambda _model: [])',
            "且未污染全局缓存",
            "不得 vendoring",
            "<!-- kb:rule status=active since=v0.30.0 -->",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, self.rule)

    def test_owner_index_routes_pp_ownership_to_existing_rules_page(self):
        self.assertIn("PP 权重归属", self.index)
        self.assertEqual(self.index.count("(rules.md#direct-代码快速入口)"), 1)


if __name__ == "__main__":
    unittest.main()
