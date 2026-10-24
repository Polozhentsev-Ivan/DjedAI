# Handoff: DjedAI — локальная AI-аналитическая платформа (BI)

## Обзор
Веб-приложение для бизнес-аналитиков: загрузка CSV / подключение PostgreSQL или ClickHouse → проверка распознанной структуры → вопросы к данным на естественном языке → ответ с фактами, графиком и прозрачным расчётом → сохранение графиков на дашборды. Язык интерфейса — русский. Основной сценарий — десктоп от 1280 px, каркас сжимается до планшета.

## О файлах
Файлы в этой папке — **дизайн-референсы в HTML** (прототипы с итоговым видом и поведением), а не продакшн-код. Задача — воссоздать их на **React + TypeScript + Tailwind + shadcn/ui**, графики — **ECharts** (`echarts-for-react` или собственная обёртка). Каждый `*.dc.html` открывается в браузере напрямую; имена файлов совпадают с именами React-компонентов.

## Точность
**Hi-fi.** Цвета, типографика, отступы, радиусы и состояния — итоговые. Все значения вынесены в CSS-переменные (`tokens.css`); в разметке нет захардкоженных цветов, радиусов, теней, отступов и шрифтов. Исключение — ширины колонок таблиц (`DataTable.columns[].width`): это данные конкретной таблицы.

## Как смотреть
- `DjedAI.dc.html` — интерактивный прототип. Сверху панель: экран / состояние / тема.
- `Frames.dc.html` — доска со всеми экранами и состояниями. Каждый фрейм подписан как `Компонент / Состояние` (например, `CatalogPage / Loading`) и имеет id (`3b`).
- `Components.dc.html` — токены, шрифты, иконки и все компоненты по отдельности.

## Шрифты
| Токен | Стек | Подключение |
|---|---|---|
| `--font-sans` | -apple-system, BlinkMacSystemFont, "SF Pro Text", **Inter**, system-ui | Inter — Google Fonts, 400/600/700, `font-feature-settings: "ss03"` |
| `--font-display` | -apple-system, "SF Pro Display", **Inter** | заголовки ≥ 28px |
| `--font-mono` | "SF Mono", **JetBrains Mono**, ui-monospace | Google Fonts, 400/600 — технические имена колонок, SQL, имена датасетов |

Веса: 400 / 600 / 700, вес 500 не используется. Числа — `font-variant-numeric: tabular-nums`.

## Иконки
**lucide-react** (в прототипе — lucide 0.460 UMD), `strokeWidth={1.75}`, размеры `--size-icon-xs/sm/—/lg/xl` = 12/16/18/20/24.
Используются: `Database, Table2, MessageSquare, LayoutDashboard` (навигация), `Upload, Inbox, LoaderCircle` (с `animate-spin`), `TriangleAlert, Info, CircleHelp, Check, Circle, ChevronDown, ChevronRight, RefreshCw, Plus, Square, ArrowRight, ArrowLeftRight, Download, LogOut`.

## Токены
Полный список — в `tokens.css`. Цвета названы по конвенции shadcn/ui, тёмная тема включается классом `.dark` на `<html>`.

**Подключение к Tailwind v4** (`globals.css`):
```css
@import "tailwindcss";
@import "./tokens.css";
@custom-variant dark (&:is(.dark *));
@theme inline {
  --color-background: var(--background); --color-foreground: var(--foreground);
  --color-card: var(--card); --color-card-foreground: var(--card-foreground);
  --color-popover: var(--popover); --color-popover-foreground: var(--popover-foreground);
  --color-primary: var(--primary); --color-primary-foreground: var(--primary-foreground);
  --color-secondary: var(--secondary); --color-secondary-foreground: var(--secondary-foreground);
  --color-muted: var(--muted); --color-muted-foreground: var(--muted-foreground);
  --color-accent: var(--accent); --color-accent-foreground: var(--accent-foreground);
  --color-destructive: var(--destructive); --color-destructive-muted: var(--destructive-muted);
  --color-success: var(--success); --color-success-muted: var(--success-muted);
  --color-warning: var(--warning); --color-warning-muted: var(--warning-muted);
  --color-info-muted: var(--info-muted); --color-link: var(--link);
  --color-border: var(--border); --color-border-subtle: var(--border-subtle);
  --color-input: var(--input); --color-ring: var(--ring);
  --color-surface-subtle: var(--surface-subtle); --color-inverse: var(--inverse); --color-inverse-foreground: var(--inverse-foreground);
  --color-sidebar: var(--sidebar); --color-sidebar-foreground: var(--sidebar-foreground);
  --color-sidebar-accent: var(--sidebar-accent); --color-sidebar-accent-foreground: var(--sidebar-accent-foreground);
  --color-sidebar-border: var(--sidebar-border); --color-topbar: var(--topbar);
  --color-chart-1: var(--chart-1); --color-chart-2: var(--chart-2); --color-chart-3: var(--chart-3);
  --color-chart-4: var(--chart-4); --color-chart-5: var(--chart-5); --color-chart-6: var(--chart-6);
  --font-sans: var(--font-sans); --font-mono: var(--font-mono);
  --radius-xs: var(--radius-xs); --radius-sm: var(--radius-sm); --radius-md: var(--radius-md); --radius-lg: var(--radius-lg);
  --text-xs: var(--text-xs); --text-sm: var(--text-sm); --text-base: var(--text-base); --text-lg: var(--text-lg); --text-xl: var(--text-xl); --text-2xl: var(--text-2xl); --text-3xl: var(--text-3xl);
}
```
Отступы — шаг 4 px, совпадают со стандартной шкалой Tailwind (`--space-4` = `p-4` = 16px; `--space-3-5` = `p-3.5`).

