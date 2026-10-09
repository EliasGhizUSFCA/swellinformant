import Link from "next/link";

import { Logo } from "@/components/brand/logo";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-6 bg-ocean-gradient px-6 text-center text-white">
      <Logo light />
      <h1 className="font-display text-5xl font-semibold">Flat spell.</h1>
      <p className="max-w-md text-white/75">We couldn&apos;t find that page. The swell might have moved on.</p>
      <Button asChild variant="coral">
        <Link href="/">Back to the lineup</Link>
      </Button>
    </div>
  );
}
