"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorMessage, post } from "@/lib/api";

export function Unsubscribe() {
  const token = useSearchParams().get("token");
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function confirm() {
    setBusy(true);
    try {
      const r = await post<{ message: string }>("/api/notifications/unsubscribe", { token });
      setResult({ ok: true, message: r.message });
    } catch (e) {
      setResult({ ok: false, message: errorMessage(e) });
    } finally {
      setBusy(false);
    }
  }

  if (!token) return <p className="text-sm text-muted-foreground">This unsubscribe link is incomplete.</p>;
  if (result)
    return (
      <div className="space-y-4">
        <Alert variant={result.ok ? "success" : "danger"}>
          <AlertDescription>{result.message}</AlertDescription>
        </Alert>
        <Button asChild variant="outline" className="w-full">
          <Link href="/settings">Manage notification settings</Link>
        </Button>
      </div>
    );
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        You&apos;ll stop receiving email alerts. Your searches keep running and opportunities stay on your dashboard.
      </p>
      <Button onClick={confirm} disabled={busy} className="w-full" variant="destructive">
        {busy ? "Unsubscribing…" : "Unsubscribe from email alerts"}
      </Button>
    </div>
  );
}
