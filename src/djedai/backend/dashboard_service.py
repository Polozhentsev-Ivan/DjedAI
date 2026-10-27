"""Сервис дашбордов и перевычисления виджетов (§7.2, B5-6)."""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import duckdb
import pandas as pd
from fastapi import HTTPException
from sqlalchemy.orm import Session

from djedai.analytics.metrics import MetricContext
from djedai.analytics.quality import assess
from djedai.analytics.registry import execute_tool
from djedai.backend.config import Settings
from djedai.backend.dataset_service import DatasetService
from djedai.backend.db.models import DashboardModel, DatasetModel, ToolResultModel
from djedai.backend.db.repositories import (
    _escape_sql_literal,
    _resolve_data_ref_path,
    get_owned_tool_result,
    save_tool_result,
)
from djedai.backend.db.repositories import (
    create_dashboard as repo_create_dashboard,
)
from djedai.backend.db.repositories import (
    get_dashboard as repo_get_dashboard,
)
from djedai.backend.db.repositories import (
    get_dataset as repo_get_dataset,
)
from djedai.backend.db.repositories import (
    list_dashboards as repo_list_dashboards,
)
from djedai.backend.db.repositories import (
    update_dashboard as repo_update_dashboard,
)
from djedai.backend.metric_service import MetricService
from djedai.contracts.chart import ChartSpec, ChartTemplate
from djedai.contracts.dashboard import (
    DashboardCreate,
    DashboardPatch,
    DashboardSnapshot,
    DashboardSummary,
    WidgetLayout,
    WidgetRecipe,
    WidgetRecipeStep,
    WidgetSnapshot,
    WidgetToolCall,
)
from djedai.contracts.freshness import Consistency, ReadProvenance
from djedai.contracts.tool_result import ToolError, ToolResult, ToolResultColumn

logger = logging.getLogger(__name__)

MAX_RECIPE_STEPS = 5


def _dashboard_consistency(reads: list[ReadProvenance]) -> Consistency:
    """Сводит чтения виджетов к согласованности снимка всего дашборда."""
    if not reads:
        return "unknown"
    values = {read.consistency for read in reads}
    if len(values) != 1:
        return "independent"
    consistency = values.pop()
    if consistency == "turn_snapshot":
        snapshot_ids = {read.snapshot_id for read in reads}
        if len(snapshot_ids) == 1 and None not in snapshot_ids:
            return "turn_snapshot"
        return "independent"
    return consistency


def _generated_consistency(dataset: DatasetModel, widgets: list[WidgetSnapshot]) -> Consistency:
    """Согласованность дашборда, собранного агентом в одном ходе чата."""
    if not any(widget.read_started_at is not None for widget in widgets):
        return "unknown"
    return "turn_snapshot" if dataset.source_kind == "postgres" else "immutable"


def _to_widget_snapshot(data: dict[str, Any]) -> WidgetSnapshot:
    """Парсит сохранённый словарь виджета в WidgetSnapshot."""
    try:
        return WidgetSnapshot.model_validate(data)
    except Exception:
        # Если виджет хранился без актуального снимка (или был повреждён)
        layout_dict = data.get("layout") or {"x": 0, "y": 0, "w": 6, "h": 4}
        layout = WidgetLayout.model_validate(layout_dict)
        chart_dict = data.get("chart") or {}
        chart = (
            ChartSpec.model_validate(chart_dict)
            if "result_id" in chart_dict and chart_dict["result_id"]
            else ChartTemplate.model_validate(
                {k: v for k, v in chart_dict.items() if k != "result_id"}
            )
        )
        tool_call = (
            WidgetToolCall.model_validate(data["tool_call"])
            if data.get("tool_call") is not None
            else None
        )
        recipe = (
            WidgetRecipe.model_validate(data["recipe"]) if data.get("recipe") is not None else None
        )
        return WidgetSnapshot(
            widget_id=data.get("widget_id", f"w_{uuid4().hex[:12]}"),
            tool_call=tool_call,
            recipe=recipe,
            chart=chart,
            caption=data.get("caption"),
            layout=layout,
            result_id=None,
            status="error",
            error=ToolError(
                code="not_refreshed",
                message="Дашборд ещё не обновлялся",
                suggestion="Нажмите кнопку 'Обновить данные'",
            ),
        )


