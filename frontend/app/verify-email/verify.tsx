"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, XCircle } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { qk } from "@/hooks/queries";
import { errorMessage, post } from "@/lib/api";

export function VerifyEmail() {
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const token = params.get("token");
  const [state, setState] = useState<{ ok: boolean; message: string } | null>(null);
  const sent = useRef(false);

  useEffect(() => {
    if (!token || sent.current) return;
    sent.current = true; // tokens are single-use: never submit twice (React strict mode)
    post<{ message: string }>("/api/auth/verify-email", { token })
      .then((r) => {
        setState({ ok: true, message: r.message });
        void queryClient.invalidateQueries({ queryKey: qk.me });
      })
      .catch((e) => setState({ ok: false, message: errorMessage(e) }));
  }, [token, queryClient]);

  if (!token) return <p className="text-sm text-muted-foreground">This verification link is incomplete.</p>;
  if (!state) return <p className="text-sm text-muted-foreground">One moment…</p>;
  return (
    <div className="space-y-5">
      <div className="flex items-start gap-3">
        {state.ok ? <CheckCircle2 className="mt-0.5 size-5 text-emerald-600" /> : <XCircle className="mt-0.5 size-5 text-destructive" />}
        <p>{state.message}</p>
      </div>
      <Button asChild className="w-full">
        <Link href="/dashboard">Go to dashboard</Link>
      </Button>
    </div>
  );
}
