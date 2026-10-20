# Frontend

Браузерное приложение DjedAI на React + TypeScript (Vite). Вычисления и данные остаются на Python-backend; интерфейс обращается к нему только через HTTP API и SSE по адресу `/api`.

Вёрстка повторяет макет из [docs/ui-design](../docs/ui-design/README.md): токены, компоненты, экраны и все их состояния.

## Запуск

Нужны Node и npm из [.nvmrc](../.nvmrc) и `engines` в [package.json](package.json) — те же версии, что в CI. С fnm или nvm версия подхватывается из `.nvmrc`:

```sh
fnm use            # или: nvm use
cd frontend
npm ci
npm run dev        # http://localhost:5173
```

В dev по умолчанию включён mock-API (MSW): запросы к `/api` обрабатываются в браузере на данных из макета. Общие backend-контракты уже доступны; функциональные обработчики источников, чатов и дашбордов добавляются в своих задачах. Сценарий выбирается параметром адреса и сохраняется для вкладки:

| Параметр | Что показывает |
|---|---|
| `?mock=normal` | обычные данные (по умолчанию) |
| `?mock=empty` | нет датасетов и дашбордов — пустые состояния |
| `?mock=slow` | запросы не завершаются — состояния ожидания |
| `?mock=error` | ошибка профилирования, сбой одного виджета дашборда, ошибка разбора CSV при загрузке |

Работа с настоящим backend: создать `frontend/.env.local` по образцу [.env.example](.env.example) с `VITE_API_MOCKS=false`. Vite проксирует `/api` на `http://localhost:8000`; другой адрес задаётся переменной окружения при запуске: `DJEDAI_API_PROXY=http://host:8000 npm run dev` (из `.env.local` она не читается — это настройка самого dev-сервера). В production-сборку моки не попадают.

## Команды

| Команда | Что делает |
|---|---|
| `npm run dev` | dev-сервер с mock-API |
| `npm run typecheck` | строгая проверка типов (`tsc -b`) |
| `npm run lint` | ESLint с проверками типов, без предупреждений |
| `npm test` | тесты Vitest |
| `npm run test:coverage` | тесты с покрытием; ниже 80% строк — ошибка |
| `npm run build` | production-сборка в `dist/` |
| `npm run preview` | раздача собранного `dist/` |
| `npm run gen:types` | TS-типы из `schema/openapi.yaml` в `src/api/generated.ts` |

CI выполняет то же самое после `npm ci` и дополнительно проверяет, что `src/api/generated.ts` совпадает с результатом `gen:types`.

## Устройство

```text
schema/openapi.yaml   согласованный снимок API (источник TS-типов)
src/
  api/                клиент, SSE, хуки запросов (TanStack Query), преобразование ответов для экранов
  app/                маршруты, каркас (TopBar + Sidebar), общее состояние
  pages/              экраны: вход, источники, каталог, чат, дашборды, 404
  components/         общие компоненты из макета
  charts/             ECharts по ChartSpec и данным результата
  mocks/              mock-API на MSW: данные и обработчики, общие для dev и тестов
  lib/                форматирование (ru-RU), тема, медиазапросы
  styles/             токены макета и подключение Tailwind
  fixtures/           статичный ответ чата до B4-4 (#86)
  test/               настройка тестов и тестовый сервер
```

### API и типы

[schema/openapi.yaml](schema/openapi.yaml) описывает API-фикстуры экранов и ссылается на [schema/contracts.json](schema/contracts.json) для общих моделей результата, журнала преобразований и SSE. Этот файл воспроизводимо экспортируется из актуальных Pydantic/OpenAPI-контрактов backend; ручные копии этих моделей не ведутся. Предложения по ещё не реализованным обработчикам и дополнительным полям помечены `x-proposal` и остаются в снимке до согласования соответствующего API.

Обновление общих контрактов из корня репозитория и генерация TS-типов:

```sh
uv run --locked python scripts/export_frontend_contracts.py
cd frontend
npm run gen:types
```

Python CI проверяет актуальность `contracts.json`, frontend CI — результат генерации TS. Параметр `--default-non-nullable false` сохраняет необязательность полей с Pydantic-default, включая nullable-поля дескрипторов колонок.

- `src/api/client.ts` — базовый адрес из `VITE_API_BASE_URL` (по умолчанию `/api`), любые ошибки превращаются в `ApiError` с `message` и `suggestion`.
- `src/api/sse.ts` — SSE через `fetch`, в том числе для POST. Разбитые на чанки события собираются, `done`/`error` закрывают поток, отмена через `AbortSignal`. Обрыв без `done` даёт ошибку `stream_interrupted`; запрос никогда не повторяется автоматически, чтобы не запустить агента второй раз.
- `src/api/queries.ts` — хуки по разделам и ключи кэша; `src/api/view.ts` — форматирование ответов для компонентов.

### Как подключить новый запрос к экрану

1. Общий контракт изменить в Python `contracts/`, обновить экспорт; предложение нового пути добавить в `schema/openapi.yaml`, затем выполнить `npm run gen:types`.
2. Добавить хук в `src/api/queries.ts`, при необходимости — преобразование в `src/api/view.ts`.
3. Добавить обработчик в `src/mocks/handlers.ts` (и данные в `src/mocks/db.ts`), чтобы экран работал в dev и тестах.
4. На экране показать `LoadingState` / `EmptyState` / `ErrorState` по статусу запроса — они одинаковы на всех экранах.
5. Тест: страница через `renderRoute()` из `src/test/renderRoute.tsx`, сценарии — `useScenario()` или `server.use()`.

## Зависимости

npm, только точные версии без диапазонов (`.npmrc`: `save-exact=true`, `engine-strict=true`), `package-lock.json` в Git. Новую зависимость добавляем через `npm install <пакет>@<версия>`; проверка репозитория в CI отклоняет диапазоны и рассинхронизацию lock-файла.

## Что делают другие задачи

- Блоки ответа чата и стриминг по SSE — B4-4 (#86); транспорт и mock-поток уже есть.
- Рендер всех типов `ChartSpec` — B4-5; сейчас поддержаны line, area, bar и pie.
- Авторизация — своя задача; экран входа сверстан, но не отправляет запросов.
- Раздача `dist/` и `/api` в Compose — #99: nginx должен отдавать `index.html` на любые пути приложения (`try_files $uri /index.html`), чтобы прямой URL и обновление вложенного маршрута работали.
