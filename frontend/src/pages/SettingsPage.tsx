import { useState, type SubmitEvent } from 'react';
import type { Schemas } from '@/api/client';
import { useCheckModelSettings, useModelSettings, useUpdateModelSettings } from '@/api/queries';
import { PageContainer, PageTitle } from '@/app/PageContainer';
import { Button } from '@/components/Button';
import { ErrorState, LoadingState } from '@/components/StateBlock';
import { cn } from '@/lib/cn';

const card = 'overflow-hidden rounded-lg border border-border bg-card';
const label = 'text-xs font-semibold text-secondary-foreground';
const hint = 'text-xs text-muted-foreground';
const input = 'box-border h-(--size-control) rounded-md border border-input bg-card px-3.5 font-mono text-sm text-foreground outline-none focus-visible:outline-(--focus-ring)';

type Update = Schemas['ModelSettingsUpdate'];
interface Notice { ok: boolean; text: string }

/** Model, API address and key (#209); the key and address only when the server allows it. */
export function SettingsPage() {
  const settings = useModelSettings();
  const update = useUpdateModelSettings();
  const check = useCheckModelSettings();
  // Only the outcome is kept: the request may carry the key, so each mutation is reset once
  // it settles and the key never stays in the cache.
  const [notice, setNotice] = useState<Notice | null>(null);

  const save = (body: Update) => {
    setNotice(null);
    update.mutate(body, {
      onSuccess: () => { setNotice({ ok: true, text: 'Сохранено. Новые настройки действуют со следующего вопроса.' }); },
      onError: (error) => { setNotice({ ok: false, text: error.message }); },
      onSettled: () => { update.reset(); },
    });
  };

  const probe = (body: Update) => {
    setNotice(null);
    check.mutate(body, {
      onSuccess: (result) => { setNotice({ ok: result.ok, text: result.message }); },
      onError: (error) => { setNotice({ ok: false, text: error.message }); },
      onSettled: () => { check.reset(); },
    });
  };

  return (
    <PageContainer>
      <div className="flex flex-col gap-6 text-sm leading-normal tracking-sm">
        <div>
          <PageTitle>Настройки модели</PageTitle>
          <div className="mt-1.5 text-muted-foreground">Модель, которая отвечает в чате и описывает данные. Изменения действуют со следующего вопроса.</div>
        </div>
        <section className={card}>
          {settings.isPending && <LoadingState size="compact" title="Загружаем настройки…" />}
          {settings.isError && (
            <ErrorState size="compact" title="Не удалось получить настройки модели" text={settings.error.message} onAction={() => { void settings.refetch(); }} />
          )}
          {/* Saved values remount the form, so typed values never outlive a save. */}
          {settings.isSuccess && (
            <ModelForm
              key={JSON.stringify(settings.data)}
              settings={settings.data}
              notice={notice}
              saving={update.isPending}
              checking={check.isPending}
              onEdit={() => { setNotice(null); }}
              onSave={save}
              onCheck={probe}
            />
          )}
        </section>
      </div>
    </PageContainer>
  );
}

interface ModelFormProps {
  settings: Schemas['ModelSettingsRead'];
  notice: Notice | null;
  saving: boolean;
  checking: boolean;
  onEdit: () => void;
  onSave: (body: Update) => void;
  onCheck: (body: Update) => void;
}

