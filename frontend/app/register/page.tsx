import { AuthLayout } from "@/components/auth-layout";
import { RegisterForm } from "./register-form";

export const metadata = { title: "Create account" };

export default function RegisterPage() {
  return (
    <AuthLayout title="Create your account" subtitle="Free to start. Set up your first swell search in two minutes.">
      <RegisterForm />
    </AuthLayout>
  );
}
