import unittest

from tradingagents.graph.analyst_execution import (
    build_analyst_execution_plan,
)


class AnalystExecutionPlanTests(unittest.TestCase):
    def test_build_plan_preserves_selected_order(self):
        plan = build_analyst_execution_plan(["news", "market"])

        self.assertEqual([spec.key for spec in plan.specs], ["news", "market"])
        self.assertEqual(plan.specs[0].agent_node, "News Analyst")
        self.assertEqual(plan.specs[0].report_key, "news_report")
        self.assertFalse(hasattr(plan.specs[0], "clear_node"))

    def test_rejects_unknown_analyst_keys(self):
        with self.assertRaises(ValueError):
            build_analyst_execution_plan(["market", "macro"])

    def test_social_key_displays_as_sentiment_analyst(self):
        # The wire key stays "social" for saved-config back-compat, but the
        # user-visible agent_node label must match the v0.2.5 rename so the
        # wall-time summary and any future consumer of agent_node says
        # "Sentiment Analyst" rather than the legacy "Social Analyst".
        plan = build_analyst_execution_plan(["social"])
        spec = plan.specs[0]
        self.assertEqual(spec.key, "social")
        self.assertEqual(spec.agent_node, "Sentiment Analyst")
        self.assertEqual(spec.report_key, "sentiment_report")


class AnalystFailureIsolationTests(unittest.TestCase):
    def test_one_no_tool_analyst_failure_becomes_visible_report(self):
        from tradingagents.graph.setup import ANALYST_FAILURE_PREFIX, _analyst_graph
        from tradingagents.graph.analyst_execution import AnalystNodeSpec

        spec = AnalystNodeSpec(
            key="broken",
            agent_node="Broken Analyst",
            report_key="broken_report",
            tools=(),
        )

        def broken_agent(_state):
            raise RuntimeError("simulated provider outage")

        isolated = _analyst_graph(spec, broken_agent, max_tool_rounds=1)
        result = isolated.invoke({"messages": [("human", "AAPL")]})

        self.assertTrue(result["broken_report"].startswith(ANALYST_FAILURE_PREFIX))
        self.assertIn("simulated provider outage", result["broken_report"])

    def test_all_analysts_failed_blocks_trade_path(self):
        from types import SimpleNamespace

        from tradingagents.graph.setup import _check_analyst_results

        specs = [
            SimpleNamespace(agent_node="Market Analyst", report_key="market_report"),
            SimpleNamespace(agent_node="News Analyst", report_key="news_report"),
        ]
        state = {
            "market_report": "⚠️ ANALYST FAILURE — Market Analyst",
            "news_report": "⚠️ ANALYST FAILURE — News Analyst",
        }
        with self.assertRaisesRegex(RuntimeError, "All selected analysts failed"):
            _check_analyst_results(state, specs)

    def test_partial_analyst_failure_is_allowed_when_another_succeeds(self):
        from types import SimpleNamespace

        from tradingagents.graph.setup import _check_analyst_results

        specs = [
            SimpleNamespace(agent_node="Market Analyst", report_key="market_report"),
            SimpleNamespace(agent_node="News Analyst", report_key="news_report"),
        ]
        state = {
            "market_report": "A grounded market report",
            "news_report": "⚠️ ANALYST FAILURE — News Analyst",
        }
        self.assertEqual(_check_analyst_results(state, specs), {})
