import { Suspense } from "react";

import { AuthLayout } from "@/components/auth-layout";
import { LoginForm } from "./login-form";

export const metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <AuthLayout title="Welcome back" subtitle="Sign in to see your swell opportunities.">
      <Suspense>
        <LoginForm />
      </Suspense>
    </AuthLayout>
  );
}