**Основные значения (светлая / тёмная):**
- background `#ffffff` / `#1d1d1f`; foreground `#1d1d1f` / `#ffffff`; card `#ffffff` / `#272729`
- primary `#0066cc` (обе темы), primary-foreground `#ffffff`; link `#0066cc` / `#2997ff`; ring `#0071e3` / `#2997ff`
- muted-foreground `#6e6e73` / `#a1a1a6`; border `#e0e0e0` / `#3a3a3c`; sidebar `#f5f5f7` / `#000000`; topbar `#000000`
- success `#1b7f45` / `#4cc38a`; warning `#9a5b00` / `#f0b450`; destructive `#c4281c` / `#ff6b5e`
- графики: `#0066cc #34a37a #e3a008 #8e5cd9 #d9577b #7a7a7a` / `#2997ff #4cc38a #f0b450 #b48cf2 #f07a9a #a1a1a6`
- радиусы: xs 5 · sm 8 · md 11 · lg 18 · full 9999
- типографика: 56/40/28 (600, display, tracking −0.28px) · 21/600 · 17/400 lh 1.47 tracking −0.374px · 14/400 lh 1.43 · 12/400
- **тени не используются** (`--shadow-popover: none`): элементы отделяются границей и сменой фона. Фокус: `outline: 2px solid var(--ring); outline-offset: 1px`. Нажатие кнопки: `scale(.95)`.

## Компоненты
| Компонент | Файл | Ключевые props |
|---|---|---|
| AppLayout | AppLayout.dc.html | page, state, theme, compact |
| TopBar | TopBar.dc.html | dataset, datasets, user, compact, onDatasetChange, onLogout |
| DatasetSelect | DatasetSelect.dc.html | value, options[{id, meta, disabled}], onChange, onAdd → shadcn `DropdownMenu` |
| Sidebar | Sidebar.dc.html | active, compact, chats, activeChat, onNavigate, onNewChat |
| StatusBadge | StatusBadge.dc.html | label, tone(success/info/danger/warning/neutral/accent), variant(status/tag/type) → `Badge` |
| DataTable | DataTable.dc.html | columns[{key,label,type,align,width}], rows, minWidth → `Table` |
| ChartCard | ChartCard.dc.html | title, subtitle, chart, height, primaryLabel/onPrimary, secondaryLabel/onSecondary |
| LoadingState / EmptyState / ErrorState | *.dc.html (общая база StateBlock.dc.html) | size(page/inline/compact), title, text, steps, actionLabel, onAction |
| TransformLogItem | TransformLogItem.dc.html | level(info/warning), text |
| ChatMessage | ChatMessage.dc.html | role, text, parts[{t,strong,tone}], facts, streaming, children (блоки ответа) |
| AgentSteps | AgentSteps.dc.html | title, steps[{label,status(done/active/pending),pct}], framed |
| HowCalculated | HowCalculated.dc.html | variant(collapsible/panel), tool, columns[{label,name}], stats, sql, defaultOpen → `Collapsible` |
| ClarifyBlock | ClarifyBlock.dc.html | question, hint, options[{label,name,hint}], onSelect |
| SuggestionChips | SuggestionChips.dc.html | items, onSelect |
| WidgetCard | WidgetCard.dc.html | title, subtitle, kind(chart/kpi), chart, state(default/loading/error), how, kpi, onRemove, onRetry |

