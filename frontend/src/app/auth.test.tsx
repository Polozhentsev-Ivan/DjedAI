import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw/http';
import { request } from '@/api/client';
import { sessionSnapshot, setAccessToken } from '@/api/session';
import { createDb } from '@/mocks/db';
import { renderRoute } from '@/test/renderRoute';
import { server } from '@/test/server';

const api = (path: string) => `http://localhost:3000/api${path}`;
const profile = (name: string) => ({ user_id: `u_${name}`, username: name, is_active: true, created_at: '2026-10-22T00:00:00Z' });

async function credentials(name: string, password = 'secret-123') {
  await userEvent.type(screen.getByLabelText('Имя пользователя'), name);
  await userEvent.type(screen.getByLabelText('Пароль'), password);
}

it('protects a direct resource URL without requesting personal data', async () => {
  let reads = 0;
  server.use(http.get(api('/datasets'), () => { reads += 1; return HttpResponse.json([]); }));
  const { router } = await renderRoute('/catalog/old-user-dataset', { authenticated: false });
  await waitFor(() => { expect(router.state.location.pathname).toBe('/login'); });
  expect(screen.getByRole('button', { name: 'Войти в систему' })).toBeInTheDocument();
  expect(reads).toBe(0);
});

it('registers, explicitly logs in, and displays the confirmed current username', async () => {
  await renderRoute('/register');
  await credentials('Alice');
  await userEvent.click(screen.getByRole('button', { name: 'Зарегистрироваться' }));
  expect(await screen.findByText('Alice', { exact: true })).toBeInTheDocument();
  expect(sessionSnapshot().accessToken).toBeTruthy();
  expect(screen.queryByText('Локальный режим CP1')).not.toBeInTheDocument();
});

it('waits for a restored session check before loading resources and retries network failures', async () => {
  let reads = 0;
  server.use(
    http.get(api('/auth/me'), () => HttpResponse.error(), { once: true }),
    http.get(api('/datasets'), () => { reads += 1; return HttpResponse.json([]); }),
  );
  await renderRoute('/sources');
  expect(await screen.findByRole('alert')).toHaveTextContent('Не удалось проверить сессию');
  expect(reads).toBe(0);
  expect(sessionSnapshot().accessToken).toBe('test-session');
  await userEvent.click(screen.getByRole('button', { name: 'Повторить' }));
  expect(await screen.findByText('ivan', { exact: true })).toBeInTheDocument();
  await waitFor(() => { expect(reads).toBe(1); });
});

it('redirects an expired restored session without mounting a private workspace', async () => {
  setAccessToken('expired-session');
  const { router } = await renderRoute('/chat/private-chat', { authenticated: false });
  await waitFor(() => { expect(router.state.location.pathname).toBe('/login'); });
  expect(await screen.findByRole('status')).toHaveTextContent('Сессия истекла');
  expect(sessionSnapshot().accessToken).toBeNull();
});

it.each([401, 403])('removes private UI and aborts the old session after HTTP %i', async (status) => {
  const { router, client } = await renderRoute('/sources');
  await screen.findByText('ivan', { exact: true });
  const previous = sessionSnapshot();
  client.setQueryData(['private-result'], { rows: [['secret']] });
  server.use(http.get(api('/results/r_denied'), () => HttpResponse.json({ error: { code: status === 401 ? 'unauthorized' : 'forbidden', message: 'Denied' } }, { status })));
  await act(async () => { await expect(request('/results/r_denied')).rejects.toMatchObject({ status }); });
  await waitFor(() => { expect(router.state.location.pathname).toBe('/login'); });
  expect(previous.signal.aborted).toBe(true);
  expect(client.getQueryData(['private-result'])).toBeUndefined();
  expect(screen.queryByRole('heading', { name: 'Источники данных' })).not.toBeInTheDocument();
});

it('clears resources, active dataset and late responses before a second account signs in', async () => {
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const oldDataset = { ...createDb().datasets[0]!, name: 'private-alpha' };
  server.use(
    http.post(api('/auth/login'), async ({ request: incoming }) => {
      const input = await incoming.json() as { username: string };
      return HttpResponse.json({ access_token: input.username, token_type: 'bearer', expires_in: 86400 });
    }),
    http.get(api('/auth/me'), ({ request: incoming }) => HttpResponse.json(profile(incoming.headers.get('Authorization')?.endsWith('alpha') ? 'alpha' : 'beta'))),
    http.get(api('/datasets'), ({ request: incoming }) => HttpResponse.json(incoming.headers.get('Authorization')?.endsWith('alpha') ? [oldDataset] : [])),
    http.get(api('/results/late'), async () => { await pending; return HttpResponse.json({ private: 'alpha' }); }),
  );
  const { client, router } = await renderRoute('/login');
  await credentials('alpha');
  await userEvent.click(screen.getByRole('button', { name: 'Войти в систему' }));
  await screen.findByRole('table', { name: 'Ваши датасеты' });
  expect(screen.getAllByText('private-alpha').length).toBeGreaterThan(0);
  client.setQueryData(['dashboard', 'old'], { private: 'alpha-dashboard' });
  window.localStorage.setItem('djedai.activeDataset', oldDataset.dataset_id);
  const previous = sessionSnapshot();
  const late = expect(request('/results/late')).rejects.toMatchObject({ name: 'AbortError' });
  await userEvent.click(screen.getByRole('button', { name: 'Выйти' }));
  await waitFor(() => { expect(router.state.location.pathname).toBe('/login'); });
  expect(previous.signal.aborted).toBe(true);
  expect(client.getQueryData(['dashboard', 'old'])).toBeUndefined();
  expect(window.localStorage.getItem('djedai.activeDataset')).toBeNull();
  await credentials('beta');
  await userEvent.click(screen.getByRole('button', { name: 'Войти в систему' }));
  expect(await screen.findByText('beta', { exact: true })).toBeInTheDocument();
  await act(async () => { release?.(); await late; });
  expect(screen.queryByText('private-alpha')).not.toBeInTheDocument();
  expect(await screen.findByText('У вас пока нет загруженных данных')).toBeInTheDocument();
});

it('submits a login once while pending, shows server errors and clears the password', async () => {
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let calls = 0;
  server.use(http.post(api('/auth/login'), async () => {
    calls += 1;
    await pending;
    return HttpResponse.json({ error: { code: 'busy', message: 'Сервер недоступен' } }, { status: 503 });
  }));
  await renderRoute('/login');
  await credentials('ivan');
  const form = screen.getByRole('button', { name: 'Войти в систему' }).closest('form')!;
  fireEvent.submit(form);
  fireEvent.submit(form);
  expect(await screen.findByRole('button', { name: 'Входим…' })).toBeDisabled();
  await waitFor(() => { expect(calls).toBe(1); });
  await act(async () => { release?.(); await pending; });
  expect(await screen.findByRole('alert')).toHaveTextContent('Сервер недоступен');
  expect(screen.getByLabelText('Пароль')).toHaveValue('');
});
