# Работа с API

API DjedAI обслуживает тот же backend, с которым работает браузер: локальные аккаунты, источники и каталоги, чаты с SSE, вычисления, бизнес-метрики, дашборды и настройки модели. В стандартной поставке базовый адрес — `http://localhost:8080/api`. Backend-порт 8000 не публикуется на хост.

Эта глава объясняет последовательность запросов и важные гарантии. Точные поля, обязательность и варианты объединений берите из актуальной OpenAPI-схемы, а не из копии всех схем в Markdown:

- `GET /api/openapi.json` — маршруты и `components.schemas`;
- `GET /api/schemas` — реестр JSON Schema общих контрактов;
- [`src/djedai/contracts`](../../src/djedai/contracts) — Pydantic-контракты;
- [`backend/api/router.py`](../../src/djedai/backend/api/router.py) и [`routes`](../../src/djedai/backend/api/routes) — фактически подключённые обработчики.

В dev-запуске напрямую на `localhost:8000` FastAPI также предоставляет `/docs` и `/redoc`. Основной nginx проксирует только `/api/`; `/docs` на frontend-порту не является Swagger UI.

## Авторизация и доступ

Ресурсные запросы требуют `Authorization: Bearer TOKEN`. Регистрация не создаёт сессию: после неё нужен отдельный login. Login возвращает непрозрачный токен и `expires_in=86400`; сервер хранит хеш токена и срок/отзыв сессии, а не доверяет владельцу из тела запроса. Logout отзывает только текущую сессию. Cookie-входа, refresh-token API, SSO и передачи токена через query string нет.

| Метод и путь после `/api` | Назначение |
| --- | --- |
| `POST /auth/register` | Создать аккаунт, 201; повтор username — 409 |
| `POST /auth/login` | Проверить пароль, вернуть новую Bearer-сессию |
| `GET /auth/me` | Прочитать пользователя текущей сессии |
| `POST /auth/logout` | Отозвать текущую сессию, 204 |

Username — 3–50 символов, пароль — 6–1024, email необязателен. Схемы не принимают произвольные дополнительные поля. В login ошибка не различает неизвестный username и неверный пароль. Слишком большие/медленные auth-body и частые попытки ограничены до разбора реквизитов; 429 может содержать `Retry-After`.

Чужой dataset/chat/result/dashboard возвращает 404, чтобы не подтверждать существование чужого ресурса. Владелец устанавливается по сессии. `CP1_LOCAL_MODE` не отключает Bearer-проверку. Ошибки до начала SSE имеют обычный HTTP-статус; отзыв/истечение сессии во время потока приводит к терминальному `unauthorized` и закрытию. Потоки повторно проверяют действительность сессии.

### Вход из PowerShell

Примеры рассчитаны на запущенный Compose и выполняются из корня checkout. Пароль вводится через системный диалог терминала, не как литерал в истории команд. `$authJson` временно содержит пароль в памяти; не выводите его.

```powershell
$apiBase = 'http://localhost:8080/api'
$credentials = Get-Credential -Message 'Аккаунт DjedAI: username и пароль'
$authJson = @{
    username = $credentials.UserName
    password = $credentials.GetNetworkCredential().Password
} | ConvertTo-Json
$authBytes = [System.Text.Encoding]::UTF8.GetBytes($authJson)
```

Только для **нового** аккаунта сначала выполните регистрацию:

```powershell
Invoke-RestMethod -Method Post -Uri "$apiBase/auth/register" `
    -ContentType 'application/json; charset=utf-8' -Body $authBytes
```

Затем войдите и сохраните заголовок в переменной; не печатайте `$session` целиком:

```powershell
$session = Invoke-RestMethod -Method Post -Uri "$apiBase/auth/login" `
    -ContentType 'application/json; charset=utf-8' -Body $authBytes
$apiHeaders = @{ Authorization = "Bearer $($session.access_token)" }
Remove-Variable authJson, authBytes, credentials
Invoke-RestMethod -Uri "$apiBase/auth/me" -Headers $apiHeaders
```

