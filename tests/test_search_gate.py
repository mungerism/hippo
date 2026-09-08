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
        """空候选列表安全返回空列表。"""
        self.assertEqual(filter_search_results([], config=self.default_config, limit=5), [])

    def test_all_low_scores_returns_empty(self):
        """全低分噪音场景下 Fail-Closed，绝不强行凑数。"""
        candidates = [
            self._make_candidate("1", "无关记忆1", 0.29, 0.58),
            self._make_candidate("2", "无关记忆2", 0.28, 0.56),
            self._make_candidate("3", "无关记忆3", 0.20, 0.40),
        ]
        result = filter_search_results(candidates, config=self.default_config, limit=5)
        self.assertEqual(result, [])

    def test_real_scene_regression_pr_merge(self):
        """现场真实复现：0.34 纯语义直接事实保留，0.30/0.29 噪音被剔除。"""
        candidates = [
            self._make_candidate("1", "PR #7 提交记录", 0.63, 0.70, bm25_score=0.56),
            self._make_candidate("2", "PR #7 代码审查", 0.62, 0.68, bm25_score=0.56),
            self._make_candidate("3", "PR #7 分支测试", 0.60, 0.64, bm25_score=0.56),
            # 关键项：PR #3 合并事实，无 BM25 命中，但语义分高达 0.68，因分母为 2.0 折半为 0.34
            self._make_candidate("4", "PR #3 已合并到 main", 0.34, 0.68, bm25_score=0.0),
            # 噪音项：无 BM25，语义分仅 0.58（底噪），折半为 0.29~0.30
            self._make_candidate("5", "SQLAlchemy 迁移历史", 0.30, 0.58, bm25_score=0.0),
            self._make_candidate("6", "个人语言偏好为中文", 0.29, 0.57, bm25_score=0.0),
        ]

        # 当 limit=5 时，前 4 条通过，后 2 条（0.30, 0.29）被严格剔除
        res_limit_5 = filter_search_results(candidates, config=self.default_config, limit=5)
        self.assertEqual(len(res_limit_5), 4)
        self.assertEqual([r["id"] for r in res_limit_5], ["1", "2", "3", "4"])

        # 当 limit=3 时，只截取通过门禁的前 3 条
        res_limit_3 = filter_search_results(candidates, config=self.default_config, limit=3)
        self.assertEqual(len(res_limit_3), 3)
        self.assertEqual([r["id"] for r in res_limit_3], ["1", "2", "3"])

    def test_dense_only_threshold(self):
        """无 BM25/entity 支撑时，必须严格满足 dense_only_threshold (0.62)。"""
        # semantic_score < 0.62，即使 final_score 达到 0.35 也拒绝
        cand_low_dense = self._make_candidate("c1", "弱语义候选", 0.35, 0.60, max_possible=1.0)
        # semantic_score >= 0.62 且 final_score >= 0.32，通过
        cand_high_dense = self._make_candidate("c2", "强语义候选", 0.65, 0.65, max_possible=1.0)

        res = filter_search_results([cand_low_dense, cand_high_dense], config=self.default_config, limit=5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "c2")

    def test_bm25_or_entity_support_relaxes_semantic_threshold(self):
        """有 BM25 或 entity 支撑时放宽语义门槛，只要综合分达标即通过。"""
        # BM25 支撑：semantic_score 仅 0.50 (< 0.62)，但 bm25 > 0 且 final_score=0.35 >= 0.32
        cand_bm25 = self._make_candidate("b1", "BM25命中项", 0.35, 0.50, bm25_score=0.20)
        # Entity 支撑：semantic_score 仅 0.48 (< 0.62)，但 entity_boost > 0 且 final_score=0.36 >= 0.32
        cand_entity = self._make_candidate("e1", "Entity命中项", 0.36, 0.48, entity_boost=0.24)

        res = filter_search_results([cand_bm25, cand_entity], config=self.default_config, limit=5)
        self.assertEqual(len(res), 2)
        self.assertEqual([r["id"] for r in res], ["b1", "e1"])

    def test_relative_threshold_ratio(self):
        """相对最高分动态比例截断：显著低于最高分 50% 的尾部项被剔除。"""
        # 最高分为 0.80，动态相对下限为 0.80 * 0.50 = 0.40
        # 候选 2 综合分为 0.35（虽大于静态 0.32），但低于 0.40，应被拒绝
        cand_best = self._make_candidate("top", "极高相关记忆", 0.80, 0.80, bm25_score=0.80)
        cand_gap = self._make_candidate("gap", "相对断崖低分记忆", 0.35, 0.70)

        res = filter_search_results([cand_best, cand_gap], config=self.default_config, limit=5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "top")

    def test_best_score_calculated_only_from_absolute_pass(self):
        """最高分只能从通过绝对门禁的候选池中计算，畸形高分噪声不得抬高门槛。"""
        # 畸形项：score 标为 0.99，但缺少 score_details（绝对门禁拒绝）
        invalid_high = {"id": "bad", "memory": "畸形项", "score": 0.99}
        # 正常项：score 0.40，通过绝对门禁。如果从 invalid_high 算相对分 (0.99 * 0.5 = 0.495) 它会被误杀
        valid_item = self._make_candidate("good", "正常合格项", 0.40, 0.80)

        res = filter_search_results([invalid_high, valid_item], config=self.default_config, limit=5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "good")

    def test_fail_closed_invalid_score_details(self):
        """严格 Fail-Closed：各种异常结构直接拒绝，绝不盲目放行。"""
        bad_cases = [
            # 缺失 score_details
            {"id": "b1", "memory": "m", "score": 0.8},
            # score_details 不是 dict
            {"id": "b2", "memory": "m", "score": 0.8, "score_details": "invalid"},
            # 缺失 final_score
            {"id": "b3", "memory": "m", "score": 0.8, "score_details": {"semantic_score": 0.8}},
            # 顶层 score 与 final_score 不一致 (> 1e-4)
            {
                "id": "b4",
                "memory": "m",
                "score": 0.8,
                "score_details": {"semantic_score": 0.8, "final_score": 0.5},
            },
            # NaN 分数
            {
                "id": "b5",
                "memory": "m",
                "score": float("nan"),
                "score_details": {"semantic_score": float("nan"), "final_score": float("nan")},
            },
            # Inf 分数
            {
                "id": "b6",
                "memory": "m",
                "score": float("inf"),
                "score_details": {"semantic_score": float("inf"), "final_score": float("inf")},
            },
            # 字符串分数
            {
                "id": "b7",
                "memory": "m",
                "score": "0.8",
                "score_details": {"semantic_score": "0.8", "final_score": "0.8"},
            },
        ]
        res = filter_search_results(bad_cases, config=self.default_config, limit=5)
        self.assertEqual(res, [])

    def test_boundary_equality_conditions(self):
        """临界值相等判定：== 阈值时均通过。"""
        # final_score 恰好等于 0.32，semantic_score 恰好等于 0.62
        exact_item = self._make_candidate("exact", "临界项", 0.32, 0.62)
        res = filter_search_results([exact_item], config=self.default_config, limit=5)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "exact")

    def test_limit_zero_and_negative(self):
        """limit <= 0 时安全返回空列表。"""
        item = self._make_candidate("1", "合格项", 0.60, 0.80)
        self.assertEqual(filter_search_results([item], config=self.default_config, limit=0), [])
        self.assertEqual(filter_search_results([item], config=self.default_config, limit=-1), [])

    def test_immutability_and_order_preservation(self):
        """不修改入参对象，且严格保持 Mem0 的初始排序。"""
        c1 = self._make_candidate("1", "第一名", 0.70, 0.70)
        c2 = self._make_candidate("2", "第二名", 0.65, 0.65)
        original_c1_score = c1["score"]

        res = filter_search_results([c1, c2], config=self.default_config, limit=5)
        self.assertEqual([r["id"] for r in res], ["1", "2"])
        # 入参未被修改
        self.assertEqual(c1["score"], original_c1_score)
        # 返回的是浅拷贝字典，不是同一引用
        self.assertIsNot(res[0], c1)

    def test_gate_disabled(self):
        """当 enabled=False 时，跳过相关度门禁，但仍按 limit 截断。"""
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
        """配置阈值必须在 [0.0, 1.0] 范围内且为有限浮点数。"""
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
    """零网络开销的本地 Mem0 契约测试，断言 scoring 与 explain 数据结构未漂移。"""

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
        # 确保全部为全小写 snake_case
        for k in details.keys():
            self.assertEqual(k, k.lower())
            self.assertFalse(any(c.isupper() for c in k))


