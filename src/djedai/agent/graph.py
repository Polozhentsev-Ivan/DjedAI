import json
import uuid
from collections.abc import Callable, Collection
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from djedai.agent.dashboard import (
    WidgetExecution,
    build_widget_snapshot,
    dashboard_output_result_ids,
    dashboard_preview_blocks,
    normalize_dashboard_plan,
)
from djedai.agent.data_policy import (
    RESTRICTED_CONTEXT_MESSAGE,
    DataContext,
    DataTransferPolicy,
    policy_for_client,
    sensitive_column_names,
)
from djedai.agent.memory import conversation_context, next_focus_result_ids
from djedai.agent.prompts import load_prompt
from djedai.agent.verification import build_evidence, verify_numeric_text
from djedai.analytics.registry import RunSqlParams, get_tools_for_llm
from djedai.analytics.tools import run_sql
from djedai.config import get_limits
from djedai.contracts.agent import (
    AgentState,
    Block,
    Clarification,
    ComposeResponse,
    DashboardPlanResponse,
    Entities,
    PlanResponse,
    Understanding,
    VerifyResponse,
    VisualizeResponse,
)
from djedai.contracts.chart import ChartSpec
from djedai.contracts.dashboard import DashboardSnapshot
from djedai.contracts.metrics import MetricDefinition, metrics_prompt
from djedai.contracts.quality import QualityIssue
from djedai.contracts.tool_result import ToolError, ToolResult
from djedai.contracts.verification import (
    NumericAssertion,
    VerificationIssue,
    VerificationReport,
)
from djedai.execution import TurnCancelled, TurnTimeout, current_budget

MODEL_ERROR_MARKDOWN = "Не удалось получить ответ модели. Попробуйте повторить запрос."
VERIFICATION_ERROR_MARKDOWN = (
    "Не удалось подтвердить числа в ответе по фактам и данным. "
    "Неподтверждённый ответ не опубликован. Уточните вопрос или повторите запрос."
)
# Dashboard widgets must be independently refreshable. The exploratory analytics
# tools do not produce a stable widget result, and their schemas make the planner
# prompt large enough for a real endpoint to time out.
DASHBOARD_TOOL_NAMES = frozenset({"run_sql", "compute_metric"})
POLICY_NOTE = (
    "\n\nData policy: raw rows from earlier results are not included. "
    "Use only the provided facts, aggregates and metadata; do not invent values. "
    "If they are insufficient to answer, state that the data is unavailable."
)


def _policy(config: RunnableConfig | None) -> DataTransferPolicy:
    """Одна политика хода: приоритет у явного override, затем у клиента."""
    configurable = (config or {}).get("configurable", {})
    override = configurable.get("data_policy")
    if isinstance(override, DataTransferPolicy):
        return override
    return policy_for_client(configurable.get("client"))


def _result_sensitive_columns(
    policy: DataTransferPolicy,
    result: ToolResult,
    *,
    sensitive_columns: Collection[str] = (),
) -> set[str]:
    """Выходные колонки результата, которые нельзя передавать модели.

    ``sensitive_columns`` — исходные чувствительные имена, известные шагу. Для
    зависимого шага сюда заранее добавлены выходные колонки родителя: если он
    скрывал значение под нейтральным алиасом (``password AS value``), имя
    ``value`` должно оставаться чувствительным и для потомка.
    """
    return policy.sensitive_result_columns(result, sensitive_columns=sensitive_columns)


def _result_context(state: AgentState, result: ToolResult) -> DataContext:
    current_ids = {item.result_id for item in state.get("results", [])}
    return DataContext.CURRENT_RESULT if result.result_id in current_ids else DataContext.HISTORY


def _answer_payload(state: AgentState, policy: DataTransferPolicy) -> tuple[list[dict], bool]:
    """Факты и разрешённые данные для compose/verify и признак достаточности.

    Строки прошлых результатов в restricted-режиме заменяются метаданными и
    пометкой ``rows_omitted``. Если после фильтрации не осталось ни фактов, ни
    разрешённых строк, вызывать модель нельзя: ответ был бы выдуманным.
    """
    entries: list[dict] = []
    usable = False
    sensitive_columns = state.get("sensitive_columns", ())
    for result in answer_results(state):
        entry: dict = {"result_id": result.result_id, "tool": result.tool}
        if result.facts:
            sensitive = policy.result_has_sensitive_source(
                result, sensitive_columns=sensitive_columns
            )
            facts = [policy.sanitize_fact(fact, sensitive=sensitive) for fact in result.facts]
            entry["facts"] = facts
            if any(fact.get("text") or fact.get("values") for fact in facts):
                usable = True
        if result.data is not None:
            data = policy.result_data(
                result.data,
                context=_result_context(state, result),
                sensitive_columns=policy.sensitive_result_columns(
                    result, sensitive_columns=sensitive_columns
                ),
            )
            entry["data"] = data
            if data.get("rows"):
                usable = True
        entries.append(entry)
    return entries, usable


def _policy_note(entries: list[dict]) -> str:
    omitted = any(entry.get("data", {}).get("rows_omitted") for entry in entries)
    return POLICY_NOTE if omitted else ""


def load_ctx(state: AgentState, config: RunnableConfig) -> dict:
    """Loads the catalog and context for the specific dataset, and resets per-turn state."""
    policy = _policy(config)
    catalog_loader = config.get("configurable", {}).get("catalog_loader")
    catalog = catalog_loader(state["dataset_id"]) if catalog_loader is not None else None
    catalog_prompt = (
        policy.sanitize_catalog(catalog).to_prompt()
        if catalog is not None
        else f"Catalog mapped for {state.get('dataset_id')}"
    )
    metrics_loader = config.get("configurable", {}).get("metrics_loader")
    if metrics_loader is not None:
        catalog_prompt += "\n\n" + metrics_prompt(metrics_loader(state["dataset_id"]))
    catalog_prompt = policy.sanitize_text(catalog_prompt)
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
        # Normalized column names whose original header is sensitive. Results keep
        # the normalized name, so this is what preserves `dbPasswordText` after
        # normalization to `dbpasswordtext`.
        "sensitive_columns": sensitive_column_names(catalog) if catalog is not None else (),
        "history_results": history_results,
        # Memory belongs to this chat only; the backend loads it by chat_id and owner.
        "focus_result_ids": list(state.get("focus_result_ids", [])),
        "previous_blocks": list(state.get("previous_blocks", [])),
        "results": [],
        "charts": [],
        "answer_blocks": [],
        "retries": 0,
        "compose_retries": 0,
        "is_verified": False,
        "verification": None,
        "dashboard_plan": None,
        "dashboard_widgets": [],
        "dashboard_snapshot": None,
        "dashboard_retries": 0,
    }


