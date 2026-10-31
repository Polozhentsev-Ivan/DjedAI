# Качество, проверка и ограничения

Эта глава объясняет, как проверять DjedAI и какие выводы дают результаты разных уровней. Существование кода, валидная схема, успешный unit-тест и принятый пользовательский сценарий — разные подтверждения.

## Уровни проверки

| Уровень | Что проверяется | Чего он сам по себе не доказывает |
| --- | --- | --- |
| Repository | Структура, tracked files, UTF-8, локальные ссылки, безопасный example, выбор CI | Правильность расчётов или полный поиск секретов в истории |
| Unit | Изолированная логика, маленькие реальные данные, contracts, scope, ошибки/cancel | Работу настоящего внешнего источника или качество LLM |
| Integration | API/БД/файлы, миграции, persistence, owner, registry → SQL → storage | Все комбинации UI/данных/моделей |
| Frontend | Компоненты, состояние, API/session/SSE, форматы и ChartSpec | Полный браузерный сценарий на production-сборке |
| Browser/Compose | UI → API → controlled model, proxy, upload, restart и данные | Семантическое качество настоящей модели |
| Реальная модель | Конкретные запросы/ответы и задержки выбранного профиля | SLA или устойчивость всех сценариев без достаточных повторов |
| Golden/eval | Сопоставление вопросов с независимыми ожиданиями и рубриками | Автоматическую проверку всех смысловых утверждений |

Минимум coverage — 80% строк Python и отдельно 80% TS/TSX. Их не усредняют. Высокий процент не заменяет проверки доступа, чисел и сохранности; тесты должны подтверждать значимое поведение.

## Локальный повтор

Из корня checkout:

```sh
node --test scripts/check-repository.test.mjs scripts/check-ci-changes.test.mjs
node scripts/check-repository.mjs
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python scripts/export_frontend_contracts.py --check
uv run --locked pytest --strict-markers -m "not integration and not llm" --cov --cov-config=pyproject.toml --cov-report=term-missing --cov-fail-under=80
```

```sh
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run test:coverage
npm --prefix frontend run build
```

Integration и `llm` выбираются явно. Обычные проверки не требуют платного ключа модели. Настоящие PG/CH тестируются выделенными runner-скриптами с Docker; прямой integration pytest без требуемого DSN может быть пропущен, что не является пройденной приёмкой.

```sh
uv run --locked python scripts/test_postgres_isolation.py --output ./artifacts/postgres-isolation.json
uv run --locked python scripts/test_clickhouse_isolation.py --output ./artifacts/clickhouse-isolation.json
uv run --locked pytest --strict-markers tests/integration/test_analytics_tools_runtime.py
```

Последний файл проверяет зарегистрированные инструменты через выбранные реальные HTTP/runtime/storage-сценарии. Это приёмочные тесты, а не отдельный production startup gate, который автоматически перепроверяет каждое вычисление пользователя.

## Браузер и сохранность

Из `frontend/`:

```sh
npx playwright install chromium
npm run test:e2e:auth
npm run test:e2e
```

Auth harness проверяет transport на настоящем API; production UI проверяет формы и взаимодействие с backend/управляемой моделью. Compose/nginx и restart выполняются отдельным шагом workflow и smoke. Сервер управляемой модели не измеряет качество/latency внешнего провайдера.

Для запущенного приложения `smoke:cp1` использует настоящий API модели и может расходовать средства. Задаются отдельные тестовые `DJEDAI_SMOKE_USERNAME/PASSWORD`, результат пишется вне Git; `--verify` проверяет тот же комплект после restart. [Инструкция](../../frontend/README.md).

Сохранность означает чтение прежних каталогов, истории, result IDs, SQL/параметров и полных данных. Наличие SQLite-строки при потерянном Parquet или fallback preview не является сохранностью полного результата. Backup/restore проверяется отдельно по [operations.md](../administrator/operations.md).

## Независимые данные

### Синтетика

`scripts/generate_synthetic_data.py` создаёт stores/products/sales/campaigns с фиксированным seed. `verify_synthetic_data.py` независимо читает CSV средствами stdlib/Decimal и сверяет 11 эталонов; связанные проверки дополняют таблицы и JOIN.

```sh
uv run --locked python scripts/verify_synthetic_data.py
```

Смысл метрик зафиксирован: raw-продажи включают дубли/аномалии, деньги — записанный gross total_amount, не пересчитанное quantity×price. Количественный и денежный пик могут различаться. Эти определения входят в ожидаемый ответ.

### Golden

`eval/questions.yaml` содержит 53 вопроса по десяти сценариям и неудобным обращениям. Источник — закреплённый полный журнал фоновых заданий HK_Datasets с 500000 строками и manifest/hash. Одна строка — попытка задания; повтор chain_id не равен полному дублю. Денежных показателей в этом файле нет.

```sh
uv run --locked python scripts/verify_golden.py --validate-only
```

