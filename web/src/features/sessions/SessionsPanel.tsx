import { useMutation, useQuery } from "@tanstack/react-query";
import { Laptop, LogOut, MonitorSmartphone, RefreshCw, Smartphone } from "lucide-react";
import { AlertDialog } from "radix-ui";
import { listSessions, revokeSession } from "@/api/endpoints";
import { describeError } from "@/api/http";
import type { LiveSession } from "@/api/schemas";
import { queryClient } from "@/app/queryClient";
import { SESSIONS_KEY } from "@/app/runtime";
import { signOutHere } from "@/features/auth/signOut";
import { notify } from "@/features/shell/notices";
import { PanelFrame } from "@/features/shell/PanelFrame";
import type { PanelProps } from "@/features/shell/panels";
import { formatAge } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { useLive } from "@/state/live";
import { Badge } from "@/ui/Badge";
import { Button } from "@/ui/Button";
import { ConfirmDialog } from "@/ui/ConfirmDialog";
import { Empty } from "@/ui/Empty";
import { SectionLabel } from "@/ui/SectionLabel";
import { Spinner } from "@/ui/Spinner";
import { connectedAt, groupSessions } from "./model";

function isPhone(session: LiveSession): boolean {
  return /ios|android|iphone|mobile/i.test(`${session.label} ${session.agent ?? ""}`);
}

export function SessionsPanel({ onClose, titleId }: PanelProps) {
  const mySession = useLive((s) => s.sessionId);
  const sessions = useQuery({
    queryKey: SESSIONS_KEY,
    queryFn: ({ signal }) => listSessions(signal),
    staleTime: 5_000,
  });
  const now = useNow(10_000);

  const { here, elsewhere } = groupSessions(sessions.data ?? [], mySession);

  return (
    <PanelFrame
      title="Sessions"
      titleId={titleId}
      subtitle="Everywhere you are signed in, live"
      onClose={onClose}
      bodyClassName="px-4 pb-4"
    >
      {sessions.isPending && (
        <div className="flex justify-center py-10 text-muted">
          <Spinner />
        </div>
      )}
      {sessions.isError && (
        <Empty
          icon={<RefreshCw className="size-5" />}
          title="Sessions didn't load"
          action={
            <Button size="sm" onClick={() => void sessions.refetch()}>
              Try again
            </Button>
          }
        >
          {describeError(sessions.error)}
        </Empty>
      )}
      {sessions.data && (
        <>
          <SectionLabel>This device</SectionLabel>
          <ul className="space-y-1.5">
            {here.length > 0 ? (
              here.map((row) => (
                <SessionRow
                  key={row.session.sid}
                  session={row.session}
                  relation={
                    row.thisTab === true ? "thisTab" : row.thisTab === false ? "otherTab" : "here"
                  }
                  now={now}
                />
              ))
            ) : (
              <li className="rounded-2xl bg-surface-2/70 px-3.5 py-3 text-[12.5px] text-muted ring-1 ring-line ring-inset">
                Connecting…
              </li>
            )}
          </ul>
          {here.length > 1 && (
            <p className="mt-2 px-1 text-[11.5px] text-muted leading-relaxed">
              Tabs of this browser share one sign-in: signing out below ends all of them.
            </p>
          )}
          <SectionLabel
            aside={elsewhere.length > 1 ? <RevokeAll sessions={elsewhere} /> : undefined}
          >
            Other sessions
          </SectionLabel>
          {elsewhere.length === 0 ? (
            <div className="flex items-center gap-3 rounded-2xl bg-surface-2/70 px-3.5 py-3.5 text-[12.5px] text-muted ring-1 ring-line ring-inset">
              <MonitorSmartphone className="size-4 shrink-0" />
              No other sessions. Sign in elsewhere and it appears here instantly.
            </div>
          ) : (
            <ul className="space-y-1.5">
              {elsewhere.map((s) => (
                <SessionRow key={s.sid} session={s} relation="elsewhere" now={now} />
              ))}
            </ul>
          )}
          <div className="mt-6 border-line border-t pt-4">
            <Button
              variant="ghost"
              className="w-full text-critical hover:bg-critical/10 hover:text-critical"
              icon={<LogOut className="size-4" />}
              onClick={() => void signOutHere()}
            >
              Sign out on this device
            </Button>
          </div>
        </>
      )}
    </PanelFrame>
  );
}

