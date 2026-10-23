import json
import uuid
from collections.abc import Callable
from contextlib import nullcontext

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
    VerifyResponse,
)
from djedai.contracts.tool_result import ToolError, ToolResult
from djedai.execution import TurnCancelled, TurnTimeout, current_budget


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
        "compose_retries": 0,
        "is_verified": False,
    }


def understand(state: AgentState, config: RunnableConfig) -> dict:
    """Invokes the model to understand intent."""
    client = config.get("configurable", {}).get("client")
    messages = state.get("messages", [])
    last_msg = messages[-1].content.strip() if messages else ""

    # Short-circuit blank messages without wasting LLM tokens. A state without any
    # messages is a resume/seed invocation (e.g. with a pre-built plan), not an
    # empty user query, so it must keep flowing through the graph.
    if messages and not last_msg:
        return {"understanding": Understanding(intent="off_topic", entities=Entities())}

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
            if budget := current_budget():
                budget.check()
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

        except (TurnTimeout, TurnCancelled):
            raise
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

    # Hint for the retry iteration to explicitly fix hallucinations
    if state.get("compose_retries", 0) > 0:
        user_prompt += (
            "\n\nWARNING: Your previous response contained unverified or hallucinated numbers. "
            "Please carefully check formatting, rounding, and units, "
            "and ensure ALL numbers strictly match the facts."
        )

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

    return {"answer_blocks": blocks, "compose_retries": state.get("compose_retries", 0) + 1}


def verify(state: AgentState, config: RunnableConfig) -> dict:
    """Final check of numbers in text vs data."""
    client = config.get("configurable", {}).get("client")
    blocks = state.get("answer_blocks", [])

    text_blocks = [b for b in blocks if b.type == "text"]
    if not text_blocks:
        return {"is_verified": True}

    draft = "\n\n".join(b.markdown for b in text_blocks if b.markdown)

    facts_and_data = []
    for r in state.get("results", []):
        if r.status == "ok":
            entry = {"tool": r.tool}
            if r.facts:
                entry["facts"] = r.facts
            if r.data:
                entry["data"] = r.data
            facts_and_data.append(entry)

    if not facts_and_data:
        new_blocks = [b for b in blocks if b.type != "text"]
        new_blocks.append(Block(type="text", markdown="Нет данных для формирования ответа."))
        return {"is_verified": True, "answer_blocks": new_blocks}

    if client:
        sys_prompt = load_prompt("verify").format(facts=str(facts_and_data), draft=draft)
        response = client.generate_typed_response(
            sys_prompt, "Verify the numbers and formats.", VerifyResponse
        )

        if response.status == "success" and response.data:
            v_data = response.data
            if v_data.is_valid:
                return {"is_verified": True}
            else:
                # If we've reached max retries, accept the corrected/stripped markdown
                if state.get("compose_retries", 0) >= 2:
                    new_blocks = [b for b in blocks if b.type != "text"]
                    new_blocks.append(Block(type="text", markdown=v_data.corrected_markdown))
                    return {"is_verified": True, "answer_blocks": new_blocks}
                else:
                    # Triggers conditional route to loop back to compose
                    return {"is_verified": False}
        else:
            # The API failed (timeout, 500, etc.).
            # We do not allow the publication of an unvalidated draft.
            new_blocks = [b for b in blocks if b.type != "text"]
            new_blocks.append(
                Block(
                    type="error",
                    markdown=(
                        "Ошибка проверки ответа: недоступен API верификации. "
                        "Неподтвержденный ответ удален."
                    ),
                )
            )
            return {"is_verified": True, "answer_blocks": new_blocks}

    return {"is_verified": True}


def clarify(state: AgentState) -> dict:
    existing = state.get("answer_blocks", [])
    # Do not suppress API errors if they are already present in the response blocks
    if any(b.type == "error" for b in existing):
        return {"answer_blocks": existing}

    understanding = state.get("understanding")
    intent = understanding.intent if understanding else "invalid"

    blocks = []

    if intent in ["ambiguous", "invalid"]:
        if understanding and understanding.clarification:
            opts = "\n".join(f"- {opt}" for opt in understanding.clarification.options)
            msg = f"{understanding.clarification.question}\n{opts}"
            blocks.append(Block(type="clarification", markdown=msg))
        else:
            if intent == "ambiguous":
                msg = (
                    "Запрос неоднозначен. Пожалуйста, уточните, "
                    "какие именно данные вы имеете в виду."
                )
                blocks.append(Block(type="clarification", markdown=msg))
            else:
                msg = (
                    "Запрашиваемые поля или метрики не найдены в данных. "
                    "Пожалуйста, уточните ваш запрос или проверьте названия."
                )
                blocks.append(Block(type="error", markdown=msg))

    elif intent == "off_topic":
        msg = (
            "Я — аналитический ИИ-агент. Я могу исследовать датасеты, "
            "писать SQL-запросы, строить графики и дашборды по вашим данным. "
            "Ваш запрос выходит за рамки моих возможностей."
        )
        blocks.append(Block(type="error", markdown=msg))

    else:
        blocks.append(Block(type="clarification", markdown="Пожалуйста, уточните ваш запрос."))

    return {
        "plan": [],
        "answer_blocks": blocks,
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


def route_after_verify(state: AgentState) -> str:
    if state.get("is_verified", True):
        return END
    if state.get("compose_retries", 0) < 2:
        return "compose"
    return END


def build_agent_graph() -> StateGraph:
    workflow = StateGraph(AgentState)

    def bounded(function: Callable, *, state_only: bool = False) -> Callable:
        def node(state: AgentState, config: RunnableConfig) -> dict:
            budget = config.get("configurable", {}).get("budget")
            with budget.activate() if budget else nullcontext():
                return function(state) if state_only else function(state, config)

        return node

    workflow.add_node("load_ctx", bounded(load_ctx))
    workflow.add_node("understand", bounded(understand))
    workflow.add_node("plan", bounded(plan))
    workflow.add_node("execute", bounded(execute))
    workflow.add_node("visualize", bounded(visualize, state_only=True))
    workflow.add_node("compose", bounded(compose))
    workflow.add_node("verify", bounded(verify))
    workflow.add_node("clarify", bounded(clarify, state_only=True))

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

    # Conditional edge to handle regeneration
    workflow.add_conditional_edges("verify", route_after_verify, {"compose": "compose", END: END})

    workflow.add_edge("clarify", END)

    return workflow.compile(checkpointer=MemorySaver())
