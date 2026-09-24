import math
import unittest
from typing import Any, Dict

from hippo_memory.gate import SearchGateConfig, filter_search_results


class TestSearchGate(unittest.TestCase):
    def setUp(self):
        self.default_config = SearchGateConfig(
            final_threshold=0.32,
            dense_only_threshold=0.62,
            relative_threshold_ratio=0.50,
            enabled=True,
        )

    def _make_candidate(
        self,
        memory_id: str,
        text: str,
        score: float,
        semantic_score: float,
        bm25_score: float = 0.0,
        entity_boost: float = 0.0,
        max_possible: float = 2.0,
    ) -> Dict[str, Any]:
        return {
            "id": memory_id,
            "memory": text,
            "score": score,
            "score_details": {
                "semantic_score": semantic_score,
                "bm25_score": bm25_score,
                "entity_boost": entity_boost,
                "raw_score": semantic_score + bm25_score + entity_boost,
                "max_possible_score": max_possible,
                "final_score": score,
                "threshold": 0.1,
            },
        }

    def test_empty_results(self):
        """Empty candidate list safely returns empty list."""
        self.assertEqual(filter_search_results([], config=self.default_config, limit=5), [])

    def test_all_low_scores_returns_empty(self):
        """Fail-Closed under all low-score noise scenarios, never padding results."""
        candidates = [
            self._make_candidate("1", "无关记忆1", 0.29, 0.58),
            self._make_candidate("2", "无关记忆2", 0.28, 0.56),
            self._make_candidate("3", "无关记忆3", 0.20, 0.40),
        ]
        result = filter_search_results(candidates, config=self.default_config, limit=5)
        self.assertEqual(result, [])

    def test_real_scene_regression_pr_merge(self):
        """Real-world reproduction: 0.34 pure semantic direct fact preserved, 0.30/0.29 noise eliminated."""
        candidates = [
            self._make_candidate("1", "PR #7 提交记录", 0.63, 0.70, bm25_score=0.56),
            self._make_candidate("2", "PR #7 代码审查", 0.62, 0.68, bm25_score=0.56),
            self._make_candidate("3", "PR #7 分支测试", 0.60, 0.64, bm25_score=0.56),
            # Key item: PR #3 merge fact, no BM25 hit, semantic score 0.68 halved to 0.34 by denominator 2.0
            self._make_candidate("4", "PR #3 已合并到 main", 0.34, 0.68, bm25_score=0.0),
            # Noise item: no BM25, semantic score 0.58 (noise floor), halved to 0.29~0.30
            self._make_candidate("5", "SQLAlchemy 迁移历史", 0.30, 0.58, bm25_score=0.0),
            self._make_candidate("6", "个人语言偏好为中文", 0.29, 0.57, bm25_score=0.0),
        ]

        # With limit=5, top 4 pass, trailing 2 (0.30, 0.29) are strictly filtered out
        res_limit_5 = filter_search_results(candidates, config=self.default_config, limit=5)
        self.assertEqual(len(res_limit_5), 4)
        self.assertEqual([r["id"] for r in res_limit_5], ["1", "2", "3", "4"])

        # With limit=3, truncate to top 3 that passed the gate
        res_limit_3 = filter_search_results(candidates, config=self.default_config, limit=3)
        self.assertEqual(len(res_limit_3), 3)
        self.assertEqual([r["id"] for r in res_limit_3], ["1", "2", "3"])

    def test_dense_only_threshold(self):
        """Without BM25/entity support, dense_only_threshold (0.62) must be strictly met."""
        # semantic_score < 0.62 rejected even if final_score reaches 0.35
        cand_low_dense = self._make_candidate("c1", "弱语义候选", 0.35, 0.60, max_possible=1.0)
        # semantic_score >= 0.62 and final_score >= 0.32 passes
        cand_high_dense = self._make_candidate("c2", "强语义候选", 0.65, 0.65, max_possible=1.0)

        res = filter_search_results([cand_low_dense, cand_high_dense], config=self.default_config, limit=5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "c2")

    def test_bm25_or_entity_support_relaxes_semantic_threshold(self):
        """Semantic threshold is relaxed with BM25 or entity support as long as composite score qualifies."""
        # BM25 support: semantic_score only 0.50 (< 0.62), but bm25 > 0 and final_score=0.35 >= 0.32
        cand_bm25 = self._make_candidate("b1", "BM25命中项", 0.35, 0.50, bm25_score=0.20)
        # Entity support: semantic_score only 0.48 (< 0.62), but entity_boost > 0 and final_score=0.36 >= 0.32
        cand_entity = self._make_candidate("e1", "Entity命中项", 0.36, 0.48, entity_boost=0.24)

        res = filter_search_results([cand_bm25, cand_entity], config=self.default_config, limit=5)
        self.assertEqual(len(res), 2)
        self.assertEqual([r["id"] for r in res], ["b1", "e1"])

    def test_relative_threshold_ratio(self):
        """Dynamic relative score ratio cut: trailing items significantly below 50% of top score are filtered."""
        # Top score is 0.80, dynamic relative lower bound is 0.80 * 0.50 = 0.40
        # Candidate 2 composite score 0.35 (though > static 0.32) is below 0.40 and should be rejected
        cand_best = self._make_candidate("top", "极高相关记忆", 0.80, 0.80, bm25_score=0.80)
        cand_gap = self._make_candidate("gap", "相对断崖低分记忆", 0.35, 0.70)

        res = filter_search_results([cand_best, cand_gap], config=self.default_config, limit=5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "top")

    def test_best_score_calculated_only_from_absolute_pass(self):
        """Top score must be calculated only from candidates passing absolute gate; malformed scores cannot raise threshold."""
        # Malformed item: score marked 0.99 but lacks score_details (rejected by absolute gate)
        invalid_high = {"id": "bad", "memory": "畸形项", "score": 0.99}
        # Normal item: score 0.40 passes absolute gate. Must not be rejected by relative ratio from invalid_high
        valid_item = self._make_candidate("good", "正常合格项", 0.40, 0.80)

        res = filter_search_results([invalid_high, valid_item], config=self.default_config, limit=5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "good")

    def test_fail_closed_invalid_score_details(self):
        """Strictly Fail-Closed: abnormal structures are directly rejected, never blindly passed."""
        bad_cases = [
            # Missing score_details
            {"id": "b1", "memory": "m", "score": 0.8},
            # score_details is not a dict
            {"id": "b2", "memory": "m", "score": 0.8, "score_details": "invalid"},
            # Missing final_score
            {"id": "b3", "memory": "m", "score": 0.8, "score_details": {"semantic_score": 0.8}},
            # Top-level score and final_score mismatch (> 1e-4)
            {
                "id": "b4",
                "memory": "m",
                "score": 0.8,
                "score_details": {"semantic_score": 0.8, "final_score": 0.5},
            },
            # NaN score
            {
                "id": "b5",
                "memory": "m",
                "score": float("nan"),
                "score_details": {"semantic_score": float("nan"), "final_score": float("nan")},
            },
            # Inf score
            {
                "id": "b6",
                "memory": "m",
                "score": float("inf"),
                "score_details": {"semantic_score": float("inf"), "final_score": float("inf")},
            },
            # String score
            {
                "id": "b7",
                "memory": "m",
                "score": "0.8",
                "score_details": {"semantic_score": "0.8", "final_score": "0.8"},
            },
            # Missing bm25_score field
            {
                "id": "b8",
                "memory": "m",
                "score": 0.8,
                "score_details": {"semantic_score": 0.8, "final_score": 0.8, "entity_boost": 0.0},
            },
            # Missing entity_boost field
            {
                "id": "b9",
                "memory": "m",
                "score": 0.8,
                "score_details": {"semantic_score": 0.8, "final_score": 0.8, "bm25_score": 0.0},
            },
        ]
        res = filter_search_results(bad_cases, config=self.default_config, limit=5)
        self.assertEqual(res, [])

    def test_relative_gate_zero_threshold_and_zero_score_candidate(self):
        """When threshold is set to 0.0, valid candidate with score 0.0 should not be rejected by relative gate."""
        zero_config = SearchGateConfig(
            final_threshold=0.0,
            dense_only_threshold=0.0,
            relative_threshold_ratio=0.50,
            enabled=True,
        )
        cand_zero = self._make_candidate("z1", "零分事实", 0.0, 0.0, bm25_score=0.0, entity_boost=0.0)
        res = filter_search_results([cand_zero], config=zero_config, limit=5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "z1")

    def test_boundary_equality_conditions(self):
        """Boundary equality checks: passing when == threshold."""
        # final_score exactly equals 0.32, semantic_score exactly equals 0.62
        exact_item = self._make_candidate("exact", "临界项", 0.32, 0.62)
        res = filter_search_results([exact_item], config=self.default_config, limit=5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "exact")

    def test_limit_zero_and_negative(self):
        """Safely return empty list when limit <= 0."""
        item = self._make_candidate("1", "合格项", 0.60, 0.80)
        self.assertEqual(filter_search_results([item], config=self.default_config, limit=0), [])
        self.assertEqual(filter_search_results([item], config=self.default_config, limit=-1), [])

    def test_immutability_and_order_preservation(self):
        """Preserve input object immutability and retain Mem0 initial ordering."""
        c1 = self._make_candidate("1", "第一名", 0.70, 0.70)
        c2 = self._make_candidate("2", "第二名", 0.65, 0.65)
        original_c1_score = c1["score"]

        res = filter_search_results([c1, c2], config=self.default_config, limit=5)
        self.assertEqual([r["id"] for r in res], ["1", "2"])
        # Input object was not modified
        self.assertEqual(c1["score"], original_c1_score)
        # Returns shallow copy dict, not the same reference
        self.assertIsNot(res[0], c1)

    def test_gate_disabled(self):
        """When enabled=False, skip relevance gate while still truncating by limit."""
        disabled_config = SearchGateConfig(enabled=False)
        candidates = [
            {"id": "1", "memory": "m1", "score": 0.1},
            {"id": "2", "memory": "m2", "score": 0.05},
            {"id": "3", "memory": "m3", "score": 0.01},
        ]
        res = filter_search_results(candidates, config=disabled_config, limit=2)
        self.assertEqual(len(res), 2)
        self.assertEqual([r["id"] for r in res], ["1", "2"])

    def test_config_validation(self):
        """Config thresholds must be finite floats within [0.0, 1.0]."""
        with self.assertRaises(ValueError):
            SearchGateConfig(final_threshold=-0.1)
        with self.assertRaises(ValueError):
            SearchGateConfig(final_threshold=1.5)
        with self.assertRaises(ValueError):
            SearchGateConfig(dense_only_threshold=float("nan"))
        with self.assertRaises(ValueError):
            SearchGateConfig(relative_threshold_ratio=float("inf"))
        with self.assertRaises(ValueError):
            SearchGateConfig(final_threshold=True)  # bool is instance of int in Python