Сохраните токен только на время своего клиента. Его нельзя помещать в URL, отправлять в issue или публиковать вместе с HTTP-дампом.

## CSV → каталог → вопрос

### 1. Принять CSV

`POST /datasets/upload` принимает multipart с повторяемыми полями **`files`**, не `file`. Несколько CSV одного запроса составляют один датасет. Не задавайте multipart `Content-Type` вручную: curl/браузер формирует boundary.

Для совместимости с Windows PowerShell 5 используем настоящий `curl.exe`, а не одноимённый alias:

```powershell
$upload = (curl.exe --fail --silent --show-error `
    -H "Authorization: Bearer $($session.access_token)" `
    -F 'files=@tests/fixtures/csv/orders_utf8_comma.csv' `
    "$apiBase/datasets/upload") | ConvertFrom-Json
$datasetId = $upload.dataset_id
$upload
```

Успех — 202 с `dataset_id` и `status: processing`. Он означает, что загрузка принята и обработка поставлена в очередь, а не что каталог уже готов. Превышение размера запроса/квоты даёт 413, переполнение очереди — 429, нехватка места — 507 `insufficient_storage`. Стандартные ограничения CSV: 2 000 000 000 байт на запрос и 5 000 000 000 байт хранилища датасетов владельца. Отдельного лимита одного файла нет; подробности в [configuration.md](../configuration.md#загрузка-очереди-и-квоты).

### 2. Дождаться готовности

`GET /datasets/{id}/events` отдаёт прогресс и терминальный `done` либо `error`. Статус проверяется отдельно через `GET /datasets/{id}`; список доступен в `GET /datasets`.

```powershell
curl.exe --fail --no-buffer --silent --show-error `
    -H "Authorization: Bearer $($session.access_token)" `
    "$apiBase/datasets/$datasetId/events"
$dataset = Invoke-RestMethod -Uri "$apiBase/datasets/$datasetId" -Headers $apiHeaders
$dataset.status
```

Продолжайте только при `ready`; при `error` прочитайте `$dataset.error`. Каталог:

```powershell
$catalog = Invoke-RestMethod -Uri "$apiBase/datasets/$datasetId/catalog" -Headers $apiHeaders
$catalog.tables | Select-Object name, row_count
```

События фоновой обработки сохраняются и доступны при повторном подключении. Повторное чтение GET-потока не запускает обработку заново. Это отличается от POST вопроса. Клиент должен учитывать терминальное событие; успешный HTTP 200 сам по себе не означает успешное профилирование.

### 3. Создать чат и отправить вопрос

```powershell
$chatBody = @{ dataset_id = $datasetId } | ConvertTo-Json
$chat = Invoke-RestMethod -Method Post -Uri "$apiBase/chats" -Headers $apiHeaders `
    -ContentType 'application/json' -Body $chatBody
$chatId = $chat.chat_id
```

`POST /chats` возвращает 201 и `chat_id`; создание чата до `ready` — 409. Каждый вопрос — `POST /chats/{chat_id}/messages` с JSON `text` и необязательным UUIDv4 `request_id`. Ответ — SSE, а не один JSON-документ.

Следующий пример пишет JSON во временный UTF-8 файл, чтобы одинаково передать кириллицу и кавычки через разные версии Windows PowerShell/curl:

```powershell
$requestId = [Guid]::NewGuid().ToString()
$messageFile = New-TemporaryFile
$question = @{
    text = 'Сколько строк в загруженной таблице?'
    request_id = $requestId
} | ConvertTo-Json
[System.IO.File]::WriteAllText($messageFile.FullName, $question, [System.Text.UTF8Encoding]::new($false))
try {
    curl.exe --fail --no-buffer --silent --show-error `
        -H "Authorization: Bearer $($session.access_token)" `
        -H 'Content-Type: application/json' `
        --data-binary "@$($messageFile.FullName)" `
        "$apiBase/chats/$chatId/messages"
} finally {
    Remove-Item -LiteralPath $messageFile.FullName
}
```

