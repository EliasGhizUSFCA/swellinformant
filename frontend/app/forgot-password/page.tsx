"use client";

import Link from "next/link";
import { useState } from "react";

import { AuthLayout } from "@/components/auth-layout";
import { Field } from "@/components/field";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage, post } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setState("sending");
    setError(null);
    try {
      await post("/api/auth/forgot-password", { email });
      setState("sent");
    } catch (err) {
      setError(errorMessage(err));
      setState("idle");
    }
  }

  return (
    <AuthLayout title="Reset your password" subtitle="We'll email you a link that's valid for 60 minutes.">
      {state === "sent" ? (
        <Alert variant="success">
          <AlertDescription>If an account exists for {email}, a reset link is on its way. Check your inbox.</AlertDescription>
        </Alert>
      ) : (
        <form onSubmit={submit} className="space-y-4">
          {error && (
            <Alert variant="danger">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <Field label="Email" htmlFor="email">
            <Input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
          </Field>
          <Button type="submit" className="w-full" size="lg" disabled={state === "sending"}>
            {state === "sending" ? "Sending…" : "Send reset link"}
          </Button>
        </form>
      )}
      <p className="mt-6 text-center text-sm text-muted-foreground">
        <Link href="/login" className="text-ocean hover:underline">
          Back to sign in
        </Link>
      </p>
    </AuthLayout>
  );
}
