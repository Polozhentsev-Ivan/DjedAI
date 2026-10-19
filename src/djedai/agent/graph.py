import json
import uuid

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from djedai.agent.prompts import load_prompt
from djedai.analytics.registry import get_tools_for_llm
from djedai.analytics.tools import run_sql
from djedai.contracts.agent import (
    AgentState,
    Block,
    ComposeResponse,
    Entities,
    PlanResponse,
    Understanding,
)
from djedai.contracts.tool_result import ToolError, ToolResult


def load_ctx(state: AgentState) -> dict:
    """Loads the catalog and context for the specific dataset, and resets per-turn state."""
    # Stub: Real implementation will fetch from DB based on state["dataset_id"]
    return {
        "catalog_prompt": f"Catalog mapped for {state.get('dataset_id')}",
        "results": [],
        "charts": [],
        "answer_blocks": [],
        "retries": 0,
    }


def understand(state: AgentState, config: RunnableConfig) -> dict:
    """Invokes the model to understand intent."""
    client = config.get("configurable", {}).get("client")
    last_msg = state.get("messages", [])[-1].content if state.get("messages") else ""

    if client:
        sys_prompt = load_prompt("understand").format(catalog=state.get("catalog_prompt"))
        response = client.generate_typed_response(sys_prompt, last_msg, Understanding)
        if response.status == "success" and response.data:
            return {"understanding": response.data}
        else:
            # Fallback (например, при ошибке сервера)
            return {"understanding": Understanding(intent="data_question")}

    intent = "ambiguous" if "ambiguous" in last_msg.lower() else "data_question"
    return {"understanding": Understanding(intent=intent, entities=Entities())}


def plan(state: AgentState, config: RunnableConfig) -> dict:
    client = config.get("configurable", {}).get("client")

    registry = get_tools_for_llm()
    tools_desc = "\n".join(
        f"- {t['name']}: {t['description']} \n  Schema: {json.dumps(t['parameters_schema'])}"
        for t in registry
    )

    # Format the retry context
    results_str = "\n".join(
        f"Tool {r.tool}: {r.status} - {r.error.message if r.error else 'Success'}"
        for r in state.get("results", [])
    )

    sys_prompt = load_prompt("plan").format(
        catalog=state.get("catalog_prompt"), results=results_str, tools_registry=tools_desc
    )
    user_prompt = state.get("understanding").resolved_question or "Generate execution plan."

    if client:
        response = client.generate_typed_response(sys_prompt, user_prompt, PlanResponse)
        new_plan = response.data.plan if response.status == "success" and response.data else []
    else:
        new_plan = state.get("plan", [])

    return {"plan": new_plan, "retries": state.get("retries", 0) + 1}


def execute(state: AgentState, config: RunnableConfig) -> dict:
    plan_steps = state.get("plan", [])
    existing_results = state.get("results", [])
    new_results = []

    # In production, dispatcher comes from djedai.analytics.registry
    dispatcher = config.get("configurable", {}).get("tool_dispatcher")

    for step in plan_steps:
        try:
            if dispatcher:
                res = dispatcher(step.tool, step.args)
            elif step.tool == "run_sql":
                res = run_sql(**step.args)
            else:
                res = ToolResult(
                    result_id=f"r_{uuid.uuid4().hex[:6]}",
                    status="error",
                    error=ToolError(
                        code="not_implemented",
                        message=f"Инструмент '{step.tool}' еще не подключен к агенту.",
                    ),
                )

            # Ensure metadata is stamped on the result (ToolResult is frozen)
            res = res.model_copy(update={"tool": step.tool, "params": step.args})
            new_results.append(res)

        except Exception as e:
            new_results.append(
                ToolResult(
                    result_id=f"r_{uuid.uuid4().hex[:6]}",
                    status="error",
                    tool=step.tool,
                    params=step.args,
                    error=ToolError(code="execution_failed", message=str(e)),
                )
            )

    return {"results": existing_results + new_results}


def visualize(state: AgentState) -> dict:
    return {"charts": []}


def compose(state: AgentState, config: RunnableConfig) -> dict:
    client = config.get("configurable", {}).get("client")

    facts = []
    for r in state.get("results", []):
        if r.status == "ok" and r.facts:
            facts.extend(r.facts)

    sys_prompt = load_prompt("compose").format(facts=str(facts))

    if client and facts:
        response = client.generate_typed_response(
            sys_prompt, "Explain the results.", ComposeResponse
        )
        blocks = (
            response.data.answer_blocks if response.status == "success" and response.data else []
        )
    else:
        blocks = [Block(type="text", markdown="Based on facts: " + str(facts))]

    return {"answer_blocks": blocks}


def verify(state: AgentState) -> dict:
    # Final check of numbers in text vs data
    return {}


def clarify(state: AgentState) -> dict:
    return {
        "plan": [],
        "answer_blocks": [Block(type="clarification", markdown="Please clarify your request.")],
    }


def route_after_understand(state: AgentState) -> str:
    """Conditional edge: Routes off_topic/invalid/ambiguous to clarify, else plans."""
    intent = state["understanding"].intent
    if intent in ["ambiguous", "invalid", "off_topic"]:
        return "clarify"
    return "plan"


def route_after_execute(state: AgentState) -> str:
    """Conditional edge: Route to plan if errors exist and retries <= 2, else visualize."""
    results = state.get("results", [])
    has_errors = bool(results) and results[-1].status == "error"

    if has_errors and state.get("retries", 0) <= 2:
        return "plan"
    return "visualize"


def build_agent_graph() -> StateGraph:
    workflow = StateGraph(AgentState)

    workflow.add_node("load_ctx", load_ctx)
    workflow.add_node("understand", understand)
    workflow.add_node("plan", plan)
    workflow.add_node("execute", execute)
    workflow.add_node("visualize", visualize)
    workflow.add_node("compose", compose)
    workflow.add_node("verify", verify)
    workflow.add_node("clarify", clarify)

    workflow.add_edge(START, "load_ctx")
    workflow.add_edge("load_ctx", "understand")

    workflow.add_conditional_edges(
        "understand", route_after_understand, {"plan": "plan", "clarify": "clarify"}
    )

    workflow.add_edge("plan", "execute")

    # Dynamic loop for error correction
    workflow.add_conditional_edges(
        "execute", route_after_execute, {"plan": "plan", "visualize": "visualize"}
    )

    workflow.add_edge("visualize", "compose")
    workflow.add_edge("compose", "verify")
    workflow.add_edge("verify", END)

    workflow.add_edge("clarify", END)

    return workflow.compile(checkpointer=MemorySaver())
