"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { Field } from "@/components/field";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { qk } from "@/hooks/queries";
import { ApiError, errorMessage, post } from "@/lib/api";
import { registerSchema, type RegisterValues } from "@/lib/validation";
import type { AuthResponse } from "@/types/api";

export function RegisterForm() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const { register, handleSubmit, formState, setError: setFieldError } = useForm<RegisterValues>({
    resolver: zodResolver(registerSchema),
  });

  async function onSubmit(values: RegisterValues) {
    setError(null);
    try {
      const res = await post<AuthResponse>("/api/auth/register", values);
      queryClient.setQueryData(qk.me, res.user);
      router.push("/dashboard?welcome=1");
    } catch (e) {
      if (e instanceof ApiError && e.code === "weak_password") setFieldError("password", { message: e.message });
      else setError(errorMessage(e));
    }
  }

  const err = formState.errors;
  return (
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
      {error && (
        <Alert variant="danger">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <Field label="Full name" htmlFor="full_name" error={err.full_name?.message}>
        <Input id="full_name" autoComplete="name" {...register("full_name")} aria-invalid={!!err.full_name} />
      </Field>
      <Field label="Email" htmlFor="email" error={err.email?.message}>
        <Input id="email" type="email" autoComplete="email" {...register("email")} aria-invalid={!!err.email} />
      </Field>
      <Field label="Password" htmlFor="password" error={err.password?.message} hint="At least 10 characters, with letters and a number or symbol.">
        <Input id="password" type="password" autoComplete="new-password" {...register("password")} aria-invalid={!!err.password} />
      </Field>
      <Field label="Confirm password" htmlFor="confirm_password" error={err.confirm_password?.message}>
        <Input id="confirm_password" type="password" autoComplete="new-password" {...register("confirm_password")} aria-invalid={!!err.confirm_password} />
      </Field>
      <Button type="submit" className="w-full" size="lg" disabled={formState.isSubmitting}>
        {formState.isSubmitting ? "Creating account…" : "Create account"}
      </Button>
      <p className="text-center text-sm text-muted-foreground">
        Already have an account?{" "}
        <Link href="/login" className="font-medium text-ocean hover:underline">
          Sign in
        </Link>
      </p>
    </form>
  );
}