def memory_context(state: AgentState, policy: DataTransferPolicy) -> str:
    """Собирает единый контекст диалога для understand/plan."""
    messages = state.get("messages", [])
    return conversation_context(
        summary=state.get("history_summary"),
        recent_messages=messages[:-1] if messages else [],
        focus_result_ids=state.get("focus_result_ids", []),
        results=[*state.get("history_results", []), *state.get("results", [])],
        previous_blocks=state.get("previous_blocks", []),
        policy=policy,
        sensitive_columns=state.get("sensitive_columns", ()),
    )


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
        policy = _policy(config)
        sys_prompt = load_prompt("understand").format(
            catalog=state.get("catalog_prompt"),
            history=memory_context(state, policy),
        )
        response = client.generate_typed_response(
            sys_prompt, last_msg, Understanding, stage="understand"
        )
        if response.status == "success" and response.data:
            return {"understanding": response.data}
        if response.error_type == "POLICY_RESTRICTED":
            return {
                "understanding": Understanding(intent="invalid"),
                "answer_blocks": [Block(type="error", markdown=RESTRICTED_CONTEXT_MESSAGE)],
            }
        return {
            "understanding": Understanding(intent="ambiguous"),
            "answer_blocks": [Block(type="error", markdown=MODEL_ERROR_MARKDOWN)],
        }

    intent = "ambiguous" if "ambiguous" in last_msg.lower() else "data_question"
    return {"understanding": Understanding(intent=intent, entities=Entities())}


def plan(state: AgentState, config: RunnableConfig) -> dict:
    client = config.get("configurable", {}).get("client")
    policy = _policy(config)

    registry = get_tools_for_llm()
    tools_desc = "\n".join(
        f"- {t['name']}: {t['description']} \n  Schema: {json.dumps(t['parameters_schema'])}"
        for t in registry
    )

    # Format the retry context. Tool errors can contain SQL fragments or values,
    # so they are sanitized before they enter the prompt.
    results_str = "\n".join(
        f"Tool {r.tool}: {r.status} - "
        f"{policy.sanitize_text(r.error.message) if r.error else 'Success'}"
        for r in state.get("results", [])
    )

    sys_prompt = load_prompt("plan").format(
        catalog=state.get("catalog_prompt"),
        results=results_str,
        tools_registry=tools_desc,
        history=memory_context(state, policy),
    )

    last_msg = state.get("messages", [])[-1].content if state.get("messages") else ""
    user_prompt = (
        state.get("understanding").resolved_question if state.get("understanding") else None
    )
    user_prompt = user_prompt or last_msg or "Generate execution plan."

    if client:
        response = client.generate_typed_response(
            sys_prompt, user_prompt, PlanResponse, stage="plan"
        )
        if response.status == "success" and response.data:
            return {"plan": response.data.plan, "retries": state.get("retries", 0) + 1}
        message = (
            RESTRICTED_CONTEXT_MESSAGE
            if response.error_type == "POLICY_RESTRICTED"
            else MODEL_ERROR_MARKDOWN
        )
        return {
            "plan": [],
            "retries": state.get("retries", 0) + 1,
            "answer_blocks": [Block(type="error", markdown=message)],
        }

    return {"plan": state.get("plan", []), "retries": state.get("retries", 0) + 1}


def plan_dashboard(state: AgentState, config: RunnableConfig) -> dict:
    """Планирует 3–8 виджетов отдельно от обычного плана на пять шагов."""
    client = config.get("configurable", {}).get("client")
    policy = _policy(config)
    limits = get_limits()

    registry = [tool for tool in get_tools_for_llm() if tool["name"] in DASHBOARD_TOOL_NAMES]
    tools_desc = "\n".join(
        f"- {t['name']}: {t['description']} \n  Schema: {json.dumps(t['parameters_schema'])}"
        for t in registry
    )
    results_str = "\n".join(
        f"Tool {r.tool}: {r.status} - "
        f"{policy.sanitize_text(r.error.message) if r.error else 'Success'}"
        for r in state.get("results", [])
    )
    sys_prompt = load_prompt("dashboard").format(
        catalog=state.get("catalog_prompt"),
        results=results_str,
        tools_registry=tools_desc,
        history=memory_context(state, policy),
        max_tool_calls=limits.dashboard_max_tool_calls,
    )

    last_msg = state.get("messages", [])[-1].content if state.get("messages") else ""
    user_prompt = (
        state.get("understanding").resolved_question if state.get("understanding") else None
    )
    user_prompt = user_prompt or last_msg or "Build a dashboard."
    retries = state.get("dashboard_retries", 0) + 1

    if client:
        response = client.generate_typed_response(
            sys_prompt,
            user_prompt,
            DashboardPlanResponse,
            stage="plan",
            max_output_tokens=limits.dashboard_max_output_tokens,
        )
        if response.status == "success" and response.data:
            try:
                plan = normalize_dashboard_plan(
                    response.data, max_tool_calls=limits.dashboard_max_tool_calls
                )
            except ValueError as exc:
                return {
                    "dashboard_plan": None,
                    "dashboard_retries": retries,
                    "answer_blocks": [
                        Block(
                            type="error",
                            markdown="Не удалось спланировать дашборд: "
                            + policy.sanitize_text(str(exc)),
                        )
                    ],
                }
            return {"dashboard_plan": plan, "dashboard_retries": retries}
        message = (
            RESTRICTED_CONTEXT_MESSAGE
            if response.error_type == "POLICY_RESTRICTED"
            else MODEL_ERROR_MARKDOWN
        )
        return {
            "dashboard_plan": None,
            "dashboard_retries": retries,
            "answer_blocks": [Block(type="error", markdown=message)],
        }

    return {
        "dashboard_plan": None,
        "dashboard_retries": retries,
        "answer_blocks": [Block(type="error", markdown=MODEL_ERROR_MARKDOWN)],
    }


