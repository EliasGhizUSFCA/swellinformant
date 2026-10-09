import { Suspense } from "react";

import { AuthLayout } from "@/components/auth-layout";
import { VerifyEmail } from "./verify";

export const metadata = { title: "Verify email" };

export default function VerifyEmailPage() {
  return (
    <AuthLayout title="Verifying your email">
      <Suspense>
        <VerifyEmail />
      </Suspense>
    </AuthLayout>
  );
}
