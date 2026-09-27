import { AlertDialog } from "radix-ui";
import { Button } from "./Button";

/** Body of a destructive confirmation (use inside `AlertDialog.Root` next to its trigger). */
export function ConfirmDialog({
  title,
  body,
  confirm,
  cancel = "Cancel",
  onConfirm,
}: {
  title: string;
  body: string;
  confirm: string;
  cancel?: string;
  onConfirm: () => void;
}) {
  return (
    <AlertDialog.Portal>
      <AlertDialog.Overlay className="fixed inset-0 z-50 bg-black/25 backdrop-blur-[2px]" />
      <AlertDialog.Content className="glass fixed top-1/2 left-1/2 z-50 w-[min(92vw,380px)] -translate-x-1/2 -translate-y-1/2 animate-rise rounded-3xl p-6">
        <AlertDialog.Title className="font-semibold text-[16px] text-ink">
          {title}
        </AlertDialog.Title>
        <AlertDialog.Description className="mt-2 text-[13px] text-ink-2 leading-relaxed">
          {body}
        </AlertDialog.Description>
        <div className="mt-6 flex justify-end gap-2">
          <AlertDialog.Cancel asChild>
            <Button>{cancel}</Button>
          </AlertDialog.Cancel>
          <AlertDialog.Action asChild>
            <Button variant="danger" onClick={onConfirm}>
              {confirm}
            </Button>
          </AlertDialog.Action>
        </div>
      </AlertDialog.Content>
    </AlertDialog.Portal>
  );
}
