# Python-интерфейс блока данных — B1-10 / #62

Дата: 24.10.2026. Документ фиксирует уже работающий фасад как согласованный
интерфейс B1 для backend. Фасад реализован в #144 (CSV), #103/#60 (PostgreSQL) и #61 (ClickHouse).
Формат каталога — `DatasetCatalog` из #59. Новых функций документ не добавляет.

## Фасад

Единая точка входа — `djedai.backend.dataset_service.DatasetService(session_factory,
data_dir, *, source_encryption_key=None)`. В приложении это `app.state.dataset_service`.
В спецификации операции названы `ingest_csv(files)`, `connect_db(dsn)` и
`get_catalog(dataset_id)`. В коде у каждой операции первым аргументом идёт владелец,
а методы приёма асинхронные: источник сохраняется, сборка ставится в очередь.

| Метод | Вход | Результат |
| --- | --- | --- |
| `await ingest_csv(owner_id, sources)` | `Sequence[CsvSource(path, original_name)]`, без HTTP | `DatasetUploadAccepted(dataset_id, status="processing")` |
| `await ingest_uploads(owner_id, files)` | `Sequence[UploadFile]` (multipart-адаптер) | то же |
| `await connect_db(owner_id, dsn, tables=None, *, kind="postgresql", name=None)` | DSN и необязательный список таблиц; `kind` — `postgresql` или `clickhouse` | то же |
| `await check_db(dsn, *, kind="postgresql")` | DSN; ничего не сохраняет (#84) | `SourceCheckResult`: адрес без пароля и таблицы, имена которых принимает `connect_db` |
| `get_catalog(owner_id, dataset_id)` | — | `DatasetCatalog` готового датасета |
| `get_detail(owner_id, dataset_id)` / `list_for_owner(owner_id)` | — | `DatasetDetail` / `list[DatasetSummary]` |
| `get_context(owner_id, dataset_id)` | — | `DatasetContext`: каталог, `open_connection()` для аналитики и `open_reader()` со свежестью источника и происхождением чтения (#169) |
| `await refresh(owner_id, dataset_id)` | — | `DatasetDetail` с `refresh.status="processing"`: перепрофилировка в следующую ревизию каталога, датасет остаётся `ready` (#169) |
| `check_freshness(owner_id, dataset_id)` | — | `SourceFreshness`: схема источника сейчас относительно каталога; ничего не пересчитывается (#169) |
| `events(owner_id, dataset_id)` | — | async-поток `progress`, затем одно `done` или `error` |
| `recover_interrupted()` / `close()` | — | перезапуск незавершённых сборок и перепрофилировок / ожидание фоновых задач |

Методы приёма вызываются в event loop приложения: фоновая сборка привязана к нему.
До возврата `ingest_csv` копирует файлы в собственное хранилище, поэтому вызывающий
код может сразу удалить исходники. Сохранённые CSV и зашифрованный DSN позволяют
пересобрать датасет после перезапуска (`recover_interrupted`).

## Статусы

`processing → ready | error`. Каталог публикуется только после сборки всех таблиц:
частичного каталога не бывает. Ошибка любого файла или таблицы переводит весь
датасет в `error`. Повторно запрашивать статус можно через `get_detail`/`events` или
HTTP `GET /api/datasets/{id}` и `/events`; формат событий — `contracts.sse`.

## Ошибки

Фасад бросает `fastapi.HTTPException`. Python-вызывающий видит код и `detail`,
HTTP-клиент получает `ErrorResponse{error: ToolError}` из обработчика в
`backend/app.py`. Начиная с 500 HTTP-ответ заменяет текст на «Сервис временно
недоступен», а в Python остаётся исходный `detail`.

Ошибки ввода по HTTP бывают двух видов, и у них разный `code`. Запрос, не
прошедший схему FastAPI, отклоняется до вызова фасада обработчиком
`RequestValidationError`. Ошибку проверки внутри фасада возвращает `HTTPException`.
В Python есть только второй вид.

| Условие | Код | `ToolError.code` |
| --- | --- | --- |
| HTTP: запрос не прошёл схему — нет поля `files`, пустой или слишком длинный `dsn`, `kind` не из `postgres`/`clickhouse`, больше 100 таблиц | 422 | `invalid_request` |
| Фасад: пустой список файлов (только Python), файл не `.csv`, некорректный DSN, пустой список таблиц, плохое имя | 422 | `http_422` |
| Неизвестный или чужой `dataset_id` (любой метод чтения, SSE) | 404 | `not_found` |
| Каталог или контекст ещё не готов, файл датасета недоступен, перепрофилировка уже идёт | 409 | `conflict` |
| Схема PostgreSQL несовместима с каталогом: удалена таблица или колонка, изменился тип ([#169](169-catalog-revision.md)) | 409 | `catalog_stale` |
| `kind` не поддерживается (Python-вызов с видом кроме `postgresql`/`clickhouse`) | 501 | `http_501` |
| Не задан `SOURCE_ENCRYPTION_KEY`, сохранённый источник PostgreSQL недоступен | 503 | `unavailable` |
| Сборка завершилась ошибкой | статус `error` | `dataset_processing_failed` в `DatasetDetail.error` |

Терминальное SSE-событие `error` (`SSEErrorData`) при ошибке сборки содержит
только `message` — тот же текст, что `DatasetDetail.error.message`; `code` равен
`null`. Код в потоке задаётся лишь при отзыве или истечении сессии:
`code="unauthorized"`. Чтобы узнать код ошибки сборки, после события `error`
прочитайте `GET /api/datasets/{id}` (`get_detail`).

Текст ошибки сборки — сообщение `CsvReadError` для CSV, сообщение о превышении
порога ClickHouse ([#61](61-clickhouse.md#порог)) или общая фраза. DSN,
пути, SQL и трассировки драйвера не попадают в ответы, SSE, каталог и логи.

## Контекст владельца

`owner_id` — это `users.id` из проверенной сессии (`current_owner_id`). Клиентский
`user_id` и текст модели владельца не задают. Каждый метод проверяет пару
`(owner_id, dataset_id)`, поэтому чужой и несуществующий датасет неразличимы (404).
Фоновая сборка сохраняет владельца, с которым была принята. Подробности —
[AUTH-3](103-owner-isolation.md).

## Этапы #62

- [x] Заглушка для BE: не понадобилась, сразу сделана рабочая реализация (#144).
- [x] CP1: `ingest_csv` + `get_catalog` для CSV (#53, #56, #59, #144).
- [x] CP2: `connect_db` для PostgreSQL (#60, #103).
- [x] FINAL: ClickHouse (#61).

`connect_db(kind="clickhouse")` использует тот же формат запроса, статусы, ошибки и
контекст владельца. Таблицы ClickHouse копируются в файл датасета в пределах порога,
поэтому `DatasetContext.open_connection()` по-прежнему возвращает DuckDB-соединение и
`run_sql`/агент не меняются. Выбор по объёму, порог и приёмка —
[решение B1-9 / #61](61-clickhouse.md).

Открытый вопрос: передавать ли в SSE `error` код `dataset_processing_failed`.
Поле `SSEErrorData.code` для этого уже есть, но это изменение wire-формата SSE,
и его нужно согласовать с владельцем API/SSE.

Проверка сверки Python ↔ HTTP по ID, каталогу и ошибкам —
`tests/integration/test_data_interface.py`. PostgreSQL-часть запускается в
`scripts/test_postgres_isolation.py`.