def _to_dashboard_snapshot(model: DashboardModel) -> DashboardSnapshot:
    """Формирует DashboardSnapshot из SQLAlchemy модели."""
    snapshots = [_to_widget_snapshot(w) for w in (model.widgets or [])]
    return DashboardSnapshot(
        dashboard_id=model.dashboard_id,
        dataset_id=model.dataset_id,
        title=model.title,
        description=model.description,
        source_chat_id=model.source_chat_id,
        widgets=snapshots,
        consistency=model.consistency or "independent",
        created_at=model.created_at,
        refreshed_at=model.refreshed_at,
    )


class DashboardService:
    """Сервис управления дашбордами и автономного пересчёта виджетов."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def list_dashboards(
        self, db: Session, owner_id: str, dataset_id: str
    ) -> list[DashboardSummary]:
        """Возвращает список дашбордов владельца для указанного датасета."""
        dataset = repo_get_dataset(db, dataset_id)
        if dataset is None or dataset.owner_id != owner_id:
            raise HTTPException(404, "Датасет не найден")
        models = repo_list_dashboards(db, owner_id=owner_id, dataset_id=dataset_id)
        return [
            DashboardSummary(
                dashboard_id=m.dashboard_id,
                dataset_id=m.dataset_id,
                title=m.title,
                refreshed_at=m.refreshed_at,
            )
            for m in models
        ]

    def get_dashboard(self, db: Session, owner_id: str, dashboard_id: str) -> DashboardSnapshot:
        """Возвращает снимок дашборда без запуска вычислений."""
        dashboard = repo_get_dashboard(db, dashboard_id, owner_id=owner_id)
        if dashboard is None:
            raise HTTPException(404, "Дашборд не найден")
        dataset = repo_get_dataset(db, dashboard.dataset_id)
        if dataset is None or dataset.owner_id != owner_id:
            raise HTTPException(404, "Датасет не найден")
        return _to_dashboard_snapshot(dashboard)

    def create_or_add_widget(
        self, db: Session, owner_id: str, payload: DashboardCreate
    ) -> DashboardSnapshot:
        """Сохраняет график в новый или существующий дашборд с реконструкцией рецепта."""
        # 1. Проверяем датасет
        dataset = repo_get_dataset(db, payload.dataset_id)
        if dataset is None or dataset.owner_id != owner_id:
            raise HTTPException(404, "Датасет не найден")

        # 2. Проверяем результат и права владельца
        root_record = get_owned_tool_result(
            db, payload.result_id, owner_id, dataset_id=payload.dataset_id
        )
        if root_record is None:
            raise HTTPException(404, "Результат не найден")

        # 3. Реконструируем рецепт цепочки шагов (provenance)
        chain: list[ToolResultModel] = [root_record]
        curr = root_record
        seen_ids = {root_record.result_id}

        while curr.source_result_id:
            if len(chain) >= MAX_RECIPE_STEPS:
                raise HTTPException(
                    422,
                    f"Глубина зависимостей превышает лимит (максимум {MAX_RECIPE_STEPS} шагов)",
                )
            parent = get_owned_tool_result(
                db, curr.source_result_id, owner_id, dataset_id=payload.dataset_id
            )
            if parent is None:
                raise HTTPException(
                    422,
                    "Невозможно сохранить виджет: исходный шаг вычисления не найден или недоступен",
                )
            if parent.result_id in seen_ids:
                raise HTTPException(422, "Обнаружен цикл в зависимостях результатов")
            seen_ids.add(parent.result_id)
            chain.append(parent)
            curr = parent

        # Разворачиваем в хронологическом порядке от корня к листу
        chain.reverse()

        tool_call: WidgetToolCall | None = None
        recipe: WidgetRecipe | None = None

        if len(chain) == 1:
            rec = chain[0]
            clean_params = {k: v for k, v in (rec.params or {}).items() if k != "source_result_id"}
            tool_call = WidgetToolCall(tool=rec.tool or "run_sql", params=clean_params)
        else:
            steps: list[WidgetRecipeStep] = []
            for i, step_record in enumerate(chain):
                step_id = f"step_{i + 1}"
                source_step_id = f"step_{i}" if i > 0 else None
                clean_params = {
                    k: v for k, v in (step_record.params or {}).items() if k != "source_result_id"
                }
                steps.append(
                    WidgetRecipeStep(
                        step_id=step_id,
                        source_step_id=source_step_id,
                        call=WidgetToolCall(
                            tool=step_record.tool or "run_sql", params=clean_params
                        ),
                    )
                )
            recipe = WidgetRecipe(steps=steps, output_step_id=steps[-1].step_id)

        # Валидируем шаблон диаграммы
        ChartTemplate(**payload.chart.model_dump(exclude={"result_id"}))

        # 4. Определяем дашборд
        target_dashboard: DashboardModel | None = None
        if payload.dashboard_id is not None:
            target_dashboard = repo_get_dashboard(db, payload.dashboard_id, owner_id=owner_id)
            if target_dashboard is None or target_dashboard.dataset_id != payload.dataset_id:
                raise HTTPException(404, "Дашборд не найден")

        # 5. Вычисляем позицию виджета в сетке
        existing_widgets = copy.deepcopy(target_dashboard.widgets or []) if target_dashboard else []
        next_y = 0
        for w in existing_widgets:
            layout_data = w.get("layout") or {}
            y = layout_data.get("y", 0)
            h = layout_data.get("h", 4)
            next_y = max(next_y, y + h)

        layout = WidgetLayout(x=0, y=next_y, w=6, h=4)
        new_widget_id = f"w_{uuid4().hex[:12]}"
        root_read = ReadProvenance.model_validate(root_record.read) if root_record.read else None

        # Снимок для нового виджета с сохраняемым результатом
        widget_snapshot = WidgetSnapshot(
            widget_id=new_widget_id,
            tool_call=tool_call,
            recipe=recipe,
            chart=payload.chart,
            caption=payload.chart.subtitle,
            layout=layout,
            result_id=payload.result_id,
            status=root_record.status if root_record.status in ("ok", "empty") else "ok",
            error=None,
            read_started_at=root_read.started_at if root_read is not None else None,
            read_finished_at=root_read.finished_at if root_read is not None else None,
        )

        existing_widgets.append(widget_snapshot.model_dump(mode="json"))

        if target_dashboard is not None:
            updated = repo_update_dashboard(
                db,
                target_dashboard.dashboard_id,
                owner_id=owner_id,
                widgets=existing_widgets,
                # Новый виджет мог быть посчитан в другом снимке источника.
                consistency="independent",
            )
            assert updated is not None
            return _to_dashboard_snapshot(updated)
        else:
            title = payload.title or payload.chart.title or "Новый дашборд"
            new_dash_id = f"d_{uuid4().hex[:16]}"
            source_chat_id = root_record.chat_id
            created = repo_create_dashboard(
                db,
                dashboard_id=new_dash_id,
                owner_id=owner_id,
                dataset_id=payload.dataset_id,
                title=title,
                widgets=existing_widgets,
                source_chat_id=source_chat_id,
                consistency=root_read.consistency if root_read is not None else "independent",
            )
            return _to_dashboard_snapshot(created)

    def save_generated_dashboard(
        self,
        db: Session,
        owner_id: str,
        dataset_id: str,
        *,
        title: str,
        widgets: list[WidgetSnapshot],
        description: str | None = None,
        source_chat_id: str | None = None,
    ) -> DashboardSnapshot:
        """Сохраняет дашборд, собранный агентом, вместе со снимками виджетов.

        Виджеты уже содержат рецепт перевыполнения и результат последнего
        вычисления, поэтому повторное открытие показывает превью без запуска
        refresh. Временные ``source_result_id`` в рецептах отсутствуют: зависимости
        хранятся как ``source_step_id``.
        """
        dataset = repo_get_dataset(db, dataset_id)
        if dataset is None or dataset.owner_id != owner_id:
            raise HTTPException(404, "Датасет не найден")
        if not 3 <= len(widgets) <= 8:
            raise HTTPException(422, "Дашборд должен содержать от 3 до 8 виджетов")
        widget_ids = [widget.widget_id for widget in widgets]
        if len(set(widget_ids)) != len(widget_ids):
            raise HTTPException(422, "Идентификаторы виджетов должны быть уникальны")

        for widget in widgets:
            if widget.result_id is None:
                continue
            record = get_owned_tool_result(db, widget.result_id, owner_id, dataset_id=dataset_id)
            if record is None:
                raise HTTPException(404, "Результат виджета не найден")
            if widget.status in ("ok", "empty") and record.status not in ("ok", "empty"):
                raise HTTPException(422, "Результат виджета ещё не готов")

        dashboard_id = f"d_{uuid4().hex[:16]}"
        created = repo_create_dashboard(
            db,
            dashboard_id=dashboard_id,
            owner_id=owner_id,
            dataset_id=dataset_id,
            title=title,
            description=description,
            widgets=[widget.model_dump(mode="json") for widget in widgets],
            source_chat_id=source_chat_id,
            consistency=_generated_consistency(dataset, widgets),
        )
        updated = repo_update_dashboard(
            db,
            created.dashboard_id,
            owner_id=owner_id,
            refreshed_at=datetime.now(UTC),
        )
        assert updated is not None
        return _to_dashboard_snapshot(updated)

    def patch_dashboard(
        self, db: Session, owner_id: str, dashboard_id: str, patch: DashboardPatch
    ) -> DashboardSnapshot:
        """Изменяет заголовок, удаляет виджеты или обновляет расположение сетки."""
        dashboard = repo_get_dashboard(db, dashboard_id, owner_id=owner_id)
        if dashboard is None:
            raise HTTPException(404, "Дашборд не найден")
        dataset = repo_get_dataset(db, dashboard.dataset_id)
        if dataset is None or dataset.owner_id != owner_id:
            raise HTTPException(404, "Датасет не найден")

        # Создаем глубокую независимую копию виджетов, не мутируя исходный ORM JSON
        widgets = copy.deepcopy(dashboard.widgets or [])

        if patch.remove_widget_ids:
            remove_set = set(patch.remove_widget_ids)
            widgets = [w for w in widgets if w.get("widget_id") not in remove_set]

        if patch.layout:
            for w in widgets:
                wid = w.get("widget_id")
                if wid in patch.layout:
                    w["layout"] = patch.layout[wid].model_dump()

        new_title = patch.title if patch.title is not None else dashboard.title
        updated = repo_update_dashboard(
            db, dashboard_id, owner_id=owner_id, title=new_title, widgets=widgets
        )
        assert updated is not None
        return _to_dashboard_snapshot(updated)

    def refresh_dashboard(
        self,
        db: Session,
        dataset_service: DatasetService,
        metric_service: MetricService,
        owner_id: str,
        dashboard_id: str,
    ) -> DashboardSnapshot:
        """Перевыполняет все виджеты дашборда автономно без активного чата."""
        dashboard = repo_get_dashboard(db, dashboard_id, owner_id=owner_id)
        if dashboard is None:
            raise HTTPException(404, "Дашборд не найден")
        dataset = repo_get_dataset(db, dashboard.dataset_id)
        if dataset is None or dataset.owner_id != owner_id:
            raise HTTPException(404, "Датасет не найден")

        context = dataset_service.get_context(owner_id, dashboard.dataset_id)
        catalog = context.catalog
        metrics = metric_service.confirmed(owner_id, dashboard.dataset_id)
        semantics = MetricContext(catalog, metrics)

        budget_calls = self.settings.dashboard_max_tool_calls
        calls_count = 0

        allowed_base_tables = {t.original_name for t in catalog.tables} | {
            t.table_id for t in catalog.tables
        }
        new_snapshots: list[WidgetSnapshot] = []

        with context.open_reader() as reader:
            connection = reader.connection
            # Все виджеты одного refresh видят один снимок PostgreSQL (B1-11, #169).
            reader.share_snapshot(f"dashboard:{dashboard_id}")
            widget_reads: list[list[ReadProvenance]] = []
            for w_dict in dashboard.widgets or []:
                reads: list[ReadProvenance] = []
                widget_reads.append(reads)
                w_snapshot = _to_widget_snapshot(w_dict)
                chart_template = (
                    ChartTemplate(**w_snapshot.chart.model_dump(exclude={"result_id"}))
                    if isinstance(w_snapshot.chart, ChartSpec)
                    else w_snapshot.chart
                )

                try:
                    # 1. Одиночный вызов инструмента
                    if w_snapshot.tool_call is not None:
                        if calls_count >= budget_calls:
                            new_snapshots.append(
                                WidgetSnapshot(
                                    widget_id=w_snapshot.widget_id,
                                    tool_call=w_snapshot.tool_call,
                                    recipe=w_snapshot.recipe,
                                    chart=chart_template,
                                    caption=w_snapshot.caption,
                                    layout=w_snapshot.layout,
                                    result_id=None,
                                    status="error",
                                    error=ToolError(
                                        code="tool_budget_exceeded",
                                        message=(
                                            "Превышен лимит вызовов refresh "
                                            f"(максимум {budget_calls})"
                                        ),
                                        suggestion="Уменьшите число виджетов на дашборде",
                                    ),
                                )
                            )
                            continue

                        calls_count += 1

                        def run_tool_call(
                            snapshot: WidgetSnapshot = w_snapshot,
                        ) -> ToolResult:
                            assert snapshot.tool_call is not None
                            result = execute_tool(
                                snapshot.tool_call.tool,
                                snapshot.tool_call.params,
                                connection=connection,
                                allowed_tables=allowed_base_tables,
                                semantics=semantics,
                            )
                            return assess(result, connection, catalog)

                        tool_res, read = reader.run(run_tool_call)
                        reads.append(read)
                        new_res_id = f"r_{uuid4().hex[:16]}"
                        tool_res = tool_res.model_copy(
                            update={"result_id": new_res_id, "read": read}
                        )
                        save_tool_result(
                            db,
                            tool_res,
                            chat_id=None,
                            owner_id=owner_id,
                            persist_data=True,
                            storage_dir=self.settings.results_data_dir,
                        )

                        if tool_res.status == "error":
                            new_snapshots.append(
                                WidgetSnapshot(
                                    widget_id=w_snapshot.widget_id,
                                    tool_call=w_snapshot.tool_call,
                                    recipe=w_snapshot.recipe,
                                    chart=chart_template,
                                    caption=w_snapshot.caption,
                                    layout=w_snapshot.layout,
                                    result_id=None,
                                    status="error",
                                    error=tool_res.error
                                    or ToolError(
                                        code="execution_failed",
                                        message="Не удалось выполнить вычисление виджета",
                                    ),
                                )
                            )
                        else:
                            spec = ChartSpec(**chart_template.model_dump(), result_id=new_res_id)
                            new_snapshots.append(
                                WidgetSnapshot(
                                    widget_id=w_snapshot.widget_id,
                                    tool_call=w_snapshot.tool_call,
                                    recipe=w_snapshot.recipe,
                                    chart=spec,
                                    caption=w_snapshot.caption,
                                    layout=w_snapshot.layout,
                                    result_id=new_res_id,
                                    status=tool_res.status,
                                    error=None,
                                )
                            )

                    # 2. Зависимый рецепт вычислений
                    elif w_snapshot.recipe is not None:
                        steps = w_snapshot.recipe.steps
                        step_results: dict[str, ToolResult] = {}
                        recipe_failed = False
                        failure_error: ToolError | None = None

                        for step in steps:
                            if calls_count >= budget_calls:
                                recipe_failed = True
                                failure_error = ToolError(
                                    code="tool_budget_exceeded",
                                    message=(
                                        f"Превышен лимит вызовов refresh (максимум {budget_calls})"
                                    ),
                                    suggestion="Уменьшите число виджетов на дашборде",
                                )
                                break

                            step_allowed_tables = set(allowed_base_tables)
                            registered_view = False

                            if step.source_step_id is not None:
                                parent_res = step_results.get(step.source_step_id)
                                if parent_res is None or parent_res.status == "error":
                                    recipe_failed = True
                                    failure_error = (
                                        parent_res.error
                                        if parent_res and parent_res.error
                                        else ToolError(
                                            code="dependency_failed",
                                            message=(
                                                f"Шаг '{step.source_step_id}' завершился ошибкой"
                                            ),
                                        )
                                    )
                                    break

                                # Безопасно регистрируем DataFrame в соединении DuckDB
                                try:
                                    df = None
                                    if parent_res.data_ref:
                                        pq_path = _resolve_data_ref_path(
                                            parent_res.data_ref,
                                            self.settings.results_data_dir,
                                            expected_result_id=parent_res.result_id,
                                        )
                                        if pq_path.is_file():
                                            with duckdb.connect() as reader_con:
                                                escaped = _escape_sql_literal(pq_path.as_posix())
                                                df = reader_con.execute(
                                                    f"SELECT * FROM {escaped}"
                                                ).df()
                                    if df is None and parent_res.data:
                                        cols = [
                                            c.name if isinstance(c, ToolResultColumn) else str(c)
                                            for c in parent_res.data.columns
                                        ]
                                        df = pd.DataFrame(parent_res.data.rows, columns=cols)

                                    if df is not None:
                                        connection.register("source_result", df)
                                        registered_view = True
                                        step_allowed_tables.add("source_result")
                                    else:
                                        recipe_failed = True
                                        failure_error = ToolError(
                                            code="dependency_data_missing",
                                            message="Данные предыдущего шага недоступны",
                                        )
                                        break
                                except Exception as e:
                                    recipe_failed = True
                                    failure_error = ToolError(
                                        code="dependency_error",
                                        message=f"Не удалось подключить данные шага: {str(e)}",
                                    )
                                    break

                            calls_count += 1
                            step_params = dict(step.call.params)

                            def run_recipe_step(
                                current_step: WidgetRecipeStep = step,
                                params: dict[str, Any] = step_params,
                                allowed: set[str] = step_allowed_tables,
                                has_view: bool = registered_view,
                            ) -> ToolResult:
                                try:
                                    return execute_tool(
                                        current_step.call.tool,
                                        params,
                                        connection=connection,
                                        allowed_tables=allowed,
                                        semantics=semantics,
                                    )
                                finally:
                                    if has_view:
                                        try:
                                            connection.unregister("source_result")
                                        except Exception:
                                            pass

                            step_res, read = reader.run(run_recipe_step)
                            reads.append(read)
                            new_step_id = f"r_{uuid4().hex[:16]}"
                            parent_res = (
                                step_results.get(step.source_step_id)
                                if step.source_step_id
                                else None
                            )
                            parent_res_id = parent_res.result_id if parent_res else None
                            step_res = step_res.model_copy(
                                update={
                                    "result_id": new_step_id,
                                    "source_result_id": parent_res_id,
                                    "read": read,
                                }
                            )
                            save_tool_result(
                                db,
                                step_res,
                                chat_id=None,
                                owner_id=owner_id,
                                persist_data=True,
                                storage_dir=self.settings.results_data_dir,
                            )
                            step_results[step.step_id] = step_res

                            if step_res.status == "error":
                                recipe_failed = True
                                failure_error = step_res.error or ToolError(
                                    code="step_failed",
                                    message=f"Ошибка на шаге '{step.step_id}'",
                                )
                                break

                        if recipe_failed:
                            new_snapshots.append(
                                WidgetSnapshot(
                                    widget_id=w_snapshot.widget_id,
                                    tool_call=w_snapshot.tool_call,
                                    recipe=w_snapshot.recipe,
                                    chart=chart_template,
                                    caption=w_snapshot.caption,
                                    layout=w_snapshot.layout,
                                    result_id=None,
                                    status="error",
                                    error=failure_error,
                                )
                            )
                        else:
                            out_res = step_results.get(w_snapshot.recipe.output_step_id)
                            if out_res is not None and out_res.status in ("ok", "empty"):
                                spec = ChartSpec(
                                    **chart_template.model_dump(), result_id=out_res.result_id
                                )
                                new_snapshots.append(
                                    WidgetSnapshot(
                                        widget_id=w_snapshot.widget_id,
                                        tool_call=w_snapshot.tool_call,
                                        recipe=w_snapshot.recipe,
                                        chart=spec,
                                        caption=w_snapshot.caption,
                                        layout=w_snapshot.layout,
                                        result_id=out_res.result_id,
                                        status=out_res.status,
                                        error=None,
                                    )
                                )
                            else:
                                new_snapshots.append(
                                    WidgetSnapshot(
                                        widget_id=w_snapshot.widget_id,
                                        tool_call=w_snapshot.tool_call,
                                        recipe=w_snapshot.recipe,
                                        chart=chart_template,
                                        caption=w_snapshot.caption,
                                        layout=w_snapshot.layout,
                                        result_id=None,
                                        status="error",
                                        error=out_res.error
                                        if out_res
                                        else ToolError(
                                            code="missing_output",
                                            message="Результат вычисления не найден",
                                        ),
                                    )
                                )

                except Exception:
                    logger.exception("Widget %s refresh failed unexpectedly", w_snapshot.widget_id)
                    new_snapshots.append(
                        WidgetSnapshot(
                            widget_id=w_snapshot.widget_id,
                            tool_call=w_snapshot.tool_call,
                            recipe=w_snapshot.recipe,
                            chart=chart_template,
                            caption=w_snapshot.caption,
                            layout=w_snapshot.layout,
                            result_id=None,
                            status="error",
                            error=ToolError(
                                code="unexpected_error",
                                message="Неожиданный сбой при обновлении виджета",
                            ),
                        )
                    )

        for index, snapshot in enumerate(new_snapshots):
            reads = widget_reads[index] if index < len(widget_reads) else []
            started = [read.started_at for read in reads if read.started_at is not None]
            finished = [read.finished_at for read in reads if read.finished_at is not None]
            if started and finished:
                new_snapshots[index] = snapshot.model_copy(
                    update={
                        "read_started_at": min(started),
                        "read_finished_at": max(finished),
                    }
                )

        consistency = _dashboard_consistency([read for reads in widget_reads for read in reads])
        refreshed_at = datetime.now(UTC)
        updated = repo_update_dashboard(
            db,
            dashboard_id,
            owner_id=owner_id,
            widgets=[s.model_dump(mode="json") for s in new_snapshots],
            refreshed_at=refreshed_at,
            consistency=consistency,
        )
        assert updated is not None
        return _to_dashboard_snapshot(updated)
