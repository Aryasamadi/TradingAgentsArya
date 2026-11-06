import logging
from collections import Counter
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_fundamentals_analyst,
    create_market_analyst,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.analysts.turn import WRAP_UP
from tradingagents.agents.state import AgentState
from tradingagents.llm_clients.base_client import describe_error, run_with_rpm_retries

from .analyst_execution import build_analyst_execution_plan
from .conditional_logic import ConditionalLogic

logger = logging.getLogger(__name__)

# Every target a shared conditional router can return. Each edge driven by the
# router maps all of them, so a fall-through return (e.g. under prompt/i18n/
# refactor drift in the speaker labels) can never hit a missing path_map entry
# and crash LangGraph mid-run (#1088).
DEBATE_PATH_MAP = {
    "Bull Researcher": "Bull Researcher",
    "Bear Researcher": "Bear Researcher",
    "Research Manager": "Research Manager",
}
RISK_ANALYSIS_PATH_MAP = {
    "Aggressive Analyst": "Aggressive Analyst",
    "Conservative Analyst": "Conservative Analyst",
    "Neutral Analyst": "Neutral Analyst",
    "Portfolio Manager": "Portfolio Manager",
}

ANALYST_FAILURE_PREFIX = "⚠️ ANALYST FAILURE"


def _analyst_failure_report(agent_name: str, exc: Exception) -> str:
    """Make a visible, explicit failure marker instead of silently omitting a report."""
    return (
        f"{ANALYST_FAILURE_PREFIX} — {agent_name}\n"
        f"Error type: {type(exc).__name__}\n"
        f"Details: {describe_error(exc)}\n"
        "This analyst report is unavailable. This failure is not a bullish or bearish signal; "
        "do not infer market facts from it."
    )


def _guard_agent_node(agent_name: str, node):
    """Retry only request-RPM throttling; log and propagate every other node failure."""
    def guarded(state):
        try:
            return run_with_rpm_retries(lambda: node(state), agent_name=agent_name)
        except Exception as exc:
            logger.error(
                "%s failed; stopping the analysis run (%s): %s",
                agent_name, type(exc).__name__, describe_error(exc),
            )
            raise
    return guarded


def _check_analyst_results(state, specs):
    """Continue with partial research, but never trade on an all-failed analyst set."""
    failures = []
    successes = []
    for spec in specs:
        report = str(state.get(spec.report_key) or "").strip()
        if report.startswith(ANALYST_FAILURE_PREFIX):
            failures.append((spec.agent_node, report))
        elif report:
            successes.append(spec.agent_node)

    if not successes:
        details = "\n\n".join(report for _, report in failures)
        raise RuntimeError(
            "All selected analysts failed; no trade decision will be generated.\n" + details
        )
    if failures:
        logger.warning(
            "Continuing with partial analyst coverage. Failed analysts: %s",
            ", ".join(name for name, _ in failures),
        )
    return {}


def _tools_or_done(state) -> str:
    """Route an analyst's turn: run its tool calls, or finish with its report."""
    return "tools" if state["messages"][-1].tool_calls else END


