import { Logo } from "@/components/brand/logo";

export function AuthLayout({ title, subtitle, children }: { title: string; subtitle?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="grid min-h-dvh lg:grid-cols-[1fr_1.05fr]">
      <div className="flex flex-col px-6 py-8 sm:px-12">
        <Logo />
        <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center py-10">
          <h1 className="font-display text-3xl font-semibold text-ink">{title}</h1>
          {subtitle && <p className="mt-2 text-sm text-muted-foreground">{subtitle}</p>}
          <div className="mt-8">{children}</div>
        </div>
      </div>
      <div className="relative hidden overflow-hidden bg-ocean-gradient lg:block">
        <div className="absolute inset-0 flex flex-col justify-end p-12 text-white">
          <blockquote className="max-w-md">
            <p className="font-display text-3xl leading-snug">
              &ldquo;Instead of choosing a destination and hoping for waves, let the waves determine the destination.&rdquo;
            </p>
            <footer className="mt-4 text-sm text-white/70">Swell Travel Agent</footer>
          </blockquote>
        </div>
        <svg className="absolute bottom-40 left-0 h-48 w-[200%] wave-layer-slow opacity-40" viewBox="0 0 2880 160" preserveAspectRatio="none" aria-hidden="true">
          <path d="M0 80c240 0 240-60 480-60s240 60 480 60 240-60 480-60 240 60 480 60 240-60 480-60 240 60 480 60v80H0z" fill="#2bb3a3" />
        </svg>
      </div>
    </div>
  );
}
