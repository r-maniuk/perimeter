import { ArrowRight, Info } from "lucide-react";
import { m } from "motion/react";
import { type FormEvent, useId, useState } from "react";
import { signIn } from "@/api/endpoints";
import { describeError } from "@/api/http";
import { announceSession } from "@/app/sessionSync";
import { lastUsername, rememberUsername, useSession } from "@/state/session";
import { Button } from "@/ui/Button";
import { Logo } from "@/ui/Logo";
import { Spinner } from "@/ui/Spinner";

const USERNAME = /^[a-z0-9][a-z0-9_.-]{1,31}$/;

export function validateUsername(value: string): string | null {
  if (value.length === 0) return "Enter a username.";
  if (value.length < 2) return "Use at least 2 characters.";
  if (value.length > 32) return "Use at most 32 characters.";
  if (!/^[a-z0-9]/.test(value)) return "Start with a letter or a digit.";
  if (!USERNAME.test(value)) return "Use letters, digits, dots, dashes or underscores.";
  return null;
}

export function SignIn() {
  const notice = useSession((s) => s.notice);
  const [username, setUsername] = useState(lastUsername);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const inputId = useId();
  const hintId = useId();
  const errorId = useId();

  async function submit(event: FormEvent) {
    event.preventDefault();
    const value = username.trim().toLowerCase();
    const problem = validateUsername(value);
    setError(problem);
    if (problem) return;
    setPending(true);
    try {
      const session = await signIn(value);
      rememberUsername(session.user.username);
      useSession.getState().signedIn(session.user);
      // The other tabs of this browser send the new session cookie from now on: they follow.
      announceSession("signedIn");
    } catch (failure) {
      setError(describeError(failure));
      setPending(false);
    }
  }

  return (
    <m.div
      className="fixed inset-0 z-40 flex items-center justify-center p-4"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0, transition: { duration: 0.45, ease: [0.22, 1, 0.36, 1] } }}
    >
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-[radial-gradient(60%_55%_at_50%_45%,transparent_0%,var(--scrim)_70%,var(--bg)_130%)]"
      />
      <m.main
        className="glass relative w-[min(100%,400px)] rounded-[28px] px-8 pt-9 pb-8"
        initial={{ opacity: 0, y: 14, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: -8, scale: 0.97 }}
        transition={{ type: "spring", stiffness: 260, damping: 28 }}
      >
        <div className="flex items-center gap-2.5">
          <Logo className="size-8" animated />
          <span className="font-semibold text-[17px] text-ink tracking-[-0.01em]">Perimeter</span>
        </div>
        <h1 className="mt-7 font-semibold text-[26px] text-ink leading-tight tracking-[-0.025em]">
          Sign in
        </h1>
        <p className="mt-2 text-[14px] text-ink-2 leading-relaxed">
          Watch every device move in real time, and know the moment one crosses a line you drew.
        </p>

        {notice && (
          <div
            role="status"
            className="mt-5 flex gap-2.5 rounded-xl bg-accent-soft px-3.5 py-3 text-[13px] text-ink"
          >
            <Info className="mt-px size-4 shrink-0 text-accent" aria-hidden="true" />
            <span>{notice}</span>
          </div>
        )}

        <form className="mt-6" onSubmit={submit} noValidate>
          <label htmlFor={inputId} className="font-medium text-[13px] text-ink">
            Username
          </label>
          <input
            id={inputId}
            // biome-ignore lint/a11y/noAutofocus: the only thing to do on this screen is type a name.
            autoFocus
            value={username}
            onChange={(e) => {
              setUsername(e.target.value);
              if (error) setError(null);
            }}
            autoComplete="username"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            maxLength={32}
            placeholder="ada.lovelace"
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errorId : hintId}
            className="mt-2 h-11 w-full rounded-xl bg-surface-solid px-3.5 font-mono text-[15px] text-ink outline-none ring-1 ring-line-strong transition-shadow placeholder:text-muted/70 focus:ring-2 focus:ring-accent aria-[invalid]:ring-critical"
          />
          {error ? (
            <p id={errorId} role="alert" className="mt-2 text-[12.5px] text-critical">
              {error}
            </p>
          ) : (
            <p id={hintId} className="mt-2 text-[12.5px] text-muted">
              New names get a workspace instantly — no password.
            </p>
          )}
          <Button
            type="submit"
            variant="primary"
            size="lg"
            className="mt-5 w-full"
            disabled={pending}
            icon={pending ? <Spinner className="size-4" label="Signing in" /> : undefined}
          >
            {pending ? "Signing in" : "Continue"}
            {!pending && <ArrowRight className="size-4" aria-hidden="true" />}
          </Button>
        </form>
      </m.main>
    </m.div>
  );
}
