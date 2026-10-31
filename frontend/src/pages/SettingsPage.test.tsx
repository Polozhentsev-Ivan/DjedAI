import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw/http';
import type { Schemas } from '@/api/client';
import { renderRoute } from '@/test/renderRoute';
import { server } from '@/test/server';

const api = (path: string) => `http://localhost:3000/api${path}`;

const serverSettings: Schemas['ModelSettingsRead'] = {
  base_url: 'https://api.deepinfra.com/v1/openai',
  model_name: 'Qwen/Qwen3.6-27B',
  allowed_models: ['Qwen/Qwen3.6-27B', 'Qwen/Qwen3-235B-A22B'],
  api_key_set: true,
  api_key_from_ui: false,
  editable_connection: false,
  matches_env: true,
};

describe('SettingsPage', () => {
  it('hides the key and address when the server does not allow them', async () => {
    const sent: Schemas['ModelSettingsUpdate'][] = [];
    server.use(
      http.get(api('/settings/model'), () => HttpResponse.json(serverSettings)),
      http.put(api('/settings/model'), async ({ request }) => {
        const body = (await request.json()) as Schemas['ModelSettingsUpdate'];
        sent.push(body);
        return HttpResponse.json({ ...serverSettings, model_name: body.model_name ?? null, matches_env: false });
      }),
    );
    await renderRoute('/settings');
    const model = await screen.findByRole('combobox', { name: 'Модель' });
    expect(screen.queryByLabelText('Адрес API')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Ключ API')).not.toBeInTheDocument();
    expect(screen.queryByText('Ключ API')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Заменить' })).not.toBeInTheDocument();

    await userEvent.selectOptions(model, 'Qwen/Qwen3-235B-A22B');
    await userEvent.click(screen.getByRole('button', { name: 'Сохранить' }));
    expect(await screen.findByText(/Сохранено/)).toBeInTheDocument();
    expect(sent).toEqual([{ model_name: 'Qwen/Qwen3-235B-A22B' }]);
    expect(screen.getByRole('note')).toHaveTextContent('reasoning');
  });

  it('sets the address and key locally and then only says the key is set', async () => {
    const sent: unknown[] = [];
    const record = ({ request }: { request: Request }) => {
      if (request.method === 'PUT') void request.clone().json().then((body) => sent.push(body));
    };
    server.events.on('request:start', record);
    const { client } = await renderRoute('/settings');
    const address = await screen.findByLabelText('Адрес API');
    await userEvent.clear(address);
    await userEvent.type(address, 'http://localhost:8000/v1');
    await userEvent.type(screen.getByLabelText('Ключ API'), 'sk-local-secret');
    await userEvent.click(screen.getByRole('button', { name: 'Сохранить' }));

    expect(await screen.findByText(/Сохранено/)).toBeInTheDocument();
    await waitFor(() => { expect(sent).toHaveLength(1); });
    expect(sent[0]).toEqual({ base_url: 'http://localhost:8000/v1', api_key: 'sk-local-secret' });
    expect(screen.getByText('Ключ задан')).toBeInTheDocument();
    expect(screen.queryByLabelText('Ключ API')).not.toBeInTheDocument();
    server.events.removeListener('request:start', record);
    // The key is gone from the page and from every cached mutation.
    expect(document.body.innerHTML).not.toContain('sk-local-secret');
    await waitFor(() => { expect(JSON.stringify(client.getMutationCache().getAll().map((m) => m.state))).not.toContain('sk-local-secret'); });

    await userEvent.click(screen.getByRole('button', { name: 'Заменить' }));
    expect(screen.getByLabelText('Ключ API')).toHaveValue('');
  });

  it('checks the connection and explains a rejected key', async () => {
    await renderRoute('/settings');
    await userEvent.type(await screen.findByLabelText('Ключ API'), 'sk-wrong');
    await userEvent.click(screen.getByRole('button', { name: 'Проверить' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Ключ API не принят');
    await userEvent.clear(screen.getByLabelText('Ключ API'));
    await userEvent.type(screen.getByLabelText('Ключ API'), 'sk-good');
    await userEvent.click(screen.getByRole('button', { name: 'Проверить' }));
    expect(await screen.findByText('Модель ответила')).toBeInTheDocument();
  });

  it('clears a replacement key even when public settings do not change', async () => {
    const stored = { ...serverSettings, editable_connection: true, api_key_from_ui: true };
    const sent: Schemas['ModelSettingsUpdate'][] = [];
    server.use(
      http.get(api('/settings/model'), () => HttpResponse.json(stored)),
      http.put(api('/settings/model'), async ({ request }) => {
        sent.push((await request.json()) as Schemas['ModelSettingsUpdate']);
        return HttpResponse.json(stored);
      }),
    );
    const { client } = await renderRoute('/settings');
    await userEvent.click(await screen.findByRole('button', { name: 'Заменить' }));
    await userEvent.type(screen.getByLabelText('Ключ API'), 'sk-replacement-secret');
    await userEvent.click(screen.getByRole('button', { name: 'Сохранить' }));
    expect(await screen.findByText(/Сохранено/)).toBeInTheDocument();
    expect(sent).toEqual([{ api_key: 'sk-replacement-secret' }]);
    expect(screen.queryByLabelText('Ключ API')).not.toBeInTheDocument();
    expect(screen.getByText('Ключ задан')).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain('sk-replacement-secret');
    await waitFor(() => { expect(JSON.stringify(client.getMutationCache().getAll().map((m) => m.state))).not.toContain('sk-replacement-secret'); });
    await userEvent.click(screen.getByRole('button', { name: 'Заменить' }));
    expect(screen.getByLabelText('Ключ API')).toHaveValue('');
  });
});