class TestMem0Contract(unittest.TestCase):
    """Zero-network-overhead local Mem0 contract tests, asserting scoring and explain data structures have not drifted."""

    def test_mem0_scoring_explain_structure(self):
        from mem0.utils.scoring import score_and_rank

        sem = [{"id": "m1", "memory": "test memory", "score": 0.75}]
        ranked = score_and_rank(
            semantic_results=sem,
            bm25_scores={"m1": 0.4},
            entity_boosts={},
            threshold=0.1,
            top_k=5,
            explain=True,
        )

        self.assertEqual(len(ranked), 1)
        item = ranked[0]
        self.assertIn("score", item)
        self.assertIn("score_details", item)

        details = item["score_details"]
        expected_keys = {
            "semantic_score",
            "bm25_score",
            "entity_boost",
            "raw_score",
            "max_possible_score",
            "final_score",
            "threshold",
        }
        self.assertTrue(expected_keys.issubset(details.keys()))
        self.assertAlmostEqual(item["score"], details["final_score"], places=4)
        # Ensure all keys are lowercase snake_case
        for k in details.keys():
            self.assertEqual(k, k.lower())
            self.assertFalse(any(c.isupper() for c in k))


class TestEngineSearchWiring(unittest.TestCase):
    """Tests for Engine layer search parameter passthrough, wide candidate pool calculation, and search gate wiring (using Mock Memory)."""

    def setUp(self):
        from unittest.mock import MagicMock
        from hippo_memory.engine import HippoEngine

        self.engine = HippoEngine.__new__(HippoEngine)
        self.mock_memory = MagicMock()
        self.engine._memory = self.mock_memory

        # Mock side-effect-free config
        config_mock = MagicMock()
        config_mock.user_id = "test_user"
        config_mock.semantic_threshold = 0.1
        config_mock.final_threshold = 0.32
        config_mock.dense_only_threshold = 0.62
        config_mock.relative_threshold_ratio = 0.50
        config_mock.gate_enabled = True
        config_mock.get_gate_config.return_value = SearchGateConfig()
        self.engine.config = config_mock

        # Mock router
        router_mock = MagicMock()
        router_mock.build_search_filters.return_value = {"user_id": "test_user"}
        self.engine.router = router_mock

    def test_engine_search_explain_and_threshold_passthrough(self):
        """Engine.search must pass explain=True and use semantic_threshold from config."""
        self.mock_memory.search.return_value = []

        res = self.engine.search("test query", limit=5)
        self.assertEqual(res, [])
        self.mock_memory.search.assert_called_once()

        _, kwargs = self.mock_memory.search.call_args
        self.assertEqual(kwargs.get("explain"), True)
        self.assertEqual(kwargs.get("threshold"), 0.1)
        self.assertEqual(kwargs.get("top_k"), 20)  # max(5 * 4, 20) == 20

    def test_engine_search_threshold_zero_preservation(self):
        """When threshold=0.0 is explicitly passed, it must be passed through as-is rather than swallowed or reverted to default."""
        self.mock_memory.search.return_value = []

        self.engine.search("test query", threshold=0.0)
        _, kwargs = self.mock_memory.search.call_args
        self.assertEqual(kwargs.get("threshold"), 0.0)

    def test_engine_search_limit_zero_does_not_call_memory(self):
        """When limit <= 0, return an empty list immediately without calling underlying Memory.search."""
        res_zero = self.engine.search("query", limit=0)
        self.assertEqual(res_zero, [])
        self.mock_memory.search.assert_not_called()

        res_neg = self.engine.search("query", limit=-2)
        self.assertEqual(res_neg, [])
        self.mock_memory.search.assert_not_called()

    def test_engine_search_applies_gate_and_handles_dict_results(self):
        """Engine handles dict return format from Mem0 and properly applies gate filtering and truncation."""
        raw_results = {
            "results": [
                {
                    "id": "1",
                    "memory": "有效记忆",
                    "score": 0.65,
                    "score_details": {
                        "semantic_score": 0.65,
                        "bm25_score": 0.0,
                        "entity_boost": 0.0,
                        "raw_score": 0.65,
                        "max_possible_score": 1.0,
                        "final_score": 0.65,
                        "threshold": 0.1,
                    },
                },
                {
                    "id": "2",
                    "memory": "噪音记忆",
                    "score": 0.25,
                    "score_details": {
                        "semantic_score": 0.50,
                        "bm25_score": 0.0,
                        "entity_boost": 0.0,
                        "raw_score": 0.50,
                        "max_possible_score": 2.0,
                        "final_score": 0.25,
                        "threshold": 0.1,
                    },
                },
            ]
        }
        self.mock_memory.search.return_value = raw_results

        filtered = self.engine.search("query", limit=5)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["id"], "1")