У тестового CSV три строки. Модель должна вычислить результат через инструменты; свободная генерация ответа не является достаточной проверкой API. Состояние после завершения или обрыва читайте заново:

```powershell
$history = Invoke-RestMethod -Uri "$apiBase/chats/$chatId" -Headers $apiHeaders
$history.messages | Select-Object message_id, role, turn
```

В Bash для уже полученного токена и реального `chat_id`:

```sh
api_base='http://localhost:8080/api'
read -r -s -p 'Bearer token: ' api_token
printf '\n'
read -r -p 'chat_id: ' chat_id
curl --fail --no-buffer --silent --show-error \
  -H "Authorization: Bearer $api_token" \
  -H 'Content-Type: application/json' \
  --data-binary '{"text":"Сколько строк в загруженной таблице?"}' \
  "$api_base/chats/$chat_id/messages"
```

## Как читать SSE и управлять ходом

Событие имеет стандартную форму, завершается пустой строкой и может приходить частями сетевых чанков:

```text
event: status
data: {"node":"understand","text":"Разбираю вопрос","message_id":"m_example","run_id":"run_example","phase":"stage"}

```

| Событие агента | Смысл |
| --- | --- |
| `status` | Стадия/повтор; `message_id` связывает вопрос, `run_id` — диагностику |
| `tool_call` | Начат инструмент с параметрами |
| `tool_result` | Получен сохранённый `result_id`, `ok` и возможная ошибка |
| `block` | Структурированный блок ответа: текст, таблица, график и другие варианты контракта |
| `done` | Ход успешно завершён, `message_id` относится к assistant message |
| `error` | Терминальная ошибка с нормализованным кодом; `done` после неё не ожидается |

Профилирование датасета имеет собственный union `progress`/`done`/`error`. Это другой контракт, хотя транспорт тот же. Схемы union и блоков опубликованы в OpenAPI.

Приложение передаёт прогресс и готовые блоки; это не обязательный посимвольный stream текста модели. Парсер клиента должен поддерживать UTF-8, CRLF/LF, несколько `data:`-строк и произвольное разбиение входа на чанки. Фактический browser-клиент использует fetch с Authorization; стандартный `EventSource` не подходит для POST с JSON и произвольным Bearer-заголовком. Реализация — [frontend.md](frontend.md).

В одном чате нельзя выполнять два хода одновременно. До открытия SSE проверяются доступ, повтор `request_id` и admission: возможны 401, 404, 409, 429. В очереди ожидание по умолчанию до 10 с; сам принятый ход имеет отдельный бюджет 120 с. При разрыве потока новый POST не отправляется автоматически.

`request_id` позволяет связать явную отправку с сохранённым вопросом и отклонить уже принятый UUID. Это **не** API идемпотентного повторного получения готового ответа: повтор принятого ID получает 409. UUID нельзя переиспользовать между новыми вопросами. После неясного сетевого исхода прочитайте историю, а не вслепую повторяйте POST.

### Отмена

`POST /chats/{chat_id}/messages/{message_id}/cancel` отменяет конкретный сохранённый user message и возвращает его `ChatTurnState`. ID вопроса приходит в `status`; при заданном UUID он имеет форму `m_` + UUID без дефисов. До фактического приёма вопроса запись может ещё отсутствовать, и cancel получит 404.

В отдельном клиентском запросе, когда вопрос уже принят:

```powershell
$userMessageId = 'm_' + $requestId.Replace('-', '')
Invoke-RestMethod -Method Post -Uri "$apiBase/chats/$chatId/messages/$userMessageId/cancel" `
    -Headers $apiHeaders