def execute_dashboard(state: AgentState, config: RunnableConfig) -> dict:
    """Выполняет шаги каждого виджета, передавая зависимости как source_result."""
    plan = state.get("dashboard_plan")
    if plan is None:
        return {"dashboard_widgets": []}

    dispatcher = config.get("configurable", {}).get("tool_dispatcher")
    policy = _policy(config)
    sensitive_columns = state.get("sensitive_columns", ())
    limit = get_limits().dashboard_max_tool_calls
    existing_results = state.get("results", [])
    new_results: list[ToolResult] = []
    executions: list[WidgetExecution] = []
    calls = 0

    for widget in plan.widgets:
        execution = WidgetExecution(widget=widget)
        for step in widget.steps:
            if calls >= limit:
                execution.error = ToolError(
                    code="tool_budget_exceeded",
                    message=f"Превышен лимит вызовов инструментов (максимум {limit})",
                    suggestion="Уменьшите число виджетов или шагов в дашборде",
                )
                break

            parent: ToolResult | None = None
            parent_sensitive: set[str] = set()
            if step.source_step_id is not None:
                parent = execution.results.get(step.source_step_id)
                if parent is None or parent.status == "error":
                    execution.error = (
                        parent.error
                        if parent is not None and parent.error is not None
                        else ToolError(
                            code="dependency_failed",
                            message=f"Шаг «{step.source_step_id}» завершился ошибкой",
                        )
                    )
                    break
                # Данные родителя регистрируются как source_result. Его скрытые
                # выходные колонки остаются скрытыми и в производных шагах.
                parent_sensitive = _result_sensitive_columns(
                    policy, parent, sensitive_columns=sensitive_columns
                )
            step_sensitive = (*sensitive_columns, *parent_sensitive)

            calls += 1
            try:
                if budget := current_budget():
                    budget.check()
                if dispatcher:
                    if parent is not None:
                        res = dispatcher(step.tool, dict(step.args), source_result=parent)
                    else:
                        res = dispatcher(step.tool, dict(step.args))
                elif step.tool == "run_sql" and parent is None:
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

                updates: dict = {
                    "tool": step.tool,
                    "params": policy.sanitize_value(dict(step.args)),
                    "sensitive_columns": sorted(
                        _result_sensitive_columns(
                            policy,
                            res,
                            sensitive_columns=step_sensitive,
                        )
                    ),
                }
                if parent is not None:
                    updates["source_result_id"] = parent.result_id
                if res.error is not None:
                    updates["error"] = policy.sanitize_error(res.error)
                if res.facts:
                    sensitive = policy.result_has_sensitive_source(
                        res, sensitive_columns=step_sensitive
                    )
                    updates["facts"] = [
                        policy.sanitize_fact_model(fact, sensitive=sensitive) for fact in res.facts
                    ]
                if res.sql is not None:
                    updates["sql"] = policy.sanitize_text(res.sql)
                res = res.model_copy(update=updates)
            except (TurnTimeout, TurnCancelled):
                raise
            except Exception as exc:
                res = ToolResult(
                    result_id=f"r_{uuid.uuid4().hex[:6]}",
                    status="error",
                    tool=step.tool,
                    params=policy.sanitize_value(dict(step.args)),
                    source_result_id=parent.result_id if parent is not None else None,
                    error=ToolError(
                        code="execution_failed",
                        message=policy.sanitize_text(str(exc)) or "Ошибка выполнения инструмента.",
                    ),
                )
                res = res.model_copy(
                    update={
                        "sensitive_columns": sorted(
                            _result_sensitive_columns(policy, res, sensitive_columns=step_sensitive)
                        )
                    }
                )

            new_results.append(res)
            execution.results[step.step_id] = res
            if res.status == "error":
                execution.error = res.error
                break

        executions.append(execution)

    return {
        "results": existing_results + new_results,
        "dashboard_widgets": executions,
    }


def save_dashboard(state: AgentState, config: RunnableConfig) -> dict:
    """Сохраняет дашборд через backend и собирает ссылку с превью виджетов."""
    plan = state.get("dashboard_plan")
    executions = state.get("dashboard_widgets", [])
    if plan is None or not executions:
        return {"answer_blocks": [Block(type="error", markdown="Не удалось сформировать дашборд.")]}

    saver = config.get("configurable", {}).get("dashboard_saver")
    if saver is None:
        return {
            "answer_blocks": [
                Block(type="error", markdown="Сохранение дашбордов сейчас недоступно.")
            ]
        }

    widgets = [
        build_widget_snapshot(execution, index) for index, execution in enumerate(executions)
    ]
    try:
        snapshot = saver(plan.title, plan.description, widgets)
        if isinstance(snapshot, dict):
            snapshot = DashboardSnapshot.model_validate(snapshot)
    except (TurnTimeout, TurnCancelled):
        raise
    except Exception as exc:
        policy = _policy(config)
        return {
            "answer_blocks": [
                Block(
                    type="error",
                    markdown="Не удалось сохранить дашборд. "
                    + (policy.sanitize_text(str(exc)) or "Повторите запрос позднее."),
                )
            ]
        }

    return {
        "dashboard_snapshot": snapshot,
        "answer_blocks": dashboard_preview_blocks(snapshot),
        "charts": [
            widget.chart for widget in snapshot.widgets if isinstance(widget.chart, ChartSpec)
        ],
        "focus_result_ids": dashboard_output_result_ids(snapshot),
    }


