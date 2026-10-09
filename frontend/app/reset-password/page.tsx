import { Suspense } from "react";

import { AuthLayout } from "@/components/auth-layout";
import { ResetForm } from "./reset-form";

export const metadata = { title: "Choose a new password" };

export default function ResetPasswordPage() {
  return (
    <AuthLayout title="Choose a new password" subtitle="All your other sessions will be signed out.">
      <Suspense>
        <ResetForm />
      </Suspense>
    </AuthLayout>
  );
}