```

Затем клиент прерывает исходный fetch/чтение SSE. Закрытие соединения также запускает отмену; явный cancel позволяет надёжно сохранить пользовательское намерение. Ответ cancel подтверждает исход вопроса, но не обещает мгновенного уничтожения любого стороннего Python-вычисления. Worker освобождает admission-слот после своего фактического завершения.

История user message содержит `turn`: `running`, `success`, `error`, `timeout`, `cancelled` либо `interrupted`, последнюю стадию, код, даты, run и ссылку на assistant при успехе. У старых сообщений поле может быть `null`. Частичный непроверенный черновик после timeout не превращается в успешный ответ. Подробности — [chat-turns.md](chat-turns.md).

## Результаты и происхождение чисел

`GET /results/{id}` возвращает `ToolResult` с метаданными, параметрами, фактами и происхождением вычисления без загрузки строк Parquet. `GET /results/{id}/data` читает сохранённые строки в `ToolResultData`. Это API для конкретного расчёта, а не произвольное скачивание файла по пути.

```powershell
$resultId = 'r_замените_реальным_ID'
$result = Invoke-RestMethod -Uri "$apiBase/results/$resultId" -Headers $apiHeaders
$resultData = Invoke-RestMethod -Uri "$apiBase/results/$resultId/data" -Headers $apiHeaders
```

Берите реальный ID из `tool_result`, блоков ответа или истории. У результата без табличных данных `/data` может вернуть 404. Источник/ревизия и время чтения важны для живого PostgreSQL; сохранённый результат не пересчитывается от GET. Формат provenance и доступные инструменты — [analytics.md](analytics.md).

## Подключения БД и актуальность

| Метод и путь | Поведение |
| --- | --- |
| `POST /datasets/connect/check` | Проверить read-only источник, вернуть таблицы, ничего не сохранять |
| `POST /datasets/connect` | Сохранить зашифрованные реквизиты, поставить обработку, 202 |
| `GET /datasets/{id}` | Состояние, каталог, ошибка, ревизия, freshness и refresh |
| `GET /datasets/{id}/catalog` | Опубликованный каталог |
| `POST /datasets/{id}/freshness` | Проверить изменения схемы источника без построения нового каталога |
| `POST /datasets/{id}/refresh` | Явно перепрофилировать, 202; прежняя ревизия доступна во время обработки |

Проверка принимает `kind: "postgres"` либо `"clickhouse"` и write-only `dsn`; подключение дополнительно принимает `tables`. PostgreSQL — `postgresql://reader:password@host:5432/database`, ClickHouse — HTTP(S) URL вида `http://reader:password@host:8123/database`. Это формы адресов, не готовые реквизиты; специальные символы пароля в URL кодируются. Адрес должен быть доступен **из backend**, поэтому `localhost` контейнера не обозначает БД хоста.

Пример без литерала пароля: оператор получает DSN через закрытый ввод, преобразует его в JSON и отправляет check; при необходимости сохранения отправляет тот же объект в connect с `tables`, выбранными из ответа. Схемы ограничивают DSN 8192 символами и выбор 100 таблицами. Не логируйте payload таких запросов. Сервер возвращает безопасное описание источника без пароля и список таблиц; `truncated` означает, что показаны не все.

Для сохранения нужен корректный серверный `SOURCE_ENCRYPTION_KEY`; ключ не передаётся в запросе. Реальные права читателя нужны в самой БД. PostgreSQL остаётся живым read-only источником, ClickHouse материализуется локально с порогами объёма. Freshness не равна полному сравнению значений и не запускает тихий rebuild. Контракты ревизий/актуальности и сценарии обновления — [data-and-catalog.md](data-and-catalog.md).

## Метрики и дашборды

`GET /datasets/{id}/metrics` читает подтверждённые определения, `?history=true` включает историю. `POST` на тот же путь подтверждает определение новой версией; версию, статус и подтверждение назначает сервер. `DELETE /datasets/{id}/metrics/{metric_id}` выводит метрику из употребления, 204. Определение — структурированный `MetricDefinition`, не произвольный текст формулы; бизнес-семантика описана в [analytics.md](analytics.md).

Дашборды привязаны к датасету и владельцу:

