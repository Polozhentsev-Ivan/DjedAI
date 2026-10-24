interface SuggestionChipsProps {
  items: readonly string[];
  onSelect: (text: string) => void;
  disabled?: boolean | undefined;
}

export function SuggestionChips({ items, onSelect, disabled = false }: SuggestionChipsProps) {
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((item) => (
        <button
          key={item}
          type="button"
          disabled={disabled}
          onClick={() => { onSelect(item); }}
          className="press rounded-full border border-border bg-card px-3.5 py-1.5 text-sm tracking-sm text-foreground hover:border-link hover:text-link disabled:pointer-events-none disabled:opacity-60"
        >
          {item}
        </button>
      ))}
    </div>
  );
}
