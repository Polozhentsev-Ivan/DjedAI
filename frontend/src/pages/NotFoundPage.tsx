import { useNavigate } from 'react-router';
import { PageContainer } from '@/app/PageContainer';
import { EmptyState } from '@/components/StateBlock';

export function NotFoundPage() {
  const navigate = useNavigate();
  return (
    <PageContainer>
      <div className="rounded-lg border border-border bg-card">
        <EmptyState
          title="Страница не найдена"
          text="Проверьте адрес или вернитесь к источникам данных."
          actionLabel="К источникам"
          onAction={() => { void navigate('/sources'); }}
        />
      </div>
    </PageContainer>
  );
}