def execute(state: AgentState, config: RunnableConfig) -> dict:
    plan_steps = state.get("plan", [])
    existing_results = state.get("results", [])
    policy = _policy(config)
    sensitive_columns = state.get("sensitive_columns", ())
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

            # Ensure metadata is stamped on the result (ToolResult is frozen).
            # Errors and facts are stored sanitized because they can reach both
            # the planner and the final answer.
            updates: dict = {
                "tool": step.tool,
                "params": policy.sanitize_value(step.args),
                "sensitive_columns": sorted(
                    _result_sensitive_columns(policy, res, sensitive_columns=sensitive_columns)
                ),
            }
            if res.error is not None:
                updates["error"] = policy.sanitize_error(res.error)
            if res.facts:
                sensitive = policy.result_has_sensitive_source(
                    res, sensitive_columns=sensitive_columns
                )
                updates["facts"] = [
                    policy.sanitize_fact_model(fact, sensitive=sensitive) for fact in res.facts
                ]
            if res.sql is not None:
                updates["sql"] = policy.sanitize_text(res.sql)
            res = res.model_copy(update=updates)
            new_results.append(res)

        except (TurnTimeout, TurnCancelled):
            raise
        except Exception as e:
            error_res = ToolResult(
                result_id=f"r_{uuid.uuid4().hex[:6]}",
                status="error",
                tool=step.tool,
                params=policy.sanitize_value(step.args),
                error=ToolError(
                    code="execution_failed",
                    message=policy.sanitize_text(str(e)) or "Ошибка выполнения инструмента.",
                ),
            )
            new_results.append(
                error_res.model_copy(
                    update={
                        "sensitive_columns": sorted(
                            _result_sensitive_columns(
                                policy, error_res, sensitive_columns=sensitive_columns
                            )
                        )
                    }
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
    """Все успешные результаты текущего хода, включая сохранённые с предыдущих попыток (ретраев)."""
    return [r for r in state.get("results", []) if r.status == "ok"]


def chart_source_results(state: AgentState) -> list[ToolResult]:
    """Возвращает уже посчитанные результаты, по которым можно построить график."""
    current_ok = active_results(state)
    if current_ok:
        return current_ok

    understanding = state.get("understanding")
    references = understanding.references if understanding else []
    history_results = state.get("history_results", [])
    saved = {r.result_id: r for r in [*history_results, *state.get("results", [])]}

    if references:
        referenced = [
            saved[ref] for ref in references if ref in saved and saved[ref].status == "ok"
        ]
        if referenced:
            return referenced

    # Follow-up without an explicit reference: the conversation focus, not the
    # arbitrary last result, is what the user is asking to show differently.
    focus = state.get("focus_result_ids", [])
    focused = [saved[result_id] for result_id in focus if result_id in saved]
    focused = [result for result in focused if result.status == "ok"]
    if focused:
        return focused

    return [history_results[-1]] if history_results and history_results[-1].status == "ok" else []


def chart_result_ids(charts: list[ChartSpec]) -> list[str]:
    """Возвращает result_id, на которые ссылаются построенные графики."""
    return list(dict.fromkeys(chart.result_id for chart in charts if chart.result_id))


def chart_block_result_ids(blocks: list[Block]) -> list[str]:
    """Возвращает результат_id, на которые ссылаются блоки-графики ответа."""
    result_ids = []
    for block in blocks:
        if block.type != "chart" or not block.chart:
            continue
        result_id = block.chart.get("result_id")
        if result_id:
            result_ids.append(result_id)
    return list(dict.fromkeys(result_ids))


def answer_results(state: AgentState) -> list[ToolResult]:
    """Результаты, по которым объясняется и проверяется ответ текущего хода.

    Если ход только изменил представление, берём сохранённые результаты,
    на которые сослался пользователь или которые остаются в фокусе разговора.
    Без этого верификатор и compose теряют факты предыдущего ответа.
    """
    current = active_results(state)
    if current:
        return current

    saved = {
        result.result_id: result
        for result in state.get("history_results", []) + state.get("results", [])
    }
    referenced = [
        *chart_result_ids(state.get("charts", [])),
        *chart_block_result_ids(state.get("answer_blocks", [])),
    ]
    chart_results = [
        saved[result_id]
        for result_id in dict.fromkeys(referenced)
        if result_id in saved and saved[result_id].status == "ok"
    ]
    if chart_results:
        return chart_results

    # Смена представления без нового плана: объясняем сохранённые результаты
    # вместо того, чтобы терять факты текущего (пустого) шага.
    if state.get("plan"):
        return []
    understanding = state.get("understanding")
    references = understanding.references if understanding else []
    if references:
        return [saved[ref] for ref in references if ref in saved and saved[ref].status == "ok"]

    # Text continuation without a new plan or explicit references: keep the
    # conversation focus. Falling back to history_results[-1] would explain
    # whichever result happened to be calculated last, not the selected one.
    focus = state.get("focus_result_ids", [])
    focused = [
        saved[result_id]
        for result_id in focus
        if result_id in saved and saved[result_id].status == "ok"
    ]
    if focused:
        return focused

    history_results = state.get("history_results", [])
    return [history_results[-1]] if history_results and history_results[-1].status == "ok" else []


def updated_focus_result_ids(state: AgentState) -> list[str]:
    """Обновляет «о чём сейчас разговор» без привязки к чужому чату."""
    current_results = state.get("results", [])
    plan_len = len(state.get("plan", []))
    plan_results = current_results[-plan_len:] if plan_len > 0 else []
    understanding = state.get("understanding")
    references = understanding.references if understanding else []
    known = {result.result_id for result in [*state.get("history_results", []), *current_results]}
    return next_focus_result_ids(
        state.get("focus_result_ids", []),
        plan_results=plan_results,
        references=references,
        available_ids=known,
    )


def describe_result_for_chart(
    result: ToolResult,
    policy: DataTransferPolicy,
    context: DataContext,
    *,
    sensitive_columns: Collection[str] = (),
) -> dict:
    """Краткое описание результата для модели по правилам политики."""
    return policy.chart_result(result, context=context, sensitive_columns=sensitive_columns)


NUMERIC_TYPES = {
    "INT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "TINYINT",
    "SMALLINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "DECIMAL",
    "NUMERIC",
    "MONEY",
}


def _column_info(column: object) -> tuple[str | None, str | None]:
    """Возвращает имя и объявленный тип колонки результата."""
    if isinstance(column, str):
        return column, None
    if isinstance(column, dict):
        name = column.get("name")
        return (name, column.get("type")) if name else (None, None)
    name = getattr(column, "name", None)
    return (name, getattr(column, "type", None)) if name else (None, None)


def _values_are_numeric(rows: list[list[object]], index: int) -> bool | None:
    """Определяет числовой тип по значениям, если метаданные колонки не заданы.

    Возвращает None, когда значений нет и тип определить нельзя.
    """
    decided: bool | None = None
    for row in rows:
        if index >= len(row) or row[index] is None:
            continue
        value = row[index]
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return False
        decided = True
    return decided


def _is_numeric_type(column_type: str | None) -> bool:
    """Совместим ли объявленный тип колонки с числовой осью."""
    if not column_type:
        return True  # If unknown, allow to avoid false positives
    return any(numeric_type in str(column_type).upper() for numeric_type in NUMERIC_TYPES)


def _column_is_numeric(rows: list[list[object]], index: int, column_type: str | None) -> bool:
    """Числовая ли колонка: по объявленному типу, иначе по значениям строк."""
    if column_type:
        return _is_numeric_type(column_type)
    # Инструменты могут возвращать только имена колонок, поэтому тип
    # восстанавливаем по значениям.
    return _values_are_numeric(rows, index) is not False


def sanitize_charts(charts: list[ChartSpec], results: list[ToolResult]) -> list[ChartSpec]:
    """Оставляет только графики, совместимые с сохранённым результатом.

    Проверяются и ссылка на результат, и все поля спецификации (X, Y, цвет,
    сортировка), и типы: значения Y всегда числовые, а ось X числовая там, где
    её так читает рендерер (scatter и `x.kind = "number"`).
    """
    result_map = {r.result_id: r for r in results}
    sanitized = []
    seen_ids: set[str] = set()

    for chart in charts:
        res = result_map.get(chart.result_id)
        if not res or not res.data:
            continue

        columns: dict[str, tuple[int, str | None]] = {}
        for index, column in enumerate(res.data.columns):
            name, column_type = _column_info(column)
            if name is not None:
                columns[name] = (index, column_type)

        is_valid = True

        # Проверка оси X (наличие и совместимость типа)
        if chart.x:
            column = columns.get(chart.x.field)
            if column is None:
                is_valid = False
            else:
                # Строковые категории нельзя откладывать по числовой оси:
                # рендерер отклоняет такие спецификации (scatter и x.kind=number).
                x_requires_numbers = chart.type == "scatter" or chart.x.kind == "number"
                if x_requires_numbers and not _column_is_numeric(res.data.rows, *column):
                    is_valid = False

        # Проверка осей Y (наличие и числовой тип)
        for y_measure in chart.y:
            column = columns.get(y_measure.field)
            if column is None:
                is_valid = False
                continue
            if not _column_is_numeric(res.data.rows, *column):
                is_valid = False

        # Проверка color и sort
        if chart.color and chart.color.field not in columns:
            is_valid = False
        if chart.sort and chart.sort.field not in columns:
            is_valid = False

        if not is_valid:
            continue

        chart_id = chart.chart_id
        if not chart_id or chart_id == "string" or chart_id in seen_ids:
            chart_id = f"ch_{uuid.uuid4().hex[:8]}"
        seen_ids.add(chart_id)
        sanitized.append(chart.model_copy(update={"chart_id": chart_id}))

    return sanitized


def visualize(state: AgentState, config: RunnableConfig) -> dict:
    focus = updated_focus_result_ids(state)
    if not wants_visualization(state):
        return {"charts": [], "focus_result_ids": focus}

    valid_results = chart_source_results(state)
    if not valid_results:
        return {"charts": [], "focus_result_ids": focus}

    policy = _policy(config)
    results_summary = [
        describe_result_for_chart(
            result,
            policy,
            _result_context(state, result),
            sensitive_columns=state.get("sensitive_columns", ()),
        )
        for result in valid_results
    ]

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
        response = client.generate_typed_response(
            sys_prompt, user_prompt, VisualizeResponse, stage="visualize"
        )
        if response.status == "success" and response.data:
            return {
                "charts": sanitize_charts(response.data.charts, valid_results),
                "focus_result_ids": focus,
            }
        if response.error_type == "POLICY_RESTRICTED":
            return {
                "charts": [],
                "focus_result_ids": focus,
                "answer_blocks": [Block(type="error", markdown=RESTRICTED_CONTEXT_MESSAGE)],
            }

    return {"charts": [], "focus_result_ids": focus}


def compose(state: AgentState, config: RunnableConfig) -> dict:
    client = config.get("configurable", {}).get("client")
    focus = updated_focus_result_ids(state)

    existing_blocks = state.get("answer_blocks", [])
    if any(b.type == "error" for b in existing_blocks):
        return {"answer_blocks": existing_blocks, "focus_result_ids": focus}

    policy = _policy(config)
    results = answer_results(state)
    facts_and_data, usable = _answer_payload(state, policy)
    if results and not usable:
        return {
            "answer_blocks": [Block(type="error", markdown=RESTRICTED_CONTEXT_MESSAGE)],
            "compose_retries": state.get("compose_retries", 0) + 1,
            "focus_result_ids": focus,
        }

    sys_prompt = load_prompt("compose").format(
        facts=json.dumps(facts_and_data, ensure_ascii=False, indent=2, default=str)
        + _policy_note(facts_and_data)
    )

    last_msg = state.get("messages", [])[-1].content if state.get("messages") else ""
    user_prompt = (
        state.get("understanding").resolved_question if state.get("understanding") else None
    )
    user_prompt = user_prompt or last_msg or "Explain the results."

    if state.get("compose_retries", 0) > 0:
        user_prompt += (
            "\n\nWARNING: Your previous response contained unverified or hallucinated numbers. "
            "Please carefully check formatting, rounding, and units, "
            "and ensure ALL numbers strictly match the facts."
        )
        previous = state.get("verification")
        if previous is not None and previous.issues:
            reasons = "\n".join(f"- {issue.message}" for issue in previous.issues)
            user_prompt += (
                "\nThe deterministic verifier rejected these points; do not repeat them:\n"
                + reasons
            )

    if last_msg and user_prompt != last_msg:
        user_prompt += f"\nOriginal question and response requirements: {last_msg}"

    if client:
        response = client.generate_typed_response(
            sys_prompt, user_prompt, ComposeResponse, stage="compose"
        )
        if response.status == "success" and response.data:
            blocks = response.data.answer_blocks
        else:
            message = (
                RESTRICTED_CONTEXT_MESSAGE
                if response.error_type == "POLICY_RESTRICTED"
                else MODEL_ERROR_MARKDOWN
            )
            blocks = [Block(type="error", markdown=message)]
    else:
        blocks = [Block(type="text", markdown=f"Based on facts: {facts_and_data}")]

    charts = state.get("charts", [])
    if charts:
        for c in charts:
            blocks.append(Block(type="chart", chart=c.model_dump(mode="json")))

    return {
        "answer_blocks": blocks,
        "compose_retries": state.get("compose_retries", 0) + 1,
        "focus_result_ids": focus,
    }


def _verification_issue(code: str) -> VerificationIssue:
    messages = {
        "llm_reported": "Модель отметила утверждение как неподтверждённое.",
        "llm_unavailable": "Проверка ответа моделью недоступна.",
        "invalid_correction": "Исправленный моделью текст не прошёл проверку чисел.",
    }
    return VerificationIssue(code=code, message=messages[code])  # type: ignore[arg-type]


def _previous_rejected(state: AgentState) -> list[NumericAssertion]:
    report = state.get("verification")
    return list(report.rejected_assertions) if report is not None else []


def _unique_assertions(items: Collection[NumericAssertion]) -> list[NumericAssertion]:
    unique: dict[tuple[object, ...], NumericAssertion] = {}
    for item in items:
        key = (item.kind, item.raw_text, item.reason_code, item.text)
        unique.setdefault(key, item)
    return list(unique.values())


def _unique_issues(items: Collection[VerificationIssue]) -> list[VerificationIssue]:
    unique: dict[tuple[str, str], VerificationIssue] = {}
    for item in items:
        unique.setdefault((item.code, item.message), item)
    return list(unique.values())


def _finished_verification(
    report: VerificationReport,
    *,
    status: str,
    rejected: Collection[NumericAssertion] = (),
    issues: Collection[VerificationIssue] = (),
) -> VerificationReport:
    return report.model_copy(
        update={
            "status": status,
            "rejected_assertions": _unique_assertions(rejected),
            "issues": _unique_issues(issues),
        }
    )


def _publish_corrected(blocks: list[Block], corrected: str) -> list[Block]:
    new_blocks = [block for block in blocks if block.type not in ("text", "chart")]
    new_blocks.append(Block(type="text", markdown=corrected))
    return new_blocks


def _verification_failure(
    state: AgentState,
    blocks: list[Block],
    report: VerificationReport,
    *,
    extra_rejected: Collection[NumericAssertion] = (),
    extra_issues: Collection[VerificationIssue] = (),
) -> dict:
    rejected = [
        *_previous_rejected(state),
        *report.rejected_assertions,
        *extra_rejected,
    ]
    issues = [
        *(state.get("verification").issues if state.get("verification") else ()),
        *report.issues,
        *extra_issues,
    ]
    failed = _finished_verification(report, status="failed", rejected=rejected, issues=issues)
    new_blocks = [block for block in blocks if block.type not in ("text", "chart")]
    new_blocks.append(Block(type="error", markdown=VERIFICATION_ERROR_MARKDOWN))
    return {"is_verified": True, "answer_blocks": new_blocks, "verification": failed}


def verify(state: AgentState, config: RunnableConfig) -> dict:
    """Проверяет числа детерминированно, затем независимо уточняет у модели.

    Модель не может подтвердить число, которое не прошло детерминированную
    проверку. Исправленный ею текст проверяется тем же алгоритмом; если он
    снова не проходит, публикуется явная ошибка, а не «проверенный» ответ.
    """
    client = config.get("configurable", {}).get("client")
    blocks = state.get("answer_blocks", [])
    results = answer_results(state)
    result_ids = [result.result_id for result in results]

    draft_parts = []
    for b in blocks:
        if b.type == "text" and b.markdown:
            draft_parts.append(b.markdown)
        elif b.type == "chart" and b.chart:
            chart_dict = (
                b.chart
                if isinstance(b.chart, dict)
                else (b.chart.model_dump() if hasattr(b.chart, "model_dump") else {})
            )
            if title := chart_dict.get("title"):
                draft_parts.append(title)
            if sub := chart_dict.get("subtitle"):
                draft_parts.append(sub)

    if not draft_parts:
        report = VerificationReport(status="not_applicable", result_ids=result_ids)
        return {"is_verified": True, "verification": report}

    draft = "\n\n".join(draft_parts)

    policy = _policy(config)
    facts_and_data, usable = _answer_payload(state, policy)

    if not facts_and_data:
        new_blocks = [b for b in blocks if b.type != "text"]
        new_blocks.append(Block(type="text", markdown="Нет данных для формирования ответа."))
        report = VerificationReport(status="not_applicable", result_ids=result_ids)
        return {"is_verified": True, "answer_blocks": new_blocks, "verification": report}

    if results and not usable:
        new_blocks = [b for b in blocks if b.type not in ("text", "chart")]
        new_blocks.append(Block(type="error", markdown=RESTRICTED_CONTEXT_MESSAGE))
        report = VerificationReport(status="not_applicable", result_ids=result_ids)
        return {"is_verified": True, "answer_blocks": new_blocks, "verification": report}

    evidence = build_evidence(results, facts_and_data)
    deterministic = verify_numeric_text(draft, evidence, result_ids=result_ids)
    deterministic_ok = deterministic.status != "failed"

    if not client:
        if deterministic_ok:
            status = "verified" if deterministic.status == "verified" else "not_applicable"
            return {
                "is_verified": True,
                "verification": _finished_verification(deterministic, status=status),
            }
        return _verification_failure(state, blocks, deterministic)

    sys_prompt = load_prompt("verify").format(
        facts=json.dumps(facts_and_data, ensure_ascii=False, indent=2, default=str)
        + _policy_note(facts_and_data),
        draft=draft,
    )
    response = client.generate_typed_response(
        sys_prompt,
        "Verify the numbers and formats.",
        VerifyResponse,
        stage="verify",
    )

    if response.status != "success" or not response.data:
        message = (
            RESTRICTED_CONTEXT_MESSAGE
            if response.error_type == "POLICY_RESTRICTED"
            else (
                "Ошибка проверки ответа: недоступен API верификации. Неподтвержденный ответ удален."
            )
        )
        new_blocks = [b for b in blocks if b.type not in ("text", "chart")]
        new_blocks.append(Block(type="error", markdown=message))
        failed = _finished_verification(
            deterministic,
            status="failed",
            rejected=_previous_rejected(state),
            issues=[*deterministic.issues, _verification_issue("llm_unavailable")],
        )
        return {"is_verified": True, "answer_blocks": new_blocks, "verification": failed}

    v_data = response.data
    if v_data.is_valid and deterministic_ok:
        status = "verified" if deterministic.status == "verified" else "not_applicable"
        return {
            "is_verified": True,
            "verification": _finished_verification(deterministic, status=status),
        }

    llm_rejected = not v_data.is_valid
    at_last_attempt = state.get("compose_retries", 0) >= 2
    if not at_last_attempt:
        report = _finished_verification(
            deterministic,
            status="failed",
            rejected=_previous_rejected(state),
            issues=[
                *deterministic.issues,
                *([_verification_issue("llm_reported")] if llm_rejected else []),
            ],
        )
        return {"is_verified": False, "verification": report}

    corrected = (v_data.corrected_markdown or "").strip()
    if corrected and corrected != draft:
        corrected_report = verify_numeric_text(corrected, evidence, result_ids=result_ids)
        if corrected_report.status != "failed":
            new_blocks = _publish_corrected(blocks, corrected)
            issues = [
                *([_verification_issue("llm_reported")] if llm_rejected else []),
                *deterministic.issues,
            ]
            return {
                "is_verified": True,
                "answer_blocks": new_blocks,
                "verification": _finished_verification(
                    corrected_report,
                    status="corrected",
                    rejected=[
                        *_previous_rejected(state),
                        *deterministic.rejected_assertions,
                    ],
                    issues=issues,
                ),
            }
        return _verification_failure(
            state,
            blocks,
            corrected_report,
            extra_rejected=[*deterministic.rejected_assertions],
            extra_issues=[*deterministic.issues, _verification_issue("invalid_correction")],
        )

    return _verification_failure(
        state,
        blocks,
        deterministic,
        extra_issues=[*([_verification_issue("llm_reported")] if llm_rejected else [])],
    )


def clarify(state: AgentState, config: RunnableConfig | None = None) -> dict:
    focus = list(state.get("focus_result_ids", []))
    existing = state.get("answer_blocks", [])
    # Do not suppress API errors if they are already present in the response blocks
    if any(b.type == "error" for b in existing):
        return {"answer_blocks": existing, "focus_result_ids": focus}

    policy = _policy(config)
    understanding = state.get("understanding")
    intent = understanding.intent if understanding else "invalid"

    blocks = []

    if intent in ["ambiguous", "invalid"]:
        if understanding and understanding.clarification:
            # Options travel separately so the chat can render them as buttons.
            blocks.append(
                Block(
                    type="clarification",
                    markdown=policy.sanitize_text(understanding.clarification.question),
                    options=[
                        policy.sanitize_text(option)
                        for option in understanding.clarification.options
                    ],
                    metric_candidates=_checked_candidates(
                        understanding.clarification, state, config
                    ),
                )
            )
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

    elif (error := _metric_decision(state)) is not None:
        # Расчёт метрики упёрся в решение пользователя: определение, валюту, период, JOIN.
        text = policy.sanitize_text(error.message)
        if error.suggestion:
            text = f"{text}\n\n{policy.sanitize_text(error.suggestion)}"
        blocks.append(Block(type="clarification", markdown=text))

    elif blockers := _quality_blockers(state):
        # Неоднозначная дата или смешанные валюты в расчёте: решает пользователь (B2-10).
        issue = blockers[0]
        blocks.append(
            Block(
                type="clarification",
                markdown=policy.sanitize_text(issue.message),
                options=[policy.sanitize_text(option) for option in issue.options],
            )
        )

    else:
        blocks.append(Block(type="clarification", markdown="Пожалуйста, уточните ваш запрос."))

    return {
        "plan": [],
        "answer_blocks": blocks,
        "focus_result_ids": focus,
    }


# Ошибки compute_metric, которые решает только пользователь (B2-9): повторный план их не
# исправит. Те же коды от run_sql сначала дают перепланирование на compute_metric.
METRIC_DECISIONS = {
    "metric_undefined",
    "metric_definition_stale",
    "metric_mixed_currency",
    "metric_period_unsupported",
    "metric_grain_violation",
    "metric_rows_lost",
}


def _recent_results(state: AgentState) -> list[ToolResult]:
    plan_len = len(state.get("plan", []))
    return state.get("results", [])[-plan_len:] if plan_len > 0 else []


def _metric_decision(state: AgentState) -> ToolError | None:
    for result in reversed(state.get("results", [])):
        if result.status == "error" and result.error and result.error.code in METRIC_DECISIONS:
            return result.error
    return None


def _answered(state: AgentState) -> str:
    """Текущий вопрос: выбор в уточнении действует только на вопрос, в котором сделан.

    Чат сворачивает цепочку «вопрос — кнопки уточнений» в последнее сообщение, поэтому
    старая кнопка из истории не снимает ограничение нового расчёта.
    """
    messages = state.get("messages", [])
    return messages[-1].content if messages and messages[-1].role == "user" else ""


def _quality_blockers(state: AgentState) -> list[QualityIssue]:
    """Blocking-ограничения качества в последних результатах, на которые не ответили.

    Варианты уточнения начинаются с «таблица.колонка:»; ответ с этой ссылкой в сообщениях
    пользователя снимает блокировку, и расчёт идёт дальше с предупреждением.
    """
    answered = _answered(state)
    found = []
    for result in _recent_results(state):
        if result.status == "error" or result.quality is None:
            continue
        for column in result.quality.columns:
            if f"{column.table}.{column.column}:" in answered:
                continue
            found += [i for i in column.issues if i.severity == "blocking"]
    return found


def _checked_candidates(
    clarification: Clarification, state: AgentState, config: RunnableConfig | None
) -> list[MetricDefinition | None]:
    """Кандидаты определений, совместимые с каталогом; несовместимый вариант — ``None``.

    Кандидат от модели — только предложение: подтверждает его выбор пользователя.
    Без каталога проверить нечего, и вариант не подтверждает определение.
    """
    configurable = (config or {}).get("configurable", {})
    catalog_loader = configurable.get("catalog_loader")
    candidates = clarification.metric_candidates[: len(clarification.options)]
    if catalog_loader is None or not candidates:
        return []
    catalog = catalog_loader(state["dataset_id"])
    metrics_loader = configurable.get("metrics_loader")
    metrics = metrics_loader(state["dataset_id"]) if metrics_loader is not None else {}
    checked: list[MetricDefinition | None] = []
    for candidate in candidates:
        proposal = MetricDefinition.model_validate(candidate.model_dump())  # status=proposed
        try:
            proposal.validate_against(catalog, metrics)
        except ValueError:
            checked.append(None)
        else:
            checked.append(proposal)
    return checked


def cite_metrics(state: AgentState) -> dict:
    """Добавляет к проверенному ответу ограничения данных и версии определений метрик.

    Оба блока собираются из результатов инструментов, а не из текста модели: если модель
    промолчала о потерях типизации, пользователь всё равно их увидит (B2-10).
    """
    blocks = state.get("answer_blocks", [])
    if any(b.type == "error" for b in blocks):
        return {}
    added = []
    if limits := _quality_lines(state):
        text = "**Ограничения данных.**\n" + "\n".join(f"- {line}" for line in limits)
        added.append(Block(type="text", markdown=text))
    lines = []
    for result in answer_results(state):
        if result.status == "error" or result.metric is None:
            continue
        lines += [f.text for f in result.facts if f.kind == "metric_definition"]
    lines += _source_lines(state)
    if lines:
        text = "**Как посчитано.** " + " ".join(f"{line}." for line in dict.fromkeys(lines))
        added.append(Block(type="text", markdown=text))
    if not added:
        return {}
    return {"answer_blocks": [*blocks, *added]}


def _saved_result_ids(state: AgentState) -> set[str]:
    """Результаты прошлых ходов: их числа берутся из хранилища, а не пересчитываются."""
    current = {result.result_id for result in state.get("results", [])}
    return {r.result_id for r in state.get("history_results", []) if r.result_id not in current}


def _moment(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")


def _read_line(result: ToolResult, saved: bool) -> str:
    """Откуда и когда взяты данные результата (B1-11)."""
    read = result.read
    if read.read_mode == "unknown":
        return "версия каталога и время чтения не сохранены"
    revision = (
        f"каталог r{read.catalog_revision}" if read.revision_known else "версия каталога неизвестна"
    )
    if read.read_mode == "live":
        line = f"живое чтение источника {_moment(read.finished_at)}, {revision}"
    else:
        profiled = read.freshness.profiled_at
        line = f"снимок датасета, {revision}" + (
            f" (профиль от {_moment(profiled)})" if profiled else ""
        )
    if saved:
        line = f"сохранённый расчёт от {_moment(read.finished_at)} без пересчёта: {line}"
    return line


def _source_lines(state: AgentState) -> list[str]:
    saved = _saved_result_ids(state)
    used = [r for r in answer_results(state) if r.status != "error" and r.read is not None]
    return list(dict.fromkeys(f"Данные: {_read_line(r, r.result_id in saved)}" for r in used))


def _read_limits(state: AgentState) -> list[str]:
    """Свежесть источника и согласованность чтений ответа как ограничения данных."""
    saved = _saved_result_ids(state)
    reads = [
        (r.result_id in saved, r.read)
        for r in answer_results(state)
        if r.status != "error" and r.read is not None
    ]
    lines = []
    for _, read in reads:
        if read.freshness.state in ("aged", "drifted", "unknown") and read.freshness.reason:
            lines.append(read.freshness.reason + ".")
    live = [(old, read) for old, read in reads if read.read_mode == "live"]
    if any(old for old, _ in live) and any(not old for old, _ in live):
        lines.append(
            "Ответ сочетает сохранённый ранее расчёт и новое чтение источника: данные могли "
            "измениться между ними."
        )
    snapshots = {read.snapshot_id for old, read in live if not old}
    if len(snapshots) > 1 or (
        len(live) > 1 and any(read.consistency == "independent" for _, read in live)
    ):
        lines.append(
            "Расчёты ответа прочитали источник в разных состояниях: числа между ними могут "
            "не сходиться."
        )
    return list(dict.fromkeys(lines))


def _quality_lines(state: AgentState) -> list[str]:
    answered = _answered(state)
    lines = []
    for result in answer_results(state):
        report = result.quality
        if result.status == "error" or report is None:
            continue
        if report.lineage == "partial":
            lines.append(
                "Не все колонки расчёта удалось связать с данными: ограничения ниже могут "
                "быть неполными."
            )
        for column in report.columns:
            chosen = f"{column.table}.{column.column}:" in answered
            for issue in column.issues:
                if issue.severity == "info":
                    continue
                note = (
                    " Выбор уточнён пользователем."
                    if issue.severity == "blocking" and chosen
                    else ""
                )
                lines.append(issue.message + note)
    lines += _read_limits(state)
    return list(dict.fromkeys(lines))


def route_after_understand(state: AgentState) -> str:
    """Conditional edge: уточнения — в clarify, дашборд — в свою ветку, иначе plan."""
    intent = state["understanding"].intent
    if intent in ["ambiguous", "invalid", "off_topic"]:
        return "clarify"
    if intent == "dashboard":
        return "plan_dashboard"
    return "plan"


def route_after_plan_dashboard(state: AgentState) -> str:
    if state.get("dashboard_plan") is None:
        return "end"
    return "execute_dashboard"


def route_after_execute_dashboard(state: AgentState) -> str:
    """Один повтор полного плана, если хотя бы один виджет не посчитался.

    План должен содержать завершённые расчёты, поэтому частичный сбой тоже
    перепланируется: модель видит ошибки и может исправить шаг. Если после
    повтора ошибки остались, сохраняем частичный дашборд с явными ошибками
    виджетов — успешные соседи остаются доступны.
    """
    executions = state.get("dashboard_widgets", [])
    if any(not execution.is_successful for execution in executions):
        if state.get("dashboard_retries", 0) < 2:
            return "plan_dashboard"
    return "save_dashboard"


def route_after_execute(state: AgentState) -> str:
    recent_results = _recent_results(state)
    has_errors = any(r.status == "error" for r in recent_results)
    metric_errors = [
        r
        for r in recent_results
        if r.status == "error" and r.error and r.error.code in METRIC_DECISIONS
    ]
    if any(r.tool == "compute_metric" for r in metric_errors):
        return "clarify"
    if has_errors and state.get("retries", 0) <= 2:
        return "plan"
    if metric_errors or _quality_blockers(state):
        return "clarify"
    return "visualize"


def route_after_verify(state: AgentState) -> str:
    if state.get("is_verified", True):
        return "cite_metrics"
    if state.get("compose_retries", 0) < 2:
        return "compose"
    return "cite_metrics"


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
    workflow.add_node("plan_dashboard", observed("plan_dashboard", plan_dashboard))
    workflow.add_node("execute_dashboard", observed("execute_dashboard", execute_dashboard))
    workflow.add_node("save_dashboard", observed("save_dashboard", save_dashboard))
    workflow.add_node("visualize", observed("visualize", visualize, state_only=False))
    workflow.add_node("compose", observed("compose", compose))
    workflow.add_node("verify", observed("verify", verify))
    workflow.add_node("clarify", observed("clarify", clarify))
    # Без модели и данных: не отдельный этап хода для статусов и диагностики.
    workflow.add_node("cite_metrics", cite_metrics)

    workflow.add_edge(START, "load_ctx")
    workflow.add_edge("load_ctx", "understand")

    workflow.add_conditional_edges(
        "understand",
        route_after_understand,
        {"plan": "plan", "plan_dashboard": "plan_dashboard", "clarify": "clarify"},
    )

    workflow.add_edge("plan", "execute")

    # Dynamic loop for error correction
    workflow.add_conditional_edges(
        "execute",
        route_after_execute,
        {"plan": "plan", "visualize": "visualize", "clarify": "clarify"},
    )

    # Отдельная ветка дашборда: план виджетов → вычисления → сохранение.
    workflow.add_conditional_edges(
        "plan_dashboard",
        route_after_plan_dashboard,
        {"execute_dashboard": "execute_dashboard", "end": END},
    )
    workflow.add_conditional_edges(
        "execute_dashboard",
        route_after_execute_dashboard,
        {"plan_dashboard": "plan_dashboard", "save_dashboard": "save_dashboard"},
    )
    workflow.add_edge("save_dashboard", END)

    workflow.add_edge("visualize", "compose")
    workflow.add_edge("compose", "verify")

    # Conditional edge to handle regeneration
    workflow.add_conditional_edges(
        "verify",
        route_after_verify,
        {"compose": "compose", "cite_metrics": "cite_metrics"},
    )
    workflow.add_edge("cite_metrics", END)

    workflow.add_edge("clarify", END)

    return workflow.compile(checkpointer=MemorySaver())
