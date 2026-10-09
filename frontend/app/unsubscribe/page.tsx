import { Suspense } from "react";

import { AuthLayout } from "@/components/auth-layout";
import { Unsubscribe } from "./unsubscribe";

export const metadata = { title: "Unsubscribe" };

export default function UnsubscribePage() {
  return (
    <AuthLayout title="Email alerts" subtitle="Stop receiving swell alerts by email.">
      <Suspense>
        <Unsubscribe />
      </Suspense>
    </AuthLayout>
  );
}