function ModelForm({ settings, notice, saving, checking, onEdit, onSave, onCheck }: ModelFormProps) {
  const editable = settings.editable_connection;
  const [model, setModel] = useState(settings.model_name ?? '');
  const [baseUrl, setBaseUrl] = useState(settings.base_url ?? '');
  const [apiKey, setApiKey] = useState('');
  const [replacingKey, setReplacingKey] = useState(!settings.api_key_from_ui);

  // Only what the user changed is sent; the server refuses the key and address when locked.
  const changes = (): Update => {
    const body: Update = {};
    if (model.trim() !== (settings.model_name ?? '')) body.model_name = model.trim();
    if (editable && baseUrl.trim() !== (settings.base_url ?? '')) body.base_url = baseUrl.trim();
    if (editable && apiKey !== '') body.api_key = apiKey;
    return body;
  };

  const edit = (set: (value: string) => void) => (value: string) => {
    set(value);
    onEdit();
  };

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSave(changes());
  };

  return (
    <form className="flex max-w-(--chat-max) flex-col gap-5 p-5" onSubmit={submit}>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="model-name" className={label}>Модель</label>
        {editable ? (
          <>
            <input
              id="model-name"
              list="allowed-models"
              autoComplete="off"
              spellCheck={false}
              required
              value={model}
              onChange={(e) => { edit(setModel)(e.target.value); }}
              className={input}
            />
            <datalist id="allowed-models">
              {settings.allowed_models.map((name) => <option key={name} value={name} />)}
            </datalist>
            <span className={hint}>Выберите из списка или введите имя модели, которое принимает адрес API.</span>
          </>
        ) : (
          <>
            <select id="model-name" value={model} onChange={(e) => { edit(setModel)(e.target.value); }} className={input}>
              {settings.allowed_models.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
            <span className={hint}>Список моделей, ключ и адрес API задаёт администратор сервера.</span>
          </>
        )}
      </div>

      {editable && (
        <>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="base-url" className={label}>Адрес API</label>
            <input
              id="base-url"
              type="url"
              autoComplete="off"
              spellCheck={false}
              placeholder="http://localhost:8000/v1"
              value={baseUrl}
              onChange={(e) => { edit(setBaseUrl)(e.target.value); }}
              className={input}
            />
            <span className={hint}>OpenAI-совместимый endpoint. Вопросы и данные уходят на этот адрес.</span>
          </div>
          <div className="flex flex-col gap-1.5">
            {replacingKey ? (
              <>
                <label htmlFor="api-key" className={label}>Ключ API</label>
                <input
                  id="api-key"
                  type="password"
                  autoComplete="new-password"
                  spellCheck={false}
                  value={apiKey}
                  onChange={(e) => { edit(setApiKey)(e.target.value); }}
                  placeholder={settings.api_key_set ? 'Ключ задан в .env; введите новый, чтобы заменить' : ''}
                  className={input}
                />
                <span className={hint}>Ключ хранится на сервере в зашифрованном виде и обратно в браузер не передаётся.</span>
              </>
            ) : (
              <>
                <span className={label}>Ключ API</span>
                <div className="flex flex-wrap items-center gap-2.5">
                  <span>Ключ задан</span>
                  <Button variant="outline" className="px-4 py-1.5" onClick={() => { setReplacingKey(true); }}>Заменить</Button>
                  <Button variant="danger-outline" className="px-4 py-1.5" disabled={saving} onClick={() => { onSave({ api_key: '' }); }}>
                    Удалить
                  </Button>
                </div>
              </>
            )}
          </div>
        </>
      )}

      {!settings.matches_env && (
        <div role="note" className="rounded-md border border-border bg-surface-subtle px-3.5 py-2.5 text-secondary-foreground">
          Для этой модели и адреса reasoning, оценка стоимости и передача значений ячеек модели выключены: администратор подтвердил их только для модели из .env.
        </div>
      )}

      {checking && <span role="status" className="text-muted-foreground">Проверяем подключение…</span>}
      {notice && (
        <span role={notice.ok ? 'status' : 'alert'} className={cn(notice.ok ? 'text-muted-foreground' : 'text-destructive')}>{notice.text}</span>
      )}

      <div className="flex flex-wrap gap-2.5">
        <Button type="submit" className="px-5 py-2" disabled={saving}>Сохранить</Button>
        <Button variant="outline" className="px-5 py-2" disabled={checking} onClick={() => { onCheck(changes()); }}>Проверить</Button>
      </div>
    </form>
  );
}
