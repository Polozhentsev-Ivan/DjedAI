import { LoaderCircle } from 'lucide-react';
import { useEffect, useRef, useState, type SubmitEvent } from 'react';
import { Link, Navigate, useNavigate } from 'react-router';
import { login as signIn, register as signUp } from '@/api/auth';
import { ApiError } from '@/api/client';
import { useAuth } from '@/app/authContext';
import { Button } from '@/components/Button';
import { Icon } from '@/components/Icon';
import { Segmented } from '@/components/Segmented';
import { ErrorState, LoadingState } from '@/components/StateBlock';
import { cn } from '@/lib/cn';

type Tab = 'login' | 'register';

const TABS = [{ value: 'login', label: 'Вход' }, { value: 'register', label: 'Регистрация' }] as const;

const field =
  'box-border h-(--size-control) rounded-md border bg-card px-3.5 text-base tracking-body text-foreground outline-none focus-visible:outline-(--focus-ring)';

/** Registration explicitly logs in after account creation; the API never auto-creates a session. */
export function LoginPage({ tab }: { tab: Tab }) {
  const navigate = useNavigate();
  const auth = useAuth();
  const request = useRef<AbortController | null>(null);
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const register = tab === 'register';
  useEffect(() => () => { request.current?.abort(); }, []);

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (loading || request.current && !request.current.signal.aborted) return;
    setError(null);
    if (!login.trim() || !password) {
      setInvalid(true);
      return;
    }
    if (register && (login.length < 3 || login.length > 50 || password.length < 6 || password.length > 1024)) {
      setError('Имя пользователя — от 3 до 50 символов, пароль — от 6 до 1024.');
      return;
    }
    setInvalid(false);
    setLoading(true);
    const controller = new AbortController();
    request.current = controller;
    try {
      const credentials = { username: login, password };
      if (register) await signUp(credentials, controller.signal);
      await signIn(credentials, controller.signal);
      if (!controller.signal.aborted) {
        setPassword('');
        void navigate('/sources', { replace: true });
      }
    } catch (failure) {
      if (controller.signal.aborted) return;
      setPassword('');
      setError(failure instanceof ApiError && failure.status === 401 ? 'Неверный логин или пароль'
        : failure instanceof ApiError && failure.status === 409 ? 'Пользователь с таким именем уже существует'
        : failure instanceof ApiError && failure.status === 429 ? 'Слишком много попыток. Подождите и попробуйте снова.'
        : failure instanceof Error ? failure.message : 'Не удалось выполнить вход');
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setLoading(false);
    }
  };

  if (auth.pending) return <LoadingState title="Проверяем сессию…" />;
  if (auth.error) return <ErrorState title="Не удалось проверить сессию" text={auth.error} onAction={auth.retry} />;
  if (auth.user) return <Navigate to="/sources" replace />;

  return (
    <div className="box-border flex h-full overflow-auto bg-surface-subtle px-4 py-12 font-sans text-sm leading-normal tracking-sm text-foreground">
      <div className="m-auto flex w-full max-w-(--login-max) flex-col items-center gap-7">
        <div className="text-center">
          <div className="font-display text-2xl font-semibold leading-tight tracking-display">DjedAI</div>
          <div className="mt-2 text-base tracking-body text-muted-foreground">Локальная аналитическая платформа</div>
        </div>
        <form noValidate onSubmit={(event) => { void submit(event); }} className="box-border flex w-full flex-col gap-4 rounded-lg border border-border bg-card p-7">
          <Segmented
            label="Вход или регистрация"
            stretch
            options={TABS}
            value={tab}
            onChange={(next) => { if (!loading) void navigate(next === 'login' ? '/login' : '/register'); }}
            itemClassName="py-2"
          />
          {auth.notice && !error && <p role="status" className="text-muted-foreground">{auth.notice === 'expired' ? 'Сессия истекла или была отозвана. Войдите снова.' : 'Доступ запрещён. Войдите снова.'}</p>}
          {error && (
            <ErrorState
              size="inline"
              actionLabel=""
              title={error}
              text={register ? 'Выберите другое имя или войдите в существующий аккаунт.' : 'Проверьте раскладку и попробуйте снова.'}
            />
          )}
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login" className="text-xs font-semibold text-secondary-foreground">Имя пользователя</label>
            <input
              id="login"
              name="login"
              autoComplete="username"
              value={login}
              onChange={(e) => { setLogin(e.target.value); }}
              placeholder="ivan"
              maxLength={50}
              disabled={loading}
              aria-invalid={invalid}
              aria-describedby={invalid ? 'login-error' : undefined}
              className={cn(field, invalid ? 'border-destructive' : 'border-input')}
            />
            {invalid && <span id="login-error" className="text-xs text-destructive">Введите имя пользователя</span>}
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="password" className="text-xs font-semibold text-secondary-foreground">Пароль</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete={register ? 'new-password' : 'current-password'}
              value={password}
              onChange={(e) => { setPassword(e.target.value); }}
              placeholder="Не менее 6 символов"
              maxLength={1024}
              disabled={loading}
              aria-invalid={invalid}
              aria-describedby={invalid ? 'password-error' : undefined}
              className={cn(field, 'tracking-normal', invalid ? 'border-destructive' : 'border-input')}
            />
            {invalid && <span id="password-error" className="text-xs text-destructive">Введите пароль</span>}
          </div>
          <Button type="submit" disabled={loading} className="h-(--size-control) gap-2.5 text-base tracking-body disabled:opacity-70">
            {loading && <Icon icon={LoaderCircle} size="sm" spin />}
            <span>{loading ? (register ? 'Создаём аккаунт…' : 'Входим…') : register ? 'Зарегистрироваться' : 'Войти в систему'}</span>
          </Button>
          <div className="text-center text-muted-foreground">
            {register ? 'Уже есть аккаунт?' : 'Нет аккаунта?'}{' '}
            <Link to={register ? '/login' : '/register'} onClick={(event) => { if (loading) event.preventDefault(); }}>{register ? 'Войти' : 'Зарегистрироваться'}</Link>
          </div>
        </form>
        <div className="text-center text-xs text-muted-foreground">Данные хранятся локально. Внешней модели передаются каталог и результаты вычислений.</div>
      </div>
    </div>
  );
}
