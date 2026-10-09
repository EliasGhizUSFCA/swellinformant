import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, errorMessage, get } from "@/lib/api";

function stubFetch(status: number, body: string, contentType = "application/json") {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(body, { status, headers: { "Content-Type": contentType } })),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("returns parsed JSON", async () => {
    stubFetch(200, JSON.stringify({ ok: true }));
    await expect(get("/api/x")).resolves.toEqual({ ok: true });
  });

  it("surfaces the backend's error message and field details", async () => {
    stubFetch(
      422,
      JSON.stringify({
        error: { code: "validation_error", message: "Some fields are invalid.", details: [{ field: "email", message: "Enter a valid email." }] },
      }),
    );
    const err = await get("/api/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("validation_error");
    expect(errorMessage(err)).toBe("Enter a valid email.");
  });

  it("gives a friendly message when the proxy returns a plain-text 5xx (API down)", async () => {
    stubFetch(500, "Internal Server Error", "text/plain");
    const err = await get("/api/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(500);
    expect(errorMessage(err)).toMatch(/temporarily unavailable/);
  });

  it("rejects a non-JSON success body instead of returning garbage", async () => {
    stubFetch(200, "<html>captive portal</html>", "text/html");
    await expect(get("/api/x")).rejects.toBeInstanceOf(ApiError);
  });
});
