import type { LogLevel } from '@/components/TransformLogItem';

export interface CatalogColumn {
  source: string;
  system: string;
  type: string;
  missing: string;
  note: string;
  /** Missing share worth a warning. */
  high?: boolean;
}

export interface CatalogTable {
  name: string;
  rows: string;
  columns: readonly CatalogColumn[];
}

export const CATALOG_SUMMARY = '2 таблицы · 120 450 строк · CSV · загружен 18.10.2026 13:52';

export const TABLES: readonly CatalogTable[] = [
  {
    name: 'orders',
    rows: '120 000',
    columns: [
      { source: 'Номер заказа', system: 'order_id', type: 'text (id)', missing: '0%', note: 'Уникальный ключ' },
      { source: 'Дата оплаты', system: 'payment_date', type: 'date', missing: '0,1%', note: 'Формат d.m.y' },
      { source: 'Сумма с НДС, руб', system: 'amount', type: 'money (₽)', missing: '0%', note: '—' },
      { source: 'Скидка', system: 'discount', type: 'percent', missing: '5,2%', note: '—', high: true },
      { source: 'Статус', system: 'status', type: 'category', missing: '0%', note: '4 значения' },
      { source: 'Клиент', system: 'customer_id', type: 'text (id)', missing: '0%', note: 'Связь с customers.id' },
    ],
  },
  {
    name: 'customers',
    rows: '450',
    columns: [
      { source: 'ID клиента', system: 'id', type: 'text (id)', missing: '0%', note: 'Уникальный ключ' },
      { source: 'Название клиента', system: 'name', type: 'text', missing: '0%', note: '—' },
      { source: 'Регион', system: 'region', type: 'category', missing: '0,4%', note: '8 значений' },
      { source: 'Дата регистрации', system: 'registered_at', type: 'date', missing: '0%', note: 'Формат ДД.ММ.ГГГГ' },
    ],
  },
];

export const JOURNAL: readonly { level: LogLevel; text: string }[] = [
  { level: 'info', text: 'В колонке «Сумма с НДС, руб» распознан формат валюты (₽)' },
  { level: 'warning', text: 'В колонке «Дата оплаты» 12 строк содержали нераспознанный текст; заменены на NULL. Исходные значения сохранены в raw' },
  { level: 'info', text: 'Колонка «Дата оплаты» приведена к формату ДД.ММ.ГГГГ (исходный d.m.y)' },
  { level: 'info', text: 'В колонке «Статус» найдено 4 значения: оплачен, доставлен, отменён, возврат' },
];

export const RELATION = {
  from: { label: 'Клиент', column: 'orders.customer_id' },
  to: { label: 'ID клиента', column: 'customers.id' },
  confidence: 99.8,
  note: 'Многие к одному · совпало 450 из 450 клиентов',
};
