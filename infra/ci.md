# CI: расход минут и свой runner

## Какие проверки запускаются

Workflow [ci.yml](../.github/workflows/ci.yml) запускается для PR в `main`, push в
`main`/`ci/**` и вручную. Задания `repository` и `required` выполняются всегда:
проверяют структуру, ссылки, настройки и результат остальных заданий. Даже PR с
документацией должен пройти эти две проверки.

[check-ci-changes.mjs](../scripts/check-ci-changes.mjs) пропускает `python`,
`frontend` и `browser`, если весь диапазон изменений содержит только Markdown
вне `prompts/` либо статические документы/изображения в `docs/`
(`txt`, `rst`, `png`, `jpg`, `jpeg`, `gif`, `webp`, `svg`, `pdf`, `docx`).
Markdown в `prompts/` используется приложением: его изменения запускают полный CI.
Код, зависимости, Docker, workflow и неизвестные типы файлов также запускают все
проверки. Переименование кода в документ учитывает и удалённый исходный путь.

Для PR сравнивается вся ветка от общего предка с базовой веткой, для push — весь
диапазон `before..after`. Ручной запуск всегда полный. Если событие/история Git
недоступны, диапазон пуст или ветка только создана, выполняется полный CI.
Решение записывается в summary запуска. Workflow целиком через `paths-ignore`
не отключается, поэтому обязательная проверка `required` получает результат.

В `python` и `browser` сохраняются кэш uv и установленный Python. Ключ учитывает
`uv.lock`, `pyproject.toml` и `.python-version`. В `frontend` и `browser` setup-node
сохраняет кэш загрузок npm по `frontend/package-lock.json`. Установка остаётся
`uv sync --locked` / `npm ci`: готовые `.venv` и `node_modules` не переносятся.
Кэш npm/uv на хосте не передаётся внутрь Docker build; Docker и Chromium по-прежнему
устанавливаются/собираются обычными командами. Первый запуск наполняет кэш.

Проверка логики выбора заданий локально:

```sh
node --test scripts/check-repository.test.mjs scripts/check-ci-changes.test.mjs
node scripts/check-repository.mjs
```

## Как работает свой runner

GitHub хранит workflow, очередь, статусы и логи. Служба GitHub Actions Runner на
вашей Linux-машине получает задание, выполняет команды workflow и возвращает
результат. Компьютер должен быть включён и иметь исходящий HTTPS-доступ к GitHub,
реестрам контейнеров, PyPI и npm; входящие порты для связи runner с GitHub не нужны.
Если подходящего runner нет онлайн, задания ожидают его в очереди.