- `GET /dashboards?dataset_id=...` — список;
- `POST /dashboards` — создать или добавить виджет согласно `DashboardCreate`;
- `GET /dashboards/{id}` — сохранённый snapshot без пересчёта;
- `PATCH /dashboards/{id}` — название, удаление виджетов и раскладка;
- `POST /dashboards/{id}/refresh` — автономно повторить сохранённые инструменты/рецепты без активного чата.

Кнопка **В дашборд** на графике чата вызывает `POST /dashboards` с `dataset_id`, `result_id`, `chart` и необязательным `dashboard_id` для существующей подборки. Рецепт восстанавливает сервер по цепочке сохранённых результатов; клиент его не передаёт. Чужая ссылка отклоняется с 404, неполная цепочка — с 422. Подробнее — [сохранение графика](../decisions/88-how-calculated-save-to-dashboard.md).

Refresh синхронно возвращает snapshot; это не SSE задачи датасета и не POST вопроса. Ошибки отдельных виджетов и consistency проверяются в самом snapshot, а не только по HTTP 200. Лимит вызовов общих шагов/виджетов — `DASHBOARD_MAX_TOOL_CALLS=20`. Генерация дашборда по естественному вопросу идёт через агентную ветку чата, а не через отдельный несуществующий `/generate` маршрут. См. [agent.md](agent.md) и [руководство пользователя](../user/user-guide.md).

## Настройки модели

`GET /settings/model` возвращает эффективный endpoint/model, разрешённые модели и признаки `api_key_set`, `api_key_from_ui`, `editable_connection`, `matches_env`. Ключ не возвращается. Настройки общие на установку, а не на владельца; ресурсные данные при этом продолжают проверяться по owner.

Выбор разрешённой модели:

```powershell
$modelSettings = Invoke-RestMethod -Uri "$apiBase/settings/model" -Headers $apiHeaders
$modelSettings.allowed_models
$selection = @{ model_name = $modelSettings.allowed_models[0] } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "$apiBase/settings/model" -Headers $apiHeaders `
    -ContentType 'application/json' -Body $selection
Invoke-RestMethod -Method Post -Uri "$apiBase/settings/model/check" -Headers $apiHeaders `
    -ContentType 'application/json' -Body '{}'
```

`PUT` применяет переданные поля; пустая строка возвращает поле к `.env`, отсутствие/null его не меняет. Адрес и ключ разрешены только при `MODEL_SETTINGS_FROM_UI=true`; в серверном default-режиме — 403. Check выполняет один короткий запрос с бюджетом 10 с и ничего не сохраняет. Секрет в check не сохраняется в БД.

`matches_env=false` означает, что пара endpoint+model отличается от серверной: явный reasoning и тариф не применяются, используется restricted data policy. Эта политика всё ещё допускает текущие значения результата; `ALLOW_DATA_SHARING=false` не отключает внешний inference. Подробные правила и защита ключа — [configuration.md](../configuration.md#модель-и-настройки-интерфейса).

## Ошибки и завершение сессии

Обычный ответ ошибки имеет общий `ErrorResponse`:

```json
{"error":{"code":"conflict","message":"Понятное описание причины","suggestion":null}}
```

Тип `ToolError` может содержать дополнительные необязательные поля согласно схеме. 422 имеет безопасный код `invalid_request` без отражения введённых паролей/DSN. Ошибка 5xx не раскрывает текст исключения. Ошибка **после открытия SSE** приходит в `event: error`; HTTP-статус потока при этом уже может быть 200. `tool_result.ok=false` — ошибка конкретного инструмента, которая может обрабатываться агентом; это ещё не обязательно терминальная ошибка всего хода.

```powershell
Invoke-RestMethod -Method Post -Uri "$apiBase/auth/logout" -Headers $apiHeaders
Remove-Variable session, apiHeaders
```

После logout прежний токен недействителен, включая открытые потоки. Не встраивайте автоматический повтор POST в обработчик сетевой ошибки. Сначала читайте сохранённое состояние ресурса/вопроса и только затем решайте, нужно ли новое явное действие.
