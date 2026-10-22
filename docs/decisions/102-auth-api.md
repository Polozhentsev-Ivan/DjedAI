# Регистрация и Bearer API — AUTH-2 / #102

HTTP API использует [PL-1](47-contracts-and-runtime.md) и
[AUTH-1](101-auth-storage.md): SQLite, Argon2id и случайные серверные сессии.
В БД хранится SHA-256 токена. Срок — 86400 секунд без продления;
logout отзывает только текущую сессию.

| Метод и путь | Запрос | Результат |
| --- | --- | --- |
| `POST /api/auth/register` | `UserCreate`: username, password, необязательный email | 201 `UserRead`; без создания сессии |
| `POST /api/auth/login` | `UserLogin`: username, password | 200 `TokenResponse`: access_token, token_type=bearer, expires_in=86400 |
| `GET /api/auth/me` | `Authorization: Bearer <access_token>` | 200 `UserRead` |
| `POST /api/auth/logout` | тот же заголовок | 204 без тела; токен отозван |

Логин чувствителен к регистру; логин и пароль не нормализуются. Границы
регистрации и входа: username 3–50 символов, password 6–1024. Пустой логин,
невалидный UTF-8 и неверная форма запроса дают 422 без отражения input.
Email — поле профиля, не альтернативный идентификатор входа.

Ошибки следуют `ErrorResponse`. Занятый username — 409. Неизвестный,
отключённый аккаунт и неверный пароль — одинаковый 401. Для отсутствующего
или неподходящего хеша выполняется холостая проверка Argon2.
`get_current_user` проверяет срок, отзыв и активность аккаунта при каждом
запросе, без запасного CP1-владельца. Токен принимается только в одном Bearer
заголовке; URL/cookie игнорируются. Недействительная сессия, включая повторный
logout, даёт 401 и `WWW-Authenticate: Bearer`.

## Защита запросов

Ответы `/api/auth/*`, включая ошибки, имеют `Cache-Control: no-store` и
`Pragma: no-cache`. Пароли и хеши не возвращаются; токен возвращается при входе.
Пароли и токены исключены из `repr`; пароли в OpenAPI помечены `writeOnly`.
Обработанные ошибки БД не раскрывают SQL/параметры в ответе или журнале исключений.

POST регистрации и входа ограничены до JSON-разбора и хеширования:

- `AUTH_RATE_LIMIT_REQUESTS=10` попыток за `AUTH_RATE_LIMIT_WINDOW_SECONDS=300`
  секунд, отдельно на адрес клиента и endpoint. Учитываются также успешные и
  невалидные запросы. Блокировка не продлевает окно.
- Максимум четыре одновременных запроса и 4096 активных окон. При заполнении
  новые адреса ждут освобождения окна. Ответ — 429 с `Retry-After`.
- Тело не более 16 KiB фактически полученных байтов, включая chunked;
  превышение даёт 413. На доставку всего тела отводится 10 секунд, затем 408
  и освобождение слота; таймер не продлевается отдельными чанками.

Лимиты находятся в памяти одного backend и сбрасываются при перезапуске.
За одним NAT пользователи разделяют лимит адреса. Для нескольких backend нужен
общий механизм лимитов. Приложение берёт адрес из ASGI, не из клиентского XFF.
Контейнерный backend не публикует порт; его entrypoint доверяет forwarded
заголовкам из внутренней сети. Публичный nginx заменяет входящую цепочку
X-Forwarded-For адресом соединения. При изменении сети/proxy нужно пересмотреть
доверие; для самостоятельного Uvicorn нельзя открывать доверие произвольным
forwarded-заголовкам. Внешнее развёртывание требует HTTPS.

## Браузер и следующие задачи

`frontend/src/api/auth.ts` предоставляет типизированные register/login/me/logout.
`session.ts` хранит токен в памяти и `sessionStorage` вкладки. Общие JSON,
FormData и GET/POST SSE через fetch используют один Bearer-заголовок; токена
нет в URL и localStorage. Cookie-сессий нет, cookie-CSRF механизм не нужен.
CORS сохраняет явный allowlist и разрешает Authorization. Хранение в
sessionStorage не защищает от XSS — ограничение согласованного транспорта.

Смена/очистка токена отменяет старые запросы и потоки. Запоздалые login/logout,
401 и JSON-ответы не изменяют более новую сессию. После reload вызывают me()
до восстановления личного workspace. Формы и очистка UI/QueryClient — #104.

После миграций auth API работает и при `CP1_LOCAL_MODE=false`; entrypoint
пропускает CP1-bootstrap в этом режиме. Защита ресурсов, SQL, файлов и SSE
реализована в [AUTH-3 / #103](103-owner-isolation.md). Полный сценарий двух
пользователей с формами и очисткой интерфейса — #105 после #104.

Проверки: `uv run --locked pytest tests/unit/backend/test_auth_api.py`,
`uv run --locked pytest tests/integration/test_auth_migrations.py`; из frontend —
`npm run test:coverage` и `npm run test:e2e:auth`.

Браузерный тест использует настоящий API, SQLite с миграциями, общий клиент
React и защищённые маршруты-пробы с production get_current_user. Он проверяет
JSON, файл, GET/POST SSE, reload, перезапуск backend, logout и истечение.
Пробы существуют только в `tests/e2e/serve_auth.py`, HTML-вход — в dev-сервере
теста, вне production dist. Это проверка транспорта, не владения ресурсами.

Правила общего ответа об ошибке и ограничения попыток сверены с
[OWASP Authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html),
исключение токенов из кэша — с
[OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).
