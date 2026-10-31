# Справочники, решения и история

## Подробные справочники

Эти документы дополняют руководства точными форматами и сценариями. Номер задачи помогает найти историю решения; для знакомства с продуктом достаточно глав выше.

- Чат: [блоки ответа](chat-blocks.md), [исходы вопросов и восстановление](chat-turns.md), [графики](chart-renderer.md).
- Модель: [deadline и отмена](agent-deadlines.md), [диагностический журнал](../administrator/agent-diagnostics.md), [быстрый маршрут без модели](agent-fast-path.md), [проверка чисел](../decisions/167-numeric-verification.md), [профиль и замеры](../archive/model-profile-pl1.md).
- Аналитика: [подробные инструменты](analytics-tools.md), [семантика метрик](../decisions/166-metric-semantics.md), [потери типизации](../decisions/168-typing-losses.md).
- Источники: [общий Python-интерфейс](../decisions/62-data-interface.md), [ClickHouse](../decisions/61-clickhouse.md), [ревизия/свежесть каталога](../decisions/169-catalog-revision.md).
- Dashboard: [контракты и рецепты](../decisions/47-contracts-and-runtime.md), [API/refresh](../decisions/97-dashboard-api-refresh.md), [создание агентом](../decisions/78-dashboard-branch.md).
- Доступ: [хранение AUTH](../decisions/101-auth-storage.md), [HTTP AUTH](../decisions/102-auth-api.md), [ownership](../decisions/103-owner-isolation.md), [browser session](../decisions/104-browser-auth.md), [лимиты и очередь](../decisions/172-concurrency-queues-upload.md).
- Контекст внешней модели: [единая политика данных](../decisions/173-data-transfer-policy.md).
- Компоненты: [backend](../../src/djedai/backend/README.md), [данные](../../src/djedai/data/README.md), [аналитика](../../src/djedai/analytics/README.md), [агент](../../src/djedai/agent/README.md), [frontend](../../frontend/README.md), [contracts](../../src/djedai/contracts/README.md).
- Инфраструктура: [Compose/nginx/demo](../../infra/README.md), [CI](../../infra/ci.md), [скрипты](../../scripts/README.md), [тесты](../../tests/README.md), [golden/eval](../../eval/README.md).

- Dashboard из графика чата: [«Как посчитано» и «В дашборд»](../decisions/88-how-calculated-save-to-dashboard.md).

## Решения и исторические материалы

Решения и протоколы описывают состояние на свою дату; текущее поведение сверяется с главами и кодом.

- [Спецификация](../specification.md) — требования и исходная архитектура; текущий объём сверяется с кодом и поздними решениями.
- [План задач](../archive/issues-plan.md) и [GitHub Issues](https://github.com/Polozhentsev-Ivan/DjedAI/issues) — декомпозиция и актуальное состояние работы.
- [Реестр PL-1](../decisions/47-contracts-and-runtime.md) — общие договорённости.
- [UI-design](../ui-design/README.md) — макеты, а не отдельное рабочее приложение.
- [smoke CP1](../archive/cp1-smoke-run.md), [прогон HK_Datasets](../pl3-hk-datasets-run.md), [reasoning benchmark](../decisions/159-benchmark-validation.md) — датированные протоколы.
- [Исходный DOCX](../source/case-2-ai-native-bi.docx) и [визуальный план](../source/ai-native-bi-plan.html) — материалы кейса.
