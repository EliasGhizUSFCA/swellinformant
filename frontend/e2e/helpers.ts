import { expect, type APIRequestContext, type Page } from "@playwright/test";

/**
 * Calls the backend through the Next.js proxy with the page's cookies and CSRF token. The
 * developer endpoints run the whole pipeline inside the request, so the timeout is longer
 * than Playwright's default 30 s (the e2e runner raises the proxy's timeout to match).
 */
export async function apiPost(page: Page, path: string, body: unknown = {}) {
  const cookies = await page.context().cookies();
  const csrf = cookies.find((c) => c.name === "sta_csrf")?.value ?? "";
  const res = await page.request.post(path, { data: body, headers: { "X-CSRF-Token": csrf }, timeout: 120_000 });
  expect(res.ok(), `${path} → ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

export async function apiGet(request: APIRequestContext, path: string) {
  const res = await request.get(path);
  expect(res.ok(), `${path} → ${res.status()}`).toBeTruthy();
  return res.json();
}

/**
 * The development inbox in use: Mailpit's API when E2E_MAILPIT_URL is set (EMAIL_PROVIDER=
 * mailpit, real SMTP delivery to a local catch-all), otherwise the console provider's
 * capture file exposed by /api/dev/outbox.
 */
const MAILPIT_URL = process.env.E2E_MAILPIT_URL?.replace(/\/$/, "") ?? null;
export const DEV_INBOX = MAILPIT_URL ? "mailpit" : "console-outbox";

interface DevEmail {
  to: string;
  subject: string;
  text: string;
}

async function listDevEmails(request: APIRequestContext, to: string): Promise<DevEmail[]> {
  if (!MAILPIT_URL) {
    const box = (await apiGet(request, "/api/dev/outbox?limit=200")) as Array<{ to?: string; subject?: string; text?: string }>;
    return box.filter((m) => m.to === to).map((m) => ({ to: m.to ?? "", subject: m.subject ?? "", text: m.text ?? "" }));
  }
  const res = await request.get(`${MAILPIT_URL}/api/v1/search?query=${encodeURIComponent(`to:"${to}"`)}`);
  expect(res.ok(), `Mailpit search → ${res.status()}`).toBeTruthy();
  const found = (await res.json()) as { messages: Array<{ ID: string; Subject: string }> };
  const out: DevEmail[] = [];
  for (const m of found.messages ?? []) {
    const full = await request.get(`${MAILPIT_URL}/api/v1/message/${m.ID}`);
    expect(full.ok(), `Mailpit message ${m.ID} → ${full.status()}`).toBeTruthy();
    const body = (await full.json()) as { Text?: string; Subject?: string };
    out.push({ to, subject: body.Subject ?? m.Subject, text: body.Text ?? "" });
  }
  return out;
}

/** Waits for an email to ``to`` whose text matches ``pattern`` in the development inbox. */
export async function waitForDevEmail(request: APIRequestContext, to: string, pattern: RegExp): Promise<DevEmail> {
  let match: DevEmail | undefined;
  await expect
    .poll(
      async () => {
        match = (await listDevEmails(request, to)).find((m) => pattern.test(m.text));
        return Boolean(match);
      },
      { message: `email to ${to} matching ${pattern} in the ${DEV_INBOX} inbox`, timeout: 20_000 },
    )
    .toBe(true);
  return match!;
}

export async function verifyEmailFromOutbox(page: Page, email: string) {
  const msg = await waitForDevEmail(page.request, email, /verify-email\?token=/);
  const token = /verify-email\?token=([\w-]+)/.exec(msg.text)![1];
  await page.goto(`/verify-email?token=${token}`);
  await expect(page.getByText("Email verified")).toBeVisible();
}
