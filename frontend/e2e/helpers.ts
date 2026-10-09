import { expect, type APIRequestContext, type Page } from "@playwright/test";

/** Calls the backend through the Next.js proxy with the page's cookies and CSRF token. */
export async function apiPost(page: Page, path: string, body: unknown = {}) {
  const cookies = await page.context().cookies();
  const csrf = cookies.find((c) => c.name === "sta_csrf")?.value ?? "";
  const res = await page.request.post(path, { data: body, headers: { "X-CSRF-Token": csrf } });
  expect(res.ok(), `${path} → ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.json();
}

export async function apiGet(request: APIRequestContext, path: string) {
  const res = await request.get(path);
  expect(res.ok(), `${path} → ${res.status()}`).toBeTruthy();
  return res.json();
}

export async function verifyEmailFromOutbox(page: Page, email: string) {
  const box = (await apiGet(page.request, "/api/dev/outbox?limit=50")) as Array<{ to?: string; text?: string }>;
  const msg = box.find((m) => m.to === email && /verify-email\?token=/.test(m.text ?? ""));
  expect(msg, "verification email in dev outbox").toBeTruthy();
  const token = /verify-email\?token=([\w-]+)/.exec(msg!.text!)![1];
  await page.goto(`/verify-email?token=${token}`);
  await expect(page.getByText("Email verified")).toBeVisible();
}
