import { LoaderCircle } from 'lucide-react';
import { useState, type SubmitEvent } from 'react';
import { Link, useNavigate } from 'react-router';
import { useFixtureState } from '@/app/useFixtureState';
import { Button } from '@/components/Button';
import { Icon } from '@/components/Icon';
import { Segmented } from '@/components/Segmented';
import { ErrorState } from '@/components/StateBlock';
import { cn } from '@/lib/cn';

type Tab = 'login' | 'register';

const TABS = [{ value: 'login', label: 'Вход' }, { value: 'register', label: 'Регистрация' }] as const;

const field =
  'box-border h-(--size-control) rounded-md border bg-card px-3.5 text-base tracking-body text-foreground outline-none focus-visible:outline-(--focus-ring)';

/** Login and registration. Auth itself belongs to its own task; here the form leads to the app. */
export function LoginPage({ tab }: { tab: Tab }) {
  const navigate = useNavigate();
  const { state, set } = useFixtureState();
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const register = tab === 'register';
  const loading = state === 'loading';
  const invalid = state === 'empty';

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (loading) return;
    if (!login.trim() || !password) {
      set('empty');
      return;
    }
    void navigate('/sources');
  };

  return (
    <div className="box-border flex h-full overflow-auto bg-surface-subtle px-4 py-12 font-sans text-sm leading-normal tracking-sm text-foreground">
      <div className="m-auto flex w-full max-w-(--login-max) flex-col items-center gap-7">
        <div className="text-center">
          <div className="font-display text-2xl font-semibold leading-tight tracking-display">DjedAI</div>
          <div className="mt-2 text-base tracking-body text-muted-foreground">Локальная аналитическая платформа</div>
        </div>
        <form noValidate onSubmit={submit} className="box-border flex w-full flex-col gap-4 rounded-lg border border-border bg-card p-7">
          <Segmented
            label="Вход или регистрация"
            stretch
            options={TABS}
            value={tab}
            onChange={(next) => { void navigate(next === 'login' ? '/login' : '/register'); }}
            itemClassName="py-2"
          />
          {state === 'error' && (
            <ErrorState
              size="inline"
              actionLabel=""
              title={register ? 'Пользователь с таким именем уже существует' : 'Неверный логин или пароль'}
              text={register ? 'Выберите другое имя или войдите в существующий аккаунт.' : 'Проверьте раскладку и попробуйте снова.'}
            />
          )}
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login" className="text-xs font-semibold text-secondary-foreground">Имя пользователя / Email</label>
            <input
              id="login"
              name="login"
              autoComplete="username"
              value={login}
              onChange={(e) => { setLogin(e.target.value); }}
              placeholder="ivan или ivan@company.ru"
              aria-invalid={invalid}
              aria-describedby={invalid ? 'login-error' : undefined}
              className={cn(field, invalid ? 'border-destructive' : 'border-input')}
            />
            {invalid && <span id="login-error" className="text-xs text-destructive">Введите имя пользователя или email</span>}
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
              placeholder="Не менее 8 символов"
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
            <Link to={register ? '/login' : '/register'}>{register ? 'Войти' : 'Зарегистрироваться'}</Link>
          </div>
        </form>
        <div className="text-center text-xs text-muted-foreground">Данные обрабатываются локально и не покидают ваш сервер</div>
      </div>
    </div>
  );
}