Страницы: `LoginPage, SourcesPage, CatalogPage, ChatPage, DashboardsPage`. У каждой есть prop `state: 'default' | 'loading' | 'empty' | 'error'`.

## Экраны и состояния (Frames.dc.html)
- **01 Вход** `/login` — 1a Default · 1b Loading (спиннер, кнопка заблокирована) · 1c Validation (подсказки под пустыми полями) · 1d Error «Неверный логин или пароль» · 1e Error · Register «Пользователь с таким именем уже существует». Без каркаса, карточка max 400px по центру.
- **02 Источники** `/sources` — табы «Загрузить CSV / Подключить БД»; drop-зона; таблица «Ваши датасеты». Loading: шаги загрузки с прогрессом (60%). Empty: EmptyState вместо таблицы. Error: «Файл «sales.csv»: не удалось разобрать строку 3…» + «Загрузить заново».
- **03 Каталог** `/catalog/:id` — краткое описание датасета под заголовком, переключатель таблиц, описание таблицы и DataTable структуры (исходное имя крупнее и жирнее технического; тип, описание колонки, пропуски, примечание), журнал TransformLogItem (преобразования и предупреждения), связи таблиц: граф (узлы — таблицы, стрелка от стороны «многие» к уникальному ключу, подпись — кратность и совпадение ключей; клик по узлу выбирает таблицу) и список с кратностью, уверенностью и совпадением ключей. Без связей — «Связи между таблицами не найдены», граф не показывается. Loading: скелетоны. Empty: «Датасет не выбран». Error: «Не удалось построить профиль таблицы».
- **04 Чат** `/chat/:id` — ChatMessage (user) → ChatMessage (assistant: текст + facts) с ChartCard, HowCalculated, ссылкой на дашборд и SuggestionChips. Loading: AgentSteps, текст стримится с курсором, вместо «Отправить» — «Остановить». Empty: приветствие + 4 карточки типовых вопросов. Error: ClarifyBlock + ErrorState inline «Запрос превысил таймаут 30 сек.» с кнопкой «Повторить».
- **05 Дашборды** `/dashboards/:id` — табы дашбордов датасета, «Обновить данные», сетка WidgetCard `repeat(auto-fit, minmax(360px, 1fr))`. Loading: оверлей LoadingState compact на каждом виджете. Empty: EmptyState. Error: упал один виджет («Топ категорий»), остальные показаны как обычно.
- **06 Тёмная тема**, **07 Планшет** (Sidebar compact 64px, иконки 44×44).

## Поведение
- Навигация: Вход → Источники → «Посмотреть каталог» → «Задать вопрос в чате →» → «+ В дашборд» (тост со ссылкой) или ссылка в ответе → Дашборды. «Выйти» → /login.
- Sidebar: вертикальное меню на всех экранах; в разделе «Чат» под ним «+ Новый чат» и история чатов. Ниже 1024px — compact.
- «Как посчитано» есть у каждого ответа и каждого виджета (у виджета заменяет график, пока раскрыт).
- Форматирование: `1 250 000 ₽`, `3,4 млн ₽`, `12,5%`, `+34%` (success) / `−5%` (destructive, знак U+2212), десятичная запятая, даты `ДД.ММ.ГГГГ`. Используйте `Intl.NumberFormat('ru-RU')`.
- Колонки везде показываются бизнес-именем из файла, техническое имя — вторым, моноширинным шрифтом и приглушённым цветом.
- Анимации: спиннер `rotate 360° за .8s linear infinite`; скелетон `opacity 1→.45→1 за 1.4s`; курсор стриминга мигает `steps(1)` каждые 1s.

## Состояние приложения
`activeDataset`, `datasets[]`, `activeChatId`, `messages[]` (со стримингом и шагами агента), статус запроса для каждой страницы (`idle | loading | empty | error`), `dashboards[]` → `widgets[]` со статусом обновления каждого виджета, `theme`.

## Файлы
`tokens.css`, `charts.js` (опции ECharts для revenue / regions / categories), `Icon.dc.html` (обёртка над lucide), все компоненты `*.dc.html` из таблицы выше, страницы, `DjedAI.dc.html`, `Frames.dc.html`, `Components.dc.html`, `support.js` (среда выполнения прототипа, переносить в код не нужно).