Это проверка структуры: `oracle_verified=false`, агент не запускается. Полный oracle дополнительно требует CSV вне checkout и независимо пересчитывает численные ожидания без приложения/LLM. Сохранённые 94 проверки фактов не означают 94 правильных ответа агента.

### Eval агента

Прогонщик `scripts/eval_runner.py` уже реализован. Он запускает вопросы через настоящий граф/каталог/инструменты и записывает статусы, траектории, результаты, блоки и проверки фактов. Чужие готовые ответы/ожидания не подставляются в prompt.

```sh
uv run --locked python scripts/eval_runner.py --data-file ../data/08_Фоновые_задания.csv --output ../reports/eval-report.json
```

Нужен полный исходник, совпадающий с manifest, настроенный endpoint и явный выбор такого прогона. JSON отчёта записывается атомарно по ходу работы; interrupted/pending не превращаются в completed/pass. Зависимый контекст исполняет предыдущие вопросы реально, а сбой предпосылки блокирует продолжение.

Автоматические проверки не покрывают большинство смысловых рубрик. Такие рубрики получают `not_evaluated`, а вопрос — `needs_review`: это не принятый ответ, его оценивает человек. Отчёт разделяет `automated_status` и `semantic_status`. Форматы и команды — [eval/README.md](../../eval/README.md).

## Протокол проверки

Для воспроизводимого результата фиксируются:

- полный SHA приложения и данные/их hash;
- конфигурация модели, endpoint/profile и число повторов без секретов;
- точная команда и выбранные markers/файлы;
- ожидаемые числа, единицы, популяции, способ независимого расчёта;
- фактические outcomes, ошибки/skips/timeouts и ссылки на артефакты;
- границы вывода: локальный smoke, browser, источник, модель или полный eval.

Для latency отдельно считаются время первого события, полезного результата и полного завершения; ранний status не является готовым ответом. Ошибки не исключаются из статистики успеха. Стоимость оценивается только по известному usage и датированному тарифу; неизвестный usage/charge не приравнивается нулю.

## Ограничения выбранной версии

| Область | Практическая граница |
| --- | --- |
| Ответы модели | Typed JSON, детерминированная проверка чисел (#167) и LLM verify не дают универсальной гарантии всех чисел/смысла. Итог проверки сохраняется в отчёте, ответ с неподтверждённым числом может быть заменён отказом ([решение #167](../decisions/167-numeric-verification.md)). Dashboard — отдельная ветка с собственной проверкой плана/результатов. |
| Внешний inference | Ограниченный контекст отправляется провайдеру даже при `ALLOW_DATA_SHARING=false`; это не offline-режим. |
| Источники | PostgreSQL живой, профиль может устареть. ClickHouse — материализованный snapshot с ограничением размера, не безлимитный pushdown. |
| Статистика | NULL, малые группы, постоянные ряды, временные бакеты и единицы влияют на применимость. Корреляция не доказывает причинность; прогноз не является известным будущим. |
| Лимиты | Row limit ограничивает выдачу, не стоимость сканирования. Квоты/очередь/deadline имеют разную область. |
| Отмена | HTTP модели/SQL прерываются, произвольный синхронный CPU-код кооперативен. Permit удерживается до выхода worker. |
| Сохранение | SQLite/файлы не одна транзакция; backup включает весь app-data и прежний ключ. |
| Большие результаты | Текущий result data API не является общим пагинированным/стриминговым хранилищем произвольного объёма. |
| Удаление | Полного HTTP lifecycle удаления dataset с файлами/производными объектами нет. |
| Настройки модели | Общие на установку; model choice не создаёт персональный provider аккаунта. |
| Масштабирование | Один backend/локальные файлы; несколько реплик требуют отдельной архитектуры. |
| Сеть | `ALLOWED_OUTBOUND_HOSTS` не заменяет реально настроенный egress filter. |

Подробные условия — [данные](data-and-catalog.md), [аналитика](analytics.md), [агент](agent.md), [конфигурация](../configuration.md), [эксплуатация](../administrator/operations.md).

## Исторические доказательства и текущая готовность

[CP1 smoke](../archive/cp1-smoke-run.md), [HK_Datasets](../pl3-hk-datasets-run.md), [reasoning benchmark](../decisions/159-benchmark-validation.md) и [runtime-тесты](../../tests/integration/README.md) имеют свои SHA/окружения. Их нельзя складывать в вымышленный полный прогон сегодняшней версии.

Актуальный статус CI смотрится в [Actions](https://github.com/Polozhentsev-Ivan/DjedAI/actions). Docs-only workflow может законно пропустить application suites; это не новый полный релизный прогон. [Устройство CI](project-workflow.md).

Сверка этой документации с исходниками не означает повторного запуска модели, полного suite, чистой установки или восстановления. Такие прогоны указываются отдельными протоколами, а не подразумеваются от даты руководства.
