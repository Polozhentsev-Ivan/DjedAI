import type { Step } from '@/components/AgentSteps';
import type { Fact, TextPart } from '@/components/ChatMessage';
import type { ClarifyOption } from '@/components/ClarifyBlock';
import type { Calculation } from '@/components/HowCalculated';

export const QUESTION = 'Покажи динамику выручки по месяцам за 2026 год';

export const ANSWER: readonly TextPart[] = [
  { t: 'Выручка стабильно растёт: пик в июле — ' }, { t: '3,4 млн ₽', strong: true }, { t: ', ' }, { t: '+34%', tone: 'success' },
  { t: ' к среднему уровню. После июля — небольшое снижение (' }, { t: '−15%', tone: 'danger' }, { t: ' в августе).' },
];

export const PARTIAL_ANSWER: readonly TextPart[] = [{ t: 'Выручка стабильно растёт: пик в ию' }];

export const FACTS: readonly Fact[] = [
  { label: 'Итого янв–окт', value: '25,4 млн ₽' },
  { label: 'Среднее в месяц', value: '2 540 000 ₽' },
  { label: 'Минимум — январь', value: '1,8 млн ₽' },
];

export const CALCULATION: Calculation = {
  tool: "time_aggregate(table='orders', freq='month')",
  columns: [{ label: 'Сумма с НДС, руб', name: 'amount' }, { label: 'Дата оплаты', name: 'payment_date' }],
  stats: '120 000 строк, исключено 12 строк с пустой датой',
  sql: `SELECT date_trunc('month', payment_date) AS month,
       SUM(amount)                       AS revenue
FROM   orders
WHERE  payment_date >= '2026-01-01'
  AND  payment_date <  '2026-11-01'
GROUP  BY month
ORDER  BY month;`,
};

export const SUGGESTIONS = ['Сравнить с прошлым годом', 'Разбить по регионам', 'Топ-10 клиентов'];

export const AGENT_STEPS: readonly Step[] = [
  { label: 'Определил колонки: «Сумма с НДС, руб», «Дата оплаты»', status: 'done' },
  { label: 'Анализирую данные…', status: 'active' },
  { label: 'Считаю агрегаты…', status: 'pending' },
];

export const CLARIFY_OPTIONS: readonly ClarifyOption[] = [
  { label: 'Сумма заказа', name: 'order_amount', hint: 'все заказы' },
  { label: 'Сумма доставленных', name: 'delivered_amount', hint: 'без отмен и возвратов' },
];

export const STARTERS: readonly { question: string; columns: string }[] = [
  { question: 'Покажи динамику выручки по месяцам', columns: 'Дата оплаты · Сумма с НДС, руб' },
  { question: 'Где больше возвратов?', columns: 'Статус · Регион' },
  { question: 'Топ-10 клиентов по сумме заказов', columns: 'Клиент · Сумма с НДС, руб' },
  { question: 'Как скидка влияет на средний чек?', columns: 'Скидка · Сумма с НДС, руб' },
];
