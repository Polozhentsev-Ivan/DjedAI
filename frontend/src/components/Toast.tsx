interface ToastProps {
  message: string;
  actionLabel?: string | undefined;
  onAction?: (() => void) | undefined;
}

/** Dark notice above the chat input, e.g. after «+ В дашборд». */
export function Toast({ message, actionLabel, onAction }: ToastProps) {
  return (
    <div
      role="status"
      className="absolute bottom-[calc(var(--size-control)+--spacing(12))] left-1/2 z-50 flex -translate-x-1/2 items-center gap-4 whitespace-nowrap rounded-md bg-toast px-4 py-3 text-sm text-toast-foreground"
    >
      <span>{message}</span>
      {actionLabel && (
        <button type="button" onClick={onAction} className="text-toast-link hover:underline">
          {actionLabel}
        </button>
      )}
    </div>
  );
}
