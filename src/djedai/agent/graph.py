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
    VisualizeResponse,
)
from djedai.contracts.chart import ChartSpec
from djedai.contracts.tool_result import ToolError, ToolResult


def load_ctx(state: AgentState, config: RunnableConfig) -> dict:
    """Loads the catalog and context for the specific dataset, and resets per-turn state."""
    catalog_loader = config.get("configurable", {}).get("catalog_loader")
    catalog_prompt = (
        catalog_loader(state["dataset_id"]).to_prompt()
        if catalog_loader is not None
        else f"Catalog mapped for {state.get('dataset_id')}"
    )
    # Results already in the state belong to earlier turns: keep them as history
    # before the per-turn results are cleared.
    history_results = list(state.get("history_results", []))
    known_ids = {result.result_id for result in history_results}
    for result in state.get("results", []):
        if result.result_id not in known_ids:
            history_results.append(result)
            known_ids.add(result.result_id)
    return {
        "catalog_prompt": catalog_prompt,
        "history_results": history_results,
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


CHART_INTENTS = {"visualize", "modify_previous", "dashboard"}

# Signals that the user explicitly asked for a visualisation, even when the
# intent was classified as a plain data question.
CHART_REQUEST_WORDS = (
    "chart",
    "graph",
    "plot",
    "histogram",
    "heatmap",
    "scatter",
    "pie",
    "kpi",
    "график",
    "диаграмм",
    "визуализ",
    "гистограмм",
    "теплов",
    "столбчат",
    "кругов",
    "точечн",
)


def wants_visualization(state: AgentState) -> bool:
    """Решает, нужно ли вообще тратить вызов модели на подбор графика."""
    understanding = state.get("understanding")
    if understanding is not None and understanding.intent in CHART_INTENTS:
        return True
    messages = state.get("messages", [])
    text = messages[-1].content.lower() if messages and messages[-1].content else ""
    return any(word in text for word in CHART_REQUEST_WORDS)


def active_results(state: AgentState) -> list[ToolResult]:
    """Возвращает актуальные результаты для визуализации, текста и верификации.

    Берем результаты текущего плана, а при его отсутствии (modify_previous) —
    сохранённые результаты, на которые сослался пользователь.
    """
    current_results = state.get("results", [])
    history_results = state.get("history_results", [])
    plan_len = len(state.get("plan", []))
    if plan_len > 0:
        candidates = current_results[-plan_len:]
    else:
        understanding = state.get("understanding")
        references = understanding.references if understanding else []
        if references:
            saved = {r.result_id: r for r in history_results + current_results}
            candidates = [saved[ref] for ref in references if ref in saved]
        else:
            candidates = (current_results or history_results)[-1:]
    return [r for r in candidates if r.status == "ok"]


def describe_result_for_chart(result: ToolResult) -> dict:
    """Краткое описание результата для модели: поля, подсказка и пример строк."""
    columns = []
    for column in result.data.columns if result.data else []:
        columns.append(column.model_dump(mode="json") if hasattr(column, "model_dump") else column)
    rows = result.data.rows[:5] if result.data else []
    return {
        "result_id": result.result_id,
        "tool": result.tool,
        "columns": columns,
        "sample_rows": rows,
        "chart_hint": result.chart_hint.model_dump(mode="json") if result.chart_hint else None,
        "facts": [fact.text for fact in result.facts],
    }


def sanitize_charts(charts: list[ChartSpec], results: list[ToolResult]) -> list[ChartSpec]:
    """Оставляет только графики с реальными результатами и существующими колонками."""
    results_by_id = {r.result_id: r for r in results}
    sanitized = []
    seen_ids: set[str] = set()
    for chart in charts:
        if chart.result_id not in results_by_id:
            continue

        res = results_by_id[chart.result_id]
        if not res.data:
            continue

        valid_cols = set()
        for c in res.data.columns:
            if isinstance(c, str):
                valid_cols.add(c)
            elif hasattr(c, "name"):
                valid_cols.add(c.name)
            elif isinstance(c, dict) and "name" in c:
                valid_cols.add(c["name"])

        fields_to_check = []
        if chart.x:
            fields_to_check.append(chart.x.field)
        for y in chart.y or []:
            fields_to_check.append(y.field)
        if chart.color:
            fields_to_check.append(chart.color.field)
        if chart.sort:
            fields_to_check.append(chart.sort.field)

        if not all(f in valid_cols for f in fields_to_check):
            continue

        chart_id = chart.chart_id
        if not chart_id or chart_id == "string" or chart_id in seen_ids:
            chart_id = f"ch_{uuid.uuid4().hex[:8]}"
        seen_ids.add(chart_id)
        sanitized.append(chart.model_copy(update={"chart_id": chart_id}))
    return sanitized


def visualize(state: AgentState, config: RunnableConfig) -> dict:
    if not wants_visualization(state):
        return {"charts": []}

    valid_results = active_results(state)
    if not valid_results:
        return {"charts": []}

    results_summary = [describe_result_for_chart(result) for result in valid_results]

    messages = state.get("messages", [])
    last_msg = messages[-1].content if messages else ""
    understanding = state.get("understanding")
    user_prompt = understanding.resolved_question if understanding else None
    user_prompt = user_prompt or last_msg or "Create appropriate visualizations."

    sys_prompt = load_prompt("visualize").format(
        query=user_prompt,
        # Dates and decimals are not JSON-native but are valid tool result values.
        results_summary=json.dumps(results_summary, ensure_ascii=False, indent=2, default=str),
    )

    client = config.get("configurable", {}).get("client")
    if client:
        response = client.generate_typed_response(sys_prompt, user_prompt, VisualizeResponse)
        if response.status == "success" and response.data:
            return {"charts": sanitize_charts(response.data.charts, valid_results)}

    return {"charts": []}


def compose(state: AgentState, config: RunnableConfig) -> dict:
    client = config.get("configurable", {}).get("client")

    existing_blocks = state.get("answer_blocks", [])
    if any(b.type == "error" for b in existing_blocks):
        return {"answer_blocks": existing_blocks}

    facts_and_data = []
    for r in active_results(state):
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

    charts = state.get("charts", [])
    if charts:
        for c in charts:
            blocks.append(Block(type="chart", chart=c.model_dump(mode="json")))

    return {"answer_blocks": blocks, "compose_retries": state.get("compose_retries", 0) + 1}


def verify(state: AgentState, config: RunnableConfig) -> dict:
    """Final check of numbers in text and chart metadata vs data."""
    client = config.get("configurable", {}).get("client")
    blocks = state.get("answer_blocks", [])

    draft_parts = []
    for b in blocks:
        if b.type == "text" and b.markdown:
            draft_parts.append(b.markdown)
        elif b.type == "chart" and b.chart:
            c = b.chart if isinstance(b.chart, dict) else b.chart.model_dump()
            if c.get("title"):
                draft_parts.append(c["title"])
            if c.get("subtitle"):
                draft_parts.append(c["subtitle"])
            if c.get("caption"):
                draft_parts.append(c["caption"])

    if not draft_parts:
        return {"is_verified": True}

    draft = "\n\n".join(draft_parts)

    facts_and_data = []
    for r in active_results(state):
        entry = {"tool": r.tool}
        if r.facts:
            entry["facts"] = r.facts
        if r.data:
            entry["data"] = r.data
        facts_and_data.append(entry)

    if not facts_and_data:
        new_blocks = [b for b in blocks if b.type not in ("text", "chart")]
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
                    new_blocks = [b for b in blocks if b.type not in ("text", "chart")]
                    new_blocks.append(Block(type="text", markdown=v_data.corrected_markdown))
                    return {"is_verified": True, "answer_blocks": new_blocks}
                else:
                    # Triggers conditional route to loop back to compose
                    return {"is_verified": False}
        else:
            # The API failed (timeout, 500, etc.).
            # We do not allow the publication of an unvalidated draft.
            new_blocks = [b for b in blocks if b.type not in ("text", "chart")]
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

    def observed(name: str, function: Callable, *, state_only: bool = False) -> Callable:
        def node(state: AgentState, config: RunnableConfig) -> dict:
            diagnostics = config.get("configurable", {}).get("diagnostics")
            budget = config.get("configurable", {}).get("budget")
            with diagnostics.stage(name) if diagnostics else nullcontext():
                with budget.activate() if budget else nullcontext():
                    return function(state) if state_only else function(state, config)

        return node

    workflow.add_node("load_ctx", observed("load_ctx", load_ctx))
    workflow.add_node("understand", observed("understand", understand))
    workflow.add_node("plan", observed("plan", plan))
    workflow.add_node("execute", observed("execute", execute))
    workflow.add_node("visualize", observed("visualize", visualize, state_only=False))
    workflow.add_node("compose", observed("compose", compose))
    workflow.add_node("verify", observed("verify", verify))
    workflow.add_node("clarify", observed("clarify", clarify, state_only=True))

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