class TestEngineSearchWiring(unittest.TestCase):
    """Engine 层的检索参数透传、宽候选池计算与门禁接线测试 (使用 Mock Memory)。"""

    def setUp(self):
        from unittest.mock import MagicMock
        from hippo_memory.engine import HippoEngine

        self.engine = HippoEngine.__new__(HippoEngine)
        self.mock_memory = MagicMock()
        self.engine._memory = self.mock_memory

        # 模拟免副作用的 config
        config_mock = MagicMock()
        config_mock.user_id = "test_user"
        config_mock.semantic_threshold = 0.1
        config_mock.final_threshold = 0.32
        config_mock.dense_only_threshold = 0.62
        config_mock.relative_threshold_ratio = 0.50
        config_mock.gate_enabled = True
        config_mock.get_gate_config.return_value = SearchGateConfig()
        self.engine.config = config_mock

        # 模拟 router
        router_mock = MagicMock()
        router_mock.build_search_filters.return_value = {"user_id": "test_user"}
        self.engine.router = router_mock

    def test_engine_search_explain_and_threshold_passthrough(self):
        """Engine.search 必须传递 explain=True，并使用配置中的 semantic_threshold。"""
        self.mock_memory.search.return_value = []

        res = self.engine.search("test query", limit=5)
        self.assertEqual(res, [])
        self.mock_memory.search.assert_called_once()

        _, kwargs = self.mock_memory.search.call_args
        self.assertEqual(kwargs.get("explain"), True)
        self.assertEqual(kwargs.get("threshold"), 0.1)
        self.assertEqual(kwargs.get("top_k"), 20)  # max(5 * 4, 20) == 20

    def test_engine_search_threshold_zero_preservation(self):
        """当显式传入 threshold=0.0 时，必须原样透传，不能被误吞退回默认值。"""
        self.mock_memory.search.return_value = []

        self.engine.search("test query", threshold=0.0)
        _, kwargs = self.mock_memory.search.call_args
        self.assertEqual(kwargs.get("threshold"), 0.0)

    def test_engine_search_limit_zero_does_not_call_memory(self):
        """当 limit <= 0 时，直接返回空列表，绝不调用底层的 Memory.search。"""
        res_zero = self.engine.search("query", limit=0)
        self.assertEqual(res_zero, [])
        self.mock_memory.search.assert_not_called()

        res_neg = self.engine.search("query", limit=-2)
        self.assertEqual(res_neg, [])
        self.mock_memory.search.assert_not_called()

    def test_engine_search_applies_gate_and_handles_dict_results(self):
        """Engine 能处理 Mem0 返回 dict 的形态，并正确应用门禁过滤与截断。"""
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
    """MCP 极简安全契约与 CLI 调试选项测试。"""

    def test_mcp_search_memories_schema_has_no_threshold_or_gate_params(self):
        """MCP 接口绝不对 Agent 暴露 threshold、explain 或门禁内部配置。"""
        import asyncio
        from hippo_memory.server import mcp_server

        tools = asyncio.run(mcp_server.list_tools())
        search_tool = next(t for t in tools if t.name == "search_memories")
        properties = search_tool.input_schema.get("properties", {})

        forbidden = {"threshold", "explain", "gate_config", "final_threshold", "dense_only_threshold"}
        for f in forbidden:
            self.assertNotIn(f, properties, f"MCP schema 不应暴露内部参数: {f}")

    def test_mcp_search_memories_clamps_limit_and_handles_zero(self):
        """MCP 必须将 Agent 传入的超大 limit 钳制到 max_injected，且 limit<=0 时不调用 engine。"""
        from unittest.mock import patch, MagicMock
        from hippo_memory.server import search_memories

        mock_engine = MagicMock()
        mock_engine.config.max_injected = 3
        mock_engine.search.return_value = []

        with patch("hippo_memory.server.get_engine", return_value=mock_engine):
            # limit <= 0: 直接返回友好提示，不调用 engine.search
            res_zero = search_memories("query", limit=0)
            self.assertIn("未找到与 'query' 相关的记忆事实", res_zero)
            mock_engine.search.assert_not_called()

            res_neg = search_memories("query", limit=-5)
            self.assertIn("未找到与 'query' 相关的记忆事实", res_neg)
            mock_engine.search.assert_not_called()

            # limit=100: 自动被 clamp 到 max_injected (3)
            search_memories("query", limit=100)
            mock_engine.search.assert_called_once()
            _, kwargs = mock_engine.search.call_args
            self.assertEqual(kwargs.get("limit"), 3)

    def test_cli_search_command_threshold_passthrough(self):
        """CLI search 命令必须支持 --threshold 参数，并正确传递给 Engine。"""
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
            # 确认输出中包含了相关度列或数值
            self.assertIn("0.55", result.output)


if __name__ == "__main__":
    unittest.main()
