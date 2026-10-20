import json
import uuid

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from djedai.agent.prompts import load_prompt
from djedai.analytics.registry import RunSqlParams, get_tools_for_llm
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


def load_ctx(state: AgentState, config: RunnableConfig) -> dict:
    """Loads the catalog and context for the specific dataset, and resets per-turn state."""
    catalog_loader = config.get("configurable", {}).get("catalog_loader")
    catalog_prompt = (
        catalog_loader(state["dataset_id"]).to_prompt()
        if catalog_loader is not None
        else f"Catalog mapped for {state.get('dataset_id')}"
    )
    return {
        "catalog_prompt": catalog_prompt,
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
            return {
                "understanding": Understanding(intent="ambiguous"),
                "answer_blocks": [Block(type="error", markdown=f"API Error: {response.message}")],
            }

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

    last_msg = state.get("messages", [])[-1].content if state.get("messages") else ""
    user_prompt = (
        state.get("understanding").resolved_question if state.get("understanding") else None
    )
    user_prompt = user_prompt or last_msg or "Generate execution plan."

    if client:
        response = client.generate_typed_response(sys_prompt, user_prompt, PlanResponse)
        if response.status == "success" and response.data:
            return {"plan": response.data.plan, "retries": state.get("retries", 0) + 1}
        return {
            "plan": [],
            "retries": state.get("retries", 0) + 1,
            "answer_blocks": [Block(type="error", markdown=f"API Error: {response.message}")],
        }

    return {"plan": state.get("plan", []), "retries": state.get("retries", 0) + 1}


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
                # Prevents the injection of configuration parameters such as `max_rows`.
                validated_args = RunSqlParams(**step.args).model_dump(
                    exclude_unset=True, exclude_none=True
                )
                res = run_sql(**validated_args)

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

    existing_blocks = state.get("answer_blocks", [])
    if any(b.type == "error" for b in existing_blocks):
        return {"answer_blocks": existing_blocks}

    facts_and_data = []
    for r in state.get("results", []):
        if r.status == "ok":
            entry = {"tool": r.tool}
            if r.facts:
                entry["facts"] = r.facts
            if r.data:
                entry["data"] = r.data
            facts_and_data.append(entry)

    sys_prompt = load_prompt("compose").format(facts=str(facts_and_data))

    last_msg = state.get("messages", [])[-1].content if state.get("messages") else ""
    user_prompt = (
        state.get("understanding").resolved_question if state.get("understanding") else None
    )
    user_prompt = user_prompt or last_msg or "Explain the results."
    if last_msg and user_prompt != last_msg:
        user_prompt += f"\nOriginal question and response requirements: {last_msg}"

    if client:
        response = client.generate_typed_response(sys_prompt, user_prompt, ComposeResponse)
        if response.status == "success" and response.data:
            blocks = response.data.answer_blocks
        else:
            blocks = [Block(type="error", markdown=f"API Error: {response.message}")]
    else:
        blocks = [Block(type="text", markdown=f"Based on facts: {facts_and_data}")]

    return {"answer_blocks": blocks}


def verify(state: AgentState) -> dict:
    # Final check of numbers in text vs data
    return {}


def clarify(state: AgentState) -> dict:
    existing = state.get("answer_blocks", [])
    # Do not suppress API errors if they are already present in the response blocks
    if any(b.type == "error" for b in existing):
        return {"answer_blocks": existing}

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
    plan_len = len(state.get("plan", []))
    recent_results = state.get("results", [])[-plan_len:] if plan_len > 0 else []
    has_errors = any(r.status == "error" for r in recent_results)

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