/**
 * `thisTab`: this very socket. `otherTab`: another socket of this sign-in. `here`: this sign-in,
 * before this tab's socket said which one it is. `elsewhere`: another sign-in, which can be signed
 * out remotely.
 */
type Relation = "thisTab" | "otherTab" | "here" | "elsewhere";

function SessionRow({
  session,
  relation,
  now,
}: {
  session: LiveSession;
  relation: Relation;
  now: number;
}) {
  const since = connectedAt(session);
  const revoke = useMutation({
    mutationFn: () => revokeSession(session.sid),
    onSuccess: () => {
      queryClient.setQueryData<LiveSession[]>(SESSIONS_KEY, (list) =>
        list?.filter((s) => s.sid !== session.sid),
      );
      notify({
        tone: "success",
        title: "Signed out",
        body: `${session.label || "That session"} was signed out.`,
      });
    },
    onError: (error) =>
      notify({
        tone: "error",
        title: "Couldn't sign out that session",
        body: describeError(error),
      }),
  });

  return (
    <li className="flex items-center gap-3 rounded-2xl bg-surface-2/70 px-3.5 py-3 ring-1 ring-line ring-inset">
      <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-surface-solid text-ink-2 ring-1 ring-line ring-inset">
        {isPhone(session) ? <Smartphone className="size-4" /> : <Laptop className="size-4" />}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate font-medium text-[13px] text-ink">
            {session.label || "Browser"}
          </span>
          {relation === "thisTab" && <Badge tone="accent">This tab</Badge>}
          {relation === "otherTab" && <Badge>Other tab</Badge>}
        </span>
        <span className="mt-0.5 block truncate text-[11.5px] text-muted">
          {[
            session.ip,
            since ? `since ${formatAge(now - since).replace(" ago", "")}` : null,
            session.replica,
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
      </span>
      {relation === "elsewhere" && (
        <AlertDialog.Root>
          <AlertDialog.Trigger asChild>
            <Button size="sm" disabled={revoke.isPending}>
              {revoke.isPending ? <Spinner className="size-3" /> : "Sign out"}
            </Button>
          </AlertDialog.Trigger>
          <ConfirmDialog
            title={`Sign out ${session.label || "this session"}?`}
            body="That browser is disconnected immediately and has to sign in again."
            confirm="Sign out"
            onConfirm={() => revoke.mutate()}
          />
        </AlertDialog.Root>
      )}
    </li>
  );
}

function RevokeAll({ sessions }: { sessions: LiveSession[] }) {
  const revokeAll = useMutation({
    mutationFn: async () => {
      const results = await Promise.allSettled(sessions.map((s) => revokeSession(s.sid)));
      const failed = results.filter((r) => r.status === "rejected").length;
      if (failed > 0) throw new Error(`${failed} of ${sessions.length} could not be signed out`);
    },
    onSettled: () => void queryClient.invalidateQueries({ queryKey: SESSIONS_KEY }),
    onError: (error) =>
      notify({
        tone: "error",
        title: "Some sessions stayed signed in",
        body: describeError(error),
      }),
  });
  return (
    <AlertDialog.Root>
      <AlertDialog.Trigger asChild>
        <button type="button" className="font-medium text-[11.5px] text-critical hover:underline">
          Sign out all
        </button>
      </AlertDialog.Trigger>
      <ConfirmDialog
        title={`Sign out ${sessions.length} other sessions?`}
        body="Every other browser is disconnected immediately. This one stays signed in."
        confirm="Sign out all"
        onConfirm={() => revokeAll.mutate()}
      />
    </AlertDialog.Root>
  );
}