def _analyst_graph(spec, agent, max_tool_rounds: int):
    """One analyst as a graph of its own: the model and its tools, on a private message history.

    It returns only its report, so analysts running side by side never write the
    same key, and its tool calls never reach the other analysts' messages. After
    ``max_tool_rounds`` rounds of tool calls it is told to write its report, and
    that turn ends it whatever it answers, so a model that keeps calling tools
    cannot run the graph into its recursion limit (#1420).
    """
    output = TypedDict(f"{spec.key.capitalize()}Report", {spec.report_key: str})
    graph = StateGraph(AgentState, output_schema=output)

    def retrying_agent(state):
        return run_with_rpm_retries(lambda: agent(state), agent_name=spec.agent_node)

    graph.add_node("agent", retrying_agent)
    graph.add_edge(START, "agent")
    if not spec.tools:
        graph.add_edge("agent", END)
        return graph.compile()

    def calls(messages):
        return [call["name"] for m in messages for call in (getattr(m, "tool_calls", None) or [])]

    def rounds(messages) -> int:
        return sum(1 for m in messages if getattr(m, "tool_calls", None))

    def more_or_wrap_up(state) -> str:
        return "wrap_up" if rounds(state["messages"]) >= max_tool_rounds else "agent"

    def wrap_up(state):
        repeated = ", ".join(f"{name} x{n}" for name, n in Counter(calls(state["messages"])).most_common())
        logger.warning("%s used its %d tool rounds (%s); asking for its report",
                       spec.agent_node, max_tool_rounds, repeated)
        return retrying_agent({**state, "messages": [*state["messages"], HumanMessage(WRAP_UP)]})

    graph.add_node("tools", ToolNode(list(spec.tools)))
    graph.add_node("wrap_up", wrap_up)
    graph.add_conditional_edges("agent", _tools_or_done, ["tools", END])
    graph.add_conditional_edges("tools", more_or_wrap_up, ["agent", "wrap_up"])
    graph.add_edge("wrap_up", END)
    compiled = graph.compile()

    def isolated_analyst(state, config=None, **kwargs):
        # A failed analyst must not cancel its healthy parallel peers. Its
        # report key carries a visible diagnostic for the downstream agents.
        try:
            return compiled.invoke(state, config=config, **kwargs)
        except Exception as exc:
            logger.error(
                "%s failed; continuing without this analyst (%s): %s",
                spec.agent_node, type(exc).__name__, describe_error(exc),
            )
            return {spec.report_key: _analyst_failure_report(spec.agent_node, exc)}

    return isolated_analyst


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        conditional_logic: ConditionalLogic,
        max_tool_rounds: int,
    ):
        """Initialize with required components."""
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.conditional_logic = conditional_logic
        self.max_tool_rounds = max_tool_rounds

    def setup_graph(
        self, selected_analysts=("market", "social", "news", "fundamentals"), memory_node=None
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include. Options are:
                - "market": Market analyst
                - "social": Sentiment analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
            memory_node: Node that settles past decisions and returns the run's
                ``past_context``. It runs alongside the analysts.
        """
        plan = build_analyst_execution_plan(selected_analysts)

        analyst_factories = {
            "market": lambda: create_market_analyst(self.quick_thinking_llm),
            "social": lambda: create_sentiment_analyst(self.quick_thinking_llm),
            "news": lambda: create_news_analyst(self.quick_thinking_llm),
            "fundamentals": lambda: create_fundamentals_analyst(self.quick_thinking_llm),
        }

        bull_researcher_node = create_bull_researcher(self.quick_thinking_llm)
        bear_researcher_node = create_bear_researcher(self.quick_thinking_llm)
        research_manager_node = create_research_manager(self.deep_thinking_llm)
        trader_node = create_trader(self.quick_thinking_llm)

        aggressive_analyst = create_aggressive_debator(self.quick_thinking_llm)
        neutral_analyst = create_neutral_debator(self.quick_thinking_llm)
        conservative_analyst = create_conservative_debator(self.quick_thinking_llm)
        portfolio_manager_node = create_portfolio_manager(self.deep_thinking_llm)

        workflow = StateGraph(AgentState)

        for spec in plan.specs:
            workflow.add_node(spec.agent_node,
                              _analyst_graph(spec, analyst_factories[spec.key](), self.max_tool_rounds))

        workflow.add_node("Bull Researcher", _guard_agent_node("Bull Researcher", bull_researcher_node))
        workflow.add_node("Bear Researcher", _guard_agent_node("Bear Researcher", bear_researcher_node))
        workflow.add_node("Research Manager", _guard_agent_node("Research Manager", research_manager_node))
        workflow.add_node("Trader", _guard_agent_node("Trader", trader_node))
        workflow.add_node("Aggressive Analyst", _guard_agent_node("Aggressive Analyst", aggressive_analyst))
        workflow.add_node("Neutral Analyst", _guard_agent_node("Neutral Analyst", neutral_analyst))
        workflow.add_node("Conservative Analyst", _guard_agent_node("Conservative Analyst", conservative_analyst))
        workflow.add_node("Portfolio Manager", _guard_agent_node("Portfolio Manager", portfolio_manager_node))

        # The analysts work at the same time; the research debate starts once
        # every one of them has filed its report. The memory log settles past
        # decisions alongside them: only the Portfolio Manager reads its lessons.
        first_steps = [spec.agent_node for spec in plan.specs]
        if memory_node is not None:
            workflow.add_node("Memory Log", memory_node)
            first_steps.append("Memory Log")
        for node in first_steps:
            workflow.add_edge(START, node)
        workflow.add_node(
            "Check Analyst Results",
            lambda state: _check_analyst_results(state, plan.specs),
        )
        workflow.add_edge(first_steps, "Check Analyst Results")
        workflow.add_edge("Check Analyst Results", "Bull Researcher")

        # Both research-debate edges share the complete DEBATE_PATH_MAP (#1088).
        for debate_node in ("Bull Researcher", "Bear Researcher"):
            workflow.add_conditional_edges(
                debate_node,
                self.conditional_logic.should_continue_debate,
                DEBATE_PATH_MAP,
            )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        # All three risk edges share the complete RISK_ANALYSIS_PATH_MAP (#1088).
        for risk_node in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
            workflow.add_conditional_edges(
                risk_node,
                self.conditional_logic.should_continue_risk_analysis,
                RISK_ANALYSIS_PATH_MAP,
            )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
