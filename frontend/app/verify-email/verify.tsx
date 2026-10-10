"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, MailCheck, XCircle } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { qk } from "@/hooks/queries";
import { errorMessage, post } from "@/lib/api";
import type { VerifyResult } from "@/types/api";

type State =
  | { kind: "verified" | "already_verified"; message: string }
  | { kind: "error"; message: string };

/** Signed-out friendly "send me a new link" form (the response never reveals accounts). */
function ResendForm() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (sent) return <p className="text-sm text-muted-foreground" data-testid="resend-done">{sent}</p>;
  return (
    <form
      className="space-y-2"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          const r = await post<{ message: string }>("/api/auth/resend-verification-public", { email });
          setSent(r.message);
        } catch (err) {
          setError(errorMessage(err));
        } finally {
          setBusy(false);
        }
      }}
    >
      <Label htmlFor="resend-email">Send a new verification link</Label>
      <div className="flex gap-2">
        <Input id="resend-email" type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
        <Button type="submit" disabled={busy}>
          Send
        </Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
    </form>
  );
}

export function VerifyEmail() {
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const token = params.get("token");
  const [state, setState] = useState<State | null>(null);
  const sent = useRef(false);

  useEffect(() => {
    if (!token || sent.current) return;
    sent.current = true; // tokens are single-use: never submit twice (React strict mode)
    post<VerifyResult>("/api/auth/verify-email", { token })
      .then((r) => {
        setState({ kind: r.outcome === "already_verified" ? "already_verified" : "verified", message: r.message });
        void queryClient.invalidateQueries({ queryKey: qk.me });
        void queryClient.invalidateQueries({ queryKey: qk.verification });
      })
      .catch((e) => setState({ kind: "error", message: errorMessage(e) }));
  }, [token, queryClient]);

  if (!token)
    return (
      <div className="space-y-5" data-testid="verify-result" data-outcome="missing">
        <p className="text-sm text-muted-foreground">This verification link is incomplete. Copy the whole link from the email, or request a new one.</p>
        <ResendForm />
      </div>
    );
  if (!state) return <p className="text-sm text-muted-foreground">Checking your link…</p>;
  const ok = state.kind !== "error";
  return (
    <div className="space-y-5" data-testid="verify-result" data-outcome={state.kind}>
      <div className="flex items-start gap-3">
        {state.kind === "verified" && <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-emerald-600" />}
        {state.kind === "already_verified" && <MailCheck className="mt-0.5 size-5 shrink-0 text-emerald-600" />}
        {state.kind === "error" && <XCircle className="mt-0.5 size-5 shrink-0 text-destructive" />}
        <p>{state.message}</p>
      </div>
      {!ok && <ResendForm />}
      <Button asChild className="w-full" variant={ok ? "default" : "outline"}>
        <Link href="/dashboard">Go to dashboard</Link>
      </Button>
    </div>
  );
}
