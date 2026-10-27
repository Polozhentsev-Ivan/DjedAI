# Dashboard API и refresh — B5-6 / #97

Реализация API дашбордов, воспроизводимых рецептов виджетов и автономного
пересчёта без активного чата (§7.2, #97) поверх согласованных контрактов
[PL-1](47-contracts-and-runtime.md) и модели изоляции [AUTH-3](103-owner-isolation.md).

## 1. Контракты и эндпоинты

| Метод и путь | Запрос / Параметры | Ответ | Описание |
| --- | --- | --- | --- |
| `GET /api/dashboards` | `dataset_id: str` (query) | `200 list[DashboardSummary]` | Личный список дашбордов владельца по датасету |
| `POST /api/dashboards` | `DashboardCreate`: `dataset_id`, `result_id`, `chart`, опц. `dashboard_id` | `200 DashboardSnapshot` | Создание дашборда или добавление виджета в существующий |
| `GET /api/dashboards/{id}` | — | `200 DashboardSnapshot` | Чтение снимка; открытие не выполняет мутаций и пересчёта |
| `PATCH /api/dashboards/{id}` | `DashboardPatch`: `title`, `remove_widget_ids`, `layout` | `200 DashboardSnapshot` | Переименование, удаление виджетов, обновление сетки |
| `POST /api/dashboards/{id}/refresh` | — | `200 DashboardSnapshot` | Автономный пересчёт всех виджетов с новыми `result_id` |

Все эндпоинты требуют обязательную Bearer-сессию и валидируют владение ресурсами.
Обращение к чужим или отсутствующим `dashboard_id`, `dataset_id` или `result_id`
возвращает единый `404 Not Found` без раскрытия факта существования чужих объектов.

## 2. Воспроизводимые рецепты виджетов (reconstruct_widget_definition)

При сохранении виджета через `POST /api/dashboards`:
- Для независимого расчёта (`source_result_id is None`) виджет сохраняется как `WidgetToolCall`.
- Для цепочки расчётов восстанавливается топологически упорядоченный `WidgetRecipe`:
  - Временные UUID `source_result_id` вычищаются из сохранённых параметров шагов (`call.params`),
    заменяясь на детерминированные ссылки `source_step_id -> step_id`.
  - Выходной результат для графика фиксируется в `output_step_id`.
  - Глубина цепочки ограничена `MAX_RECIPE_STEPS = 5` (модель «4 виджета по 5 шагов» из #47).
    Превышение лимита или битые/недоступные звенья отклоняются с HTTP 422.

## 3. Автономный Refresh без чата

При вызове `POST /api/dashboards/{id}/refresh`:
- Вычисления выполняются автономно без обращения к LLM и без активного чата (`chat_id=None`).
- Защищённое соединение DuckDB датасета сохраняет `enable_external_access=false`.
- Для зависимых шагов результат родительского шага регистрируется в DuckDB как in-memory
  представление `source_result` через pandas DataFrame. Представление гарантированно
  удаляется (`connection.unregister("source_result")`) сразу после завершения шага в блоке `finally`.
- Если результат родительского шага усечён лимитом строк (`truncated=true` или
  `row_count > len(rows)`), зависимый шаг не выполняется: виджет получает `status="error"`,
  `result_id=null` и `ToolError(code="dependency_truncated")`. Неполные строки не используются
  как полный источник для агрегата.
- Для каждого успешного пересчёта генерируются **новые `result_id`**, сохраняющие
  родословную (`source_result_id`), а данные доступны через Data API `/api/results/{id}/data`.
- Refresh открывает `SourceReader` и вызывает `share_snapshot`: все виджеты PostgreSQL
  читают один снимок источника. Каждый результат получает `ToolResult.read`, каждый
  `WidgetSnapshot` — `read_started_at`/`read_finished_at`, а `DashboardSnapshot.consistency`
  сохраняется в дашборде (`turn_snapshot` для PostgreSQL, `immutable` для CSV/ClickHouse,
  `independent` при перезапуске транзакции, `unknown` без чтений).
- Бюджет вызовов: `dashboard_max_tool_calls = 20`. При исчерпании виджет получает статус
  `error` с кодом `tool_budget_exceeded`.

## 4. Частичный сбой (Partial Failure)

- Сбой одного виджета (синтаксическая ошибка SQL, отсутствие таблицы, таймаут) не прерывает
  пересчёт соседних виджетов на дашборде.
- Успешные виджеты получают статус `ok` и новые `result_id`.
- Ошибочный виджет получает статус `error`, `result_id=null` и подробную структуру ошибки `ToolError`.
  Согласно решению #47, старые данные не выдаются за свежий расчёт.
- При сбое промежуточного шага в многошаговом рецепте последующие зависимые шаги не запускаются,
  виджет изолируется со статусом `error`.

## 5. Жизненный цикл и перезапуск

Дашборды и результаты персистентны:
- Состояние дашборда сохраняется в таблице `dashboards` (SQLite/Postgres).
- Повторное открытие после перезапуска приложения через `GET /api/dashboards/{id}`
  возвращает сохранённый снимок без выполнения вычислений.
- Последующий `POST /api/dashboards/{id}/refresh` воспроизводит актуальные вычисления
  на текущих данных датасета.