class TestMcpAndCliContracts(unittest.TestCase):
    """Tests for MCP minimalist safety contract and CLI debug options."""

    def test_mcp_search_memories_schema_has_no_threshold_or_gate_params(self):
        """MCP interface must never expose threshold, explain, or internal gate configs to agents."""
        import asyncio
        from hippo_memory.server import mcp_server

        tools = asyncio.run(mcp_server.list_tools())
        search_tool = next(t for t in tools if t.name == "search_memories")
        properties = search_tool.input_schema.get("properties", {})

        forbidden = {"threshold", "explain", "gate_config", "final_threshold", "dense_only_threshold"}
        for f in forbidden:
            self.assertNotIn(f, properties, f"MCP schema 不应暴露内部参数: {f}")

        # Contract verification: default limit exposed to agents must match actual effective cap (3)
        self.assertEqual(properties.get("limit", {}).get("default"), 3)

    def test_mcp_search_memories_clamps_limit_and_handles_zero(self):
        """MCP must clamp excessively large limit passed by agent to max_injected, and avoid calling engine when limit <= 0."""
        from unittest.mock import patch, MagicMock
        from hippo_memory.server import search_memories

        mock_engine = MagicMock()
        mock_engine.config.max_injected = 3
        mock_engine.search.return_value = []

        with patch("hippo_memory.server.get_engine", return_value=mock_engine):
            # limit <= 0: return user-friendly message directly without calling engine.search
            res_zero = search_memories("query", limit=0)
            self.assertIn("未找到与 'query' 相关的记忆事实", res_zero)
            mock_engine.search.assert_not_called()

            res_neg = search_memories("query", limit=-5)
            self.assertIn("未找到与 'query' 相关的记忆事实", res_neg)
            mock_engine.search.assert_not_called()

            # limit=100: automatically clamped to max_injected (3)
            search_memories("query", limit=100)
            mock_engine.search.assert_called_once()
            _, kwargs = mock_engine.search.call_args
            self.assertEqual(kwargs.get("limit"), 3)

    def test_cli_search_command_threshold_passthrough(self):
        """CLI search command must support --threshold parameter and forward it correctly to Engine."""
        from typer.testing import CliRunner
        from unittest.mock import patch, MagicMock
        from hippo_memory.cli import app

        runner = CliRunner()
        mock_engine = MagicMock()
        mock_engine.search.return_value = [
            {
                "id": "m1",
                "memory": "测试记忆",
                "agent_id": "test_repo",
                "score": 0.55,
            }
        ]

        with patch("hippo_memory.cli._get_engine", return_value=mock_engine):
            result = runner.invoke(app, ["search", "test", "--threshold", "0.2"])
            self.assertEqual(result.exit_code, 0)
            mock_engine.search.assert_called_once()
            _, kwargs = mock_engine.search.call_args
            self.assertEqual(kwargs.get("threshold"), 0.2)
            # Verify output contains relevance score column or value
            self.assertIn("0.55", result.output)


if __name__ == "__main__":
    unittest.main()