GitHub не тарифицирует минуты self-hosted runner. Оплачиваются ваша машина и её
обслуживание; использование GitHub-хранилища артефактов/кэша учитывается отдельно.
См. [требования GitHub](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
и [биллинг Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

### Размер машины для нынешнего CI DjedAI

Это начальная инженерная оценка по составу workflow, без замера пиков памяти и
нагрузочного тестирования. Значения рассчитаны на **один runner и одно одновременно
выполняемое задание**; Python, frontend и browser будут проходить последовательно.

| Вариант | vCPU | RAM | SSD |
| --- | --- | --- | --- |
| Экономный старт, сборки медленнее | 2 | 8 ГБ | 60 ГБ |
| Рекомендуемый старт | 4 | 8 ГБ | 80–100 ГБ |
| Запас для роста тестов и сборок | 4 | 16 ГБ | 100 ГБ |

ОС — Ubuntu Server 24.04 LTS x86-64. Нужны Git, Docker Engine с Compose v2,
актуальный GitHub Actions Runner и системные зависимости Chromium. Node, Python и
uv workflow устанавливает в закреплённых версиях. Шаг `playwright install --with-deps`
требует возможности установить системные пакеты; это нужно учесть в правах runner
или подготовке его образа. Под диск учитывайте образы Python/Node/nginx,
PostgreSQL/ClickHouse, сборочные слои и кэши; следите за свободным местом.

**GPU не нужен:** нынешний CI использует управляемую тестовую модель, реальные
провайдеры и локальный inference в нём не запускаются. Тесты `llm` выполняются
отдельно. Постоянно работающие demo-БД для CI тоже не нужны — нужные контейнеры
создаются тестами.

Для двух параллельных тяжёлых заданий ориентир — суммарно 8 vCPU / 16 ГБ RAM / от
120 ГБ SSD, выделенные двум изолированным runner-VM. Нельзя просто запустить два
runner на одном Docker-хосте: auth/production UI harness используют фиксированные
порты, а maintenance hook рассчитан на отсутствие соседнего задания. Compose smoke
уже имеет отдельные проект, тома, сеть и динамический порт; для нескольких runner
нужна также изоляция остальных тестов и hooks.

### Подключение

1. Подготовить отдельную VM и пользователя для CI. Команды PR выполняются с
   доступом к Docker, поэтому на VM не должно быть рабочих данных приложения,
   ключей production и доступа к внутренним системам, которые тестам не нужны.
2. В репозитории открыть **Settings → Actions → Runners → New self-hosted runner**,
   выбрать Linux/x64 и выполнить выданные GitHub команды на VM. Регистрационный
   токен временный; в репозиторий его не добавлять.
3. Добавить runner метку `djedai-ci` и установить его как системную службу.
4. В отдельном изменении workflow заменить `runs-on: ubuntu-24.04` у всех пяти
   заданий на `runs-on: [self-hosted, linux, x64, djedai-ci]`. Если оставить часть
   заданий на GitHub-hosted, для них по-прежнему нужен доступный лимит/бюджет.
5. Запустить полный workflow вручную, проверить Docker, Chromium, статусы,
   длительность, пик RAM и свободное место. Затем подобрать ресурсы по замерам.

Инструкция регистрации: [Adding self-hosted runners](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners).
Workflow использует `[self-hosted, linux, x64, djedai-ci]` во всех пяти заданиях.

### Локальный runner на Windows через WSL 2

[setup-local-runner.ps1](../scripts/setup-local-runner.ps1) создаёт отдельный дистрибутив
`DjedAI-CI` на базе официального образа Ubuntu 24.04. Для первичной установки нужны
работающие WSL 2, Docker Desktop в режиме Linux containers и авторизованный `gh`
с административным доступом к репозиторию. Запустить из PowerShell:

```powershell
./scripts/setup-local-runner.ps1
```

Дистрибутив хранится в `%LOCALAPPDATA%/DjedAI-CI/linux`. Скрипт
[setup-local-runner.sh](../scripts/setup-local-runner.sh) устанавливает собственный
Docker Engine и Compose, проверяет SHA256 архива runner по данным API GitHub,
создаёт пользователя `djedai` и службу `djedai-ci`. Метки runner:
`self-hosted`, `Linux`, `X64`, `djedai-ci`. Один runner выполняет задания по очереди.
Node, npm, Python, uv и Chromium устанавливаются командами workflow.

В `DjedAI-CI` отключены автоматическое подключение Windows-дисков и запуск Windows
программ. Docker Desktop используется только для создания начального образа:
CI работает со своим Docker Engine, поэтому его Compose-проекты, тома и порты
не затрагивают Docker Desktop. Это отдельное окружение WSL, не граница безопасности
для выполнения произвольного недоверенного кода. Runner предназначен для этого
закрытого репозитория; не подключайте к нему публичные проекты с внешними PR.
Регистрационный токен передаётся через stdin и не сохраняется в файлах проекта.

Задание планировщика Windows `DjedAI CI Runner` запускается при входе текущего
пользователя. Оно удерживает WSL работающим: одних служб systemd для этого
недостаточно. Перезагрузка Windows требует повторного входа пользователя;
во время сна и выключения компьютера новые задания будут ждать в GitHub.
Служба runner закреплена за восемью логическими процессорами; глобальные настройки
WSL и параметры питания Windows установщик не меняет. На диске нужно место для
Python/Node, Chromium, образов БД и Docker build cache.

Проверка состояния и управление:

```powershell
gh api repos/Polozhentsev-Ivan/DjedAI/actions/runners
wsl -d DjedAI-CI -u root -- systemctl status djedai-ci docker
wsl -d DjedAI-CI -u root -- journalctl -u djedai-ci -n 100 --no-pager
Start-ScheduledTask -TaskName 'DjedAI CI Runner'
```

Для остановки без удаления runner сначала остановите службу и задачу, затем
отключите автозапуск, если он больше не нужен:

```powershell
wsl -d DjedAI-CI -u root -- systemctl stop djedai-ci
Stop-ScheduledTask -TaskName 'DjedAI CI Runner'
Disable-ScheduledTask -TaskName 'DjedAI CI Runner'
```

Для возврата на GitHub-hosted замените `runs-on` всех пяти заданий на
`ubuntu-24.04`. Обязательная проверка `required` и состав тестов сохраняются.
Browser smoke удаляет только свои тестовые Compose-тома после завершения;
это предотвращает накопление пользователей и данных между запусками.

Перед каждым заданием и после него runner удаляет контейнеры с метками
`org.djedai.postgres-acceptance`, `org.djedai.clickhouse-acceptance` и тестовый
ресурсы с меткой `org.djedai.compose-acceptance=true`, включая тестовые тома
`app-data` и сети. Проект рабочего приложения `djedai` не выбирается по имени.
Это убирает остатки после
принудительной отмены, когда обычный `finally` не успел выполниться. Очистка
ограничена 90 секундами и работает только со своим Docker Engine в `DjedAI-CI`.
Другие Docker-контейнеры и образы не удаляются.
Ручной запуск `test-compose.sh` получает значение метки `local`: hook задания
не прерывает параллельную ручную проверку. Её ресурсы удаляет собственный EXIT trap.

После обновления checkout уже установленный hook нужно обновить (между заданиями):

```sh
sudo install -m 0755 scripts/local-runner-maintenance.sh /usr/local/sbin/djedai-ci-maintenance
```

Старый hook продолжает выбирать `djedai`, пока установленная копия не заменена.
Локальное повторение Compose smoke и проверка сохранности приложения на 8080
описаны в [инструкции инфраструктуры](README.md#compose-с-управляемой-моделью-для-ci).

Таймер `djedai-ci-watchdog` раз в минуту проверяет возраст `Runner.Worker`.
Если процесс пережил максимальный таймаут задания и запас на отмену/отправку
результатов (всего 35 минут), служба runner перезапускается. Обычные задания
и вручную остановленная служба не перезапускаются. Задание `required` проверяет
все результаты средствами Bash: ему не нужны повторные checkout и загрузка Node.

При использовании VPN в режиме TUN он должен предоставлять доступ из WSL к
`github.com`, `api.github.com` и доменам `*.actions.githubusercontent.com`.
Служба systemd может быть активна даже при статусе runner `offline` в GitHub:
это разные проверки. При смене VPN могут также зависнуть загрузка зависимостей
и отправка логов. Проверяйте оба состояния и журнал:

```powershell
wsl -d DjedAI-CI -u root --cd / -- systemctl status djedai-ci-watchdog.timer
wsl -d DjedAI-CI -u root --cd / -- journalctl -u djedai-ci -u djedai-ci-watchdog --since '30 minutes ago' --no-pager
```

### Если нужен сервер самого приложения

CI и работающее приложение имеют разные нагрузки. Для пилота с внешней моделью,
небольшими CSV и несколькими пользователями начальный ориентир — 2–4 vCPU,
4–8 ГБ RAM и 40–80 ГБ SSD плюс место под данные и резервные копии. При размещении
демо-PostgreSQL и ClickHouse вместе с приложением разумнее начинать с 4 vCPU и
8–16 ГБ RAM. Это также оценка, а не подтверждённая ёмкость по пользователям:
размер датасетов, параллельные запросы DuckDB и сложность аналитики требуют замеров.

Runner лучше держать в отдельной VM от приложения: тесты пересобирают контейнеры,
перезапускают Compose и выполняют код из PR. Сервер локальной LLM подбирается
отдельно по модели, квантованию, длине контекста и числу одновременных запросов;
указанные выше размеры его не включают.
