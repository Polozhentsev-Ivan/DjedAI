# Общие контракты

Единые Pydantic v2 модели для обмена данными между компонентами платформы (B5-1, PL-1).

## Модели пакета

- `catalog.py` — `DatasetCatalog`, `Table`, `Column`, `Relation`, `Transformation` (§3.4);
- `tool_result.py` — `ToolResult`, `Fact`, `ChartHint`, `ToolError`, `ToolResultData` (§4.3);
- `chart.py` — `ChartSpec`, `XAxis`, `YMeasure`, `ColorField` (§6.2);
- `message.py` — `AssistantMessage`, `TextBlock`, `ChartBlock`, `TableBlock`, `ClarificationBlock`, `DashboardLinkBlock`, `SuggestionsBlock` (§5.6);
- `chat.py` — `ChatCreate`, `ChatCreated`, `ChatSummary`, `ChatMessage`, `ChatDetail`, `SendMessageRequest` (§7.3);
- `dashboard.py` — `Dashboard`, `DashboardWidget`, `WidgetToolCall`, `WidgetRecipe`,
  `WidgetRecipeStep`, `WidgetLayout`, `DashboardSnapshot`, `WidgetSnapshot` (§7.2);
- `auth.py` — `UserCreate`, `UserLogin`, `UserRead`, `TokenResponse`;
- `common.py` — `HealthCheck`, `ErrorResponse`, `DatasetSummary`;
- `sse.py` — `AgentStreamEvent`, `DatasetStreamEvent` и события стриминга (`status`, `tool_call`, `tool_result`, `block`, `done`, `error`) (§7.3).

Все модели экспортируются напрямую из пакета `djedai.contracts`.

Согласованные правила — [PL-1 / #47](../../../docs/decisions/47-contracts-and-runtime.md).
`Dashboard` хранит определение; `DashboardSnapshot` — результат refresh с исходом
каждого виджета. Рецепт хранит зависимости шагов вместо прежних UUID результатов.
Успешный обмен, ошибка и частичный сбой зависимого виджета приведены в
[component_exchange.json](examples/component_exchange.json) и проверяются общими моделями.
Обёртка HTTP-ошибки — `ErrorResponse = {"error": ToolError}`, версия API — 0.2.0.

## DatasetCatalog (#59)

`catalog.py` — Pydantic-модель каталога датасета по §3.4 спецификации: источник, таблицы, колонки с типами и статистикой профайлера, связи, предупреждения и журнал преобразований. Модель неизменяема и отклоняет лишние поля. Проверяется и согласованность ссылок: ключи, временные колонки, связи и преобразования должны указывать на существующие таблицы и колонки.

- Полный каталог для интерфейса и `describe_column`: `catalog.model_dump(mode="json")` / `model_dump_json()`; у связи поле называется `from`, как в спецификации.
- Компактный каталог для агента: `catalog.to_prompt()` — таблицы, колонки, связи и предупреждения. Top-values, примеры значений и журнал в него не попадают. В строке колонки выводятся, если есть что показать:
  - тип и роль; `PK` для ключа таблицы и `FK → таблица.колонка` для ссылки на другую таблицу (ключ без уникальности помечается в заголовке таблицы: «ключ order_id (не уникален)»);
  - `валюта` (у `money`, код ISO 4217 из поля `currency`);
  - исходное имя «…» — только если оно отличается от `name` и описание его не передаёт (описания нет или в нём нет текста заголовка, без учёта регистра);
  - `пропусков` и `не распознано` — отдельно, см. ниже;
  - `уникальных` — число уникальных значений у категорий, булевых и текстовых колонок (у id и чисел оно мало что говорит);
  - диапазон `min … max` в данных: у чисел и дат (у дат ещё гранулярность). Это наблюдаемый диапазон, а не допустимый.
- Пропуски и нераспознанные значения — разные вещи. `null_ratio` — доля пустых значений в исходных данных, без нераспознанных. `parse.success_ratio` — доля распознанных среди непустых. В `to_prompt()` «не распознано» — это доля от всех строк: `(1 − success_ratio) × (1 − null_ratio)`. Сумма «пропусков» и «не распознано» — доля NULL в типизированном представлении.
- Примеры: `examples/dataset_catalog.json` (полный, две таблицы и связь), `examples/dataset_catalog_minimal.json` (сразу после загрузки CSV, без описаний LLM и связей), `examples/dataset_catalog.prompt.txt` (эталонный `to_prompt()` полного примера; при изменении формата его нужно обновить).

Поля `description`, `summary` (B1-6), `relations` (B1-5) и `currency` необязательны, пока их некому заполнять. `schema_version` растёт при несовместимых изменениях формата.

Модели должны переиспользоваться потребителями вместо независимых копий. Их содержание и версии согласуются в задаче общих контрактов; бизнес-расчёты, доступ к БД и экраны сюда не относятся.
