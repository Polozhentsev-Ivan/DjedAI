import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { Check, ChevronDown, Plus } from 'lucide-react';
import { cn } from '@/lib/cn';
import { Icon } from './Icon';

export interface DatasetOption {
  id: string;
  meta: string;
  disabled?: boolean | undefined;
}

interface DatasetSelectProps {
  value: string | null;
  options: readonly DatasetOption[];
  onChange: (id: string) => void;
  onAdd: () => void;
  label?: string | undefined;
  showLabel?: boolean | undefined;
}

/** Active dataset picker in the top bar (shadcn DropdownMenu on Radix). */
export function DatasetSelect({ value, options, onChange, onAdd, label = 'Активный датасет', showLabel = true }: DatasetSelectProps) {
  return (
    <div className="relative flex items-center gap-2 text-xs">
      {showLabel && <span className="text-topbar-muted">{label}</span>}
      <DropdownMenu.Root>
        <DropdownMenu.Trigger
          aria-label={label}
          className="flex h-(--size-control-xs) items-center gap-2.5 rounded-sm border border-topbar-control-border bg-topbar-control pl-3 pr-2.5 font-mono text-sm text-topbar-foreground"
        >
          {value ?? 'не выбран'}
          <Icon icon={ChevronDown} size="sm" className="text-topbar-muted" />
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="start"
            sideOffset={6}
            className="z-40 flex w-(--popover-width) flex-col gap-0.5 rounded-md border border-border bg-popover p-1.5 font-sans text-popover-foreground shadow-(--shadow-popover)"
          >
            {options.map((option) => {
              const selected = option.id === value;
              return (
                <DropdownMenu.Item
                  key={option.id}
                  disabled={option.disabled ?? false}
                  onSelect={() => { onChange(option.id); }}
                  className={cn(
                    'flex cursor-pointer items-center gap-2 rounded-sm px-2.5 py-2 text-left outline-none data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50 data-[highlighted]:bg-accent',
                    selected ? 'bg-accent' : 'bg-transparent',
                  )}
                >
                  <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <span className="font-mono text-sm">{option.id}</span>
                    <span className="text-xs text-muted-foreground">{option.meta}</span>
                  </span>
                  {selected && <Icon icon={Check} size="sm" className="text-link" />}
                </DropdownMenu.Item>
              );
            })}
            <DropdownMenu.Separator className="my-1 h-px bg-border-subtle" />
            <DropdownMenu.Item
              onSelect={onAdd}
              className="flex cursor-pointer items-center gap-1.5 rounded-sm px-2.5 py-2 text-sm text-link outline-none data-[highlighted]:bg-accent"
            >
              <Icon icon={Plus} size="sm" />
              Добавить источник
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </div>
  );
}
