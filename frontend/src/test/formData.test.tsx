import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw/http';
import { request } from '@/api/client';
import { server } from './server';

describe('browser forms at the Node fetch boundary', () => {
  it('submits a React form and reads its successful DOM controls', async () => {
    const submitted = vi.fn();
    render(
      <form onSubmit={(event) => {
        event.preventDefault();
        submitted(new FormData(event.currentTarget).get('login'));
      }}>
        <input name="login" defaultValue="ivan" />
        <button type="submit">Submit</button>
      </form>,
    );
    await userEvent.setup().click(screen.getByRole('button', { name: 'Submit' }));
    expect(submitted).toHaveBeenCalledExactlyOnceWith('ivan');
  });

  it('preserves multipart field names, filenames, MIME types and nonempty file bytes', async () => {
    server.use(http.post('/api/multipart-check', async ({ request: incoming }) => {
      const body = await incoming.formData();
      const files = body.getAll('files');
      return HttpResponse.json({
        label: body.get('label'),
        files: await Promise.all(files.map(async (file) => {
          if (typeof file === 'string') throw new Error('Expected an uploaded file');
          return { name: file.name, type: file.type, text: await file.text() };
        })),
      });
    }));
    const body = new FormData();
    body.append('label', 'CSV upload');
    body.append('files', new File(['name,amount\nТовар,42\n'], 'orders.csv', { type: 'text/csv' }));
    body.append('files', new File(['id\n7\n'], 'customers.csv', { type: 'text/csv' }));
    await expect(request('/multipart-check', { method: 'POST', form: body })).resolves.toEqual({
      label: 'CSV upload',
      files: [
        { name: 'orders.csv', type: 'text/csv', text: 'name,amount\nТовар,42\n' },
        { name: 'customers.csv', type: 'text/csv', text: 'id\n7\n' },
      ],
    });
  });

  it('sends DOM-constructed form data through fetch with its file contents intact', async () => {
    server.use(http.post('/api/dom-form', async ({ request: incoming }) => {
      const body = await incoming.formData();
      const file = body.get('files');
      if (file === null || typeof file === 'string') throw new Error('Expected an uploaded file');
      return HttpResponse.json({ label: body.get('label'), name: file.name, text: await file.text() });
    }));
    const form = document.createElement('form');
    const label = document.createElement('input');
    label.name = 'label';
    label.value = 'DOM form';
    form.append(label);
    const body = new FormData(form);
    body.append('files', new Blob(['id\n42\n'], { type: 'text/csv' }), 'dom.csv');
    await expect(request('/dom-form', { method: 'POST', form: body })).resolves.toEqual({
      label: 'DOM form', name: 'dom.csv', text: 'id\n42\n',
    });
  });
});
