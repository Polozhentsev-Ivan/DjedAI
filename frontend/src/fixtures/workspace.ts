// Fixture data from docs/ui-design until the API (#92, #95) is connected.
import type { ChatItem } from '@/components/Sidebar';

export type DatasetStatus = 'ready' | 'error';

export interface DatasetSummary {
  id: string;
  source: string;
  tables: string;
  rows: string;
  status: DatasetStatus;
}

export const USER = 'ivan';

export const DATASETS: readonly DatasetSummary[] = [
  { id: 'orders_2026', source: 'CSV (2)', tables: '2', rows: '120 450', status: 'ready' },
  { id: 'retail_crm_prod', source: 'Postgres', tables: '5', rows: '1 420 000', status: 'ready' },
  { id: 'marketing_q3', source: 'CSV (1)', tables: '–', rows: '–', status: 'error' },
];

export const CHATS: readonly ChatItem[] = [
  { id: 'q1', title: 'Продажи Q1', meta: 'Сегодня, 14:05' },
  { id: 'reg', title: 'Регионы', meta: 'Вчера, 17:40' },
  { id: 'seg', title: 'Сегменты', meta: '12.10.2026' },
];
