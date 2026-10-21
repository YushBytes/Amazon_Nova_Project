import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  ApiError,
  TOKEN_STORAGE_KEY,
  allowedVideoExtensions,
  coerceRole,
  describeError,
  fetchCurrentUser,
  fetchJob,
  fetchSubmissions,
  fetchWorkerHealth,
  formatBytes,
  isAbortError,
  isRenderableDetection,
  login,
  maxUploadBytes,
  signup,
  validateVideoFile,
} from "./api";

/**
 * Phase 7 additions to the API client.
 *
 * `fetch` is stubbed at the network boundary in every test; nothing contacts
 * a backend.
 */

function jsonResponse(
  body: unknown,
  status = 200,
  headers: Record<string, string> = {}
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function videoFile(name: string, size = 1024): File {
  const file = new File(["x"], name, { type: "video/mp4" });
  // jsdom derives size from the content; override it for limit tests.
  Object.defineProperty(file, "size", { value: size });
  return file;
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
  delete process.env.NEXT_PUBLIC_MAX_UPLOAD_BYTES;
  delete process.env.NEXT_PUBLIC_ALLOWED_VIDEO_EXTENSIONS;
});

afterEach(() => {
  delete process.env.NEXT_PUBLIC_MAX_UPLOAD_BYTES;
  delete process.env.NEXT_PUBLIC_ALLOWED_VIDEO_EXTENSIONS;
});

// ---------------------------------------------------------------------------
// upload constraints
// ---------------------------------------------------------------------------

describe("upload constraints mirror the backend defaults", () => {
  it("defaults to the backend's 100 MiB cap and extension allowlist", () => {
    // backend/config.py: MAX_UPLOAD_BYTES=104857600,
    // ALLOWED_VIDEO_EXTENSIONS=mp4,mov,avi,webm,mkv
    expect(maxUploadBytes()).toBe(104857600);
    expect(allowedVideoExtensions()).toEqual(["mp4", "mov", "avi", "webm", "mkv"]);
  });

  it("honours a deployment that narrows the limits", () => {
    process.env.NEXT_PUBLIC_MAX_UPLOAD_BYTES = "1048576";
    process.env.NEXT_PUBLIC_ALLOWED_VIDEO_EXTENSIONS = "mp4, .MOV";
    expect(maxUploadBytes()).toBe(1048576);
    expect(allowedVideoExtensions()).toEqual(["mp4", "mov"]);
  });

  it("ignores a malformed configuration rather than disabling validation", () => {
    process.env.NEXT_PUBLIC_MAX_UPLOAD_BYTES = "not-a-number";
    process.env.NEXT_PUBLIC_ALLOWED_VIDEO_EXTENSIONS = "!!!,***";
    expect(maxUploadBytes()).toBe(104857600);
    expect(allowedVideoExtensions()).toEqual(["mp4", "mov", "avi", "webm", "mkv"]);
  });
});

describe("validateVideoFile", () => {
  it("accepts every allowed extension, case-insensitively", () => {
    expect(validateVideoFile(videoFile("reel.mp4"))).toBeNull();
    expect(validateVideoFile(videoFile("reel.MOV"))).toBeNull();
    expect(validateVideoFile(videoFile("a.b.webm"))).toBeNull();
  });

  it("rejects a disallowed extension and names what is supported", () => {
    const problem = validateVideoFile(videoFile("payload.exe"));
    expect(problem).toMatch(/unsupported file type ".exe"/i);
    expect(problem).toMatch(/mp4/);
  });

  it("rejects a file with no extension", () => {
    expect(validateVideoFile(videoFile("noextension"))).toMatch(/must have an extension/i);
  });

  it("rejects an empty file, as the backend does", () => {
    expect(validateVideoFile(videoFile("reel.mp4", 0))).toMatch(/empty/i);
  });

  it("rejects a file over the configured cap and states both sizes", () => {
    process.env.NEXT_PUBLIC_MAX_UPLOAD_BYTES = "1048576";
    const problem = validateVideoFile(videoFile("big.mp4", 5 * 1024 * 1024));
    expect(problem).toMatch(/5\.0 MB/);
    expect(problem).toMatch(/1\.0 MB/);
  });

  it("is not fooled by a path-like name", () => {
    // Only the final suffix is considered, matching backend _safe_extension.
    expect(validateVideoFile(videoFile("C:\\evil\\reel.mp4"))).toBeNull();
    expect(validateVideoFile(videoFile("../../etc/passwd"))).toMatch(
      /must have an extension/i
    );
    expect(validateVideoFile(videoFile("../../etc/passwd.sh"))).toMatch(/unsupported/i);
  });
});

describe("formatBytes", () => {
  it("renders a dash rather than a fabricated zero", () => {
    expect(formatBytes(null)).toBe("--");
    expect(formatBytes(undefined)).toBe("--");
    expect(formatBytes(0)).toBe("--");
  });

  it("scales the unit", () => {
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});

// ---------------------------------------------------------------------------
// errors
// ---------------------------------------------------------------------------

describe("ApiError", () => {
  it("carries Retry-After when the backend throttles", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "slow down" }, 429, { "Retry-After": "12" }))
    );
    const err = await login("a", "b").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).isThrottled).toBe(true);
    expect((err as ApiError).retryAfterSeconds).toBe(12);
  });

  it("reports a missing Retry-After as null rather than zero", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({}, 429)));
    const err = (await signup("a", "b").catch((e: unknown) => e)) as ApiError;
    expect(err.retryAfterSeconds).toBeNull();
  });

  it("treats a 404 and a 403 alike, because the backend returns 404 for both", async () => {
    expect(new ApiError("x", 404).isNotFound).toBe(true);
    expect(new ApiError("x", 403).isNotFound).toBe(true);
    expect(new ApiError("x", 500).isNotFound).toBe(false);
  });

  it("raises a readable error for a 200 with an unparseable body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("<html>oops</html>", { status: 200 }))
    );
    await expect(fetchJob("vid_abc12345")).rejects.toThrow(/malformed/i);
  });
});

describe("describeError", () => {
  it("rewrites an expired session into an instruction", () => {
    expect(describeError(new ApiError("Could not validate credentials", 401), "x")).toMatch(
      /session has expired/i
    );
  });

  it("passes the backend's own detail through", () => {
    expect(describeError(new ApiError("Video payload not found.", 404), "x")).toBe(
      "Video payload not found."
    );
  });

  it("turns a transport failure into something actionable", () => {
    expect(describeError(new TypeError("Failed to fetch"), "x")).toMatch(
      /could not reach the server/i
    );
  });

  it("never renders a thrown non-error as [object Object]", () => {
    expect(describeError({ secret: "value" }, "Something went wrong")).toBe(
      "Something went wrong"
    );
  });
});

describe("isAbortError", () => {
  it("recognises an aborted request", () => {
    const controller = new AbortController();
    controller.abort();
    expect(isAbortError(controller.signal.reason)).toBe(true);
  });

  it("does not mistake an ordinary failure for a cancellation", () => {
    expect(isAbortError(new Error("boom"))).toBe(false);
    expect(isAbortError(new ApiError("nope", 500))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// request cancellation
// ---------------------------------------------------------------------------

describe("request cancellation", () => {
  it("passes the caller's signal to fetch", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ submissions: [] }));
    vi.stubGlobal("fetch", fetchMock);

    const controller = new AbortController();
    await fetchSubmissions({ signal: controller.signal });

    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
  });
});

// ---------------------------------------------------------------------------
// roles
// ---------------------------------------------------------------------------

describe("coerceRole", () => {
  it("accepts the two roles the backend recognises", () => {
    expect(coerceRole("agent")).toBe("agent");
    expect(coerceRole("client")).toBe("client");
  });

  it("falls back to the least-privileged role for anything else", () => {
    // Presentation must never widen on the strength of an unknown string.
    expect(coerceRole("admin")).toBe("client");
    expect(coerceRole("AGENT")).toBe("client");
    expect(coerceRole(undefined)).toBe("client");
    expect(coerceRole({ role: "agent" })).toBe("client");
  });
});

describe("fetchCurrentUser", () => {
  it("returns the server's view of the identity", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ username: "alice", role: "agent" }))
    );
    expect(await fetchCurrentUser()).toEqual({ username: "alice", role: "agent" });
  });

  it("narrows an unexpected role rather than trusting it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ username: "alice", role: "superuser" }))
    );
    expect((await fetchCurrentUser()).role).toBe("client");
  });

  it("surfaces a rejected session as a 401 ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Could not validate credentials" }, 401))
    );
    const err = (await fetchCurrentUser().catch((e: unknown) => e)) as ApiError;
    expect(err.isUnauthorized).toBe(true);
  });

  it("sends the stored bearer token", async () => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "a.b.c");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ username: "alice", role: "client" }));
    vi.stubGlobal("fetch", fetchMock);

    await fetchCurrentUser();

    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe("Bearer a.b.c");
    // The credential must never appear in the target.
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("a.b.c");
    window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  });
});

// ---------------------------------------------------------------------------
// worker health
// ---------------------------------------------------------------------------

describe("fetchWorkerHealth", () => {
  it("returns the counts the backend reported", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          status: "ok",
          execution_mode: "worker",
          counts: { queued: 1, completed: 4 },
          active: 0,
          abandoned: 0,
          note: "Inferred from the job queue; the worker process is not contacted.",
        })
      )
    );

    const health = await fetchWorkerHealth();
    expect(health.status).toBe("ok");
    expect(health.counts).toEqual({ queued: 1, completed: 4 });
    expect(health.note).toMatch(/not contacted/i);
  });

  it("reads the body of a 503, because degraded is an answer not a failure", async () => {
    // backend/app.py returns 503 with the same body shape so it can drive an
    // alert; discarding that body would lose the reason.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ status: "degraded", abandoned: 2 }, 503))
    );

    const health = await fetchWorkerHealth();
    expect(health.status).toBe("degraded");
    expect(health.abandoned).toBe(2);
  });

  it("throws for a genuine server error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Internal server error" }, 500))
    );
    await expect(fetchWorkerHealth()).rejects.toBeInstanceOf(ApiError);
  });
});

// ---------------------------------------------------------------------------
// detections
// ---------------------------------------------------------------------------

describe("isRenderableDetection narrows the box", () => {
  it("lets a caller read the box without an assertion", () => {
    const detection = { bounding_box: [1, 2, 3, 4] };
    if (isRenderableDetection(detection)) {
      // Compiles only because the predicate narrows the tuple.
      const [ymin] = detection.bounding_box;
      expect(ymin).toBe(1);
    } else {
      throw new Error("expected the detection to be renderable");
    }
  });

  it("rejects malformed model output rather than rendering NaN", () => {
    expect(isRenderableDetection({ bounding_box: [1, 2, 3] })).toBe(false);
    expect(isRenderableDetection({ bounding_box: [1, 2, 3, Number.NaN] })).toBe(false);
    expect(isRenderableDetection({})).toBe(false);
    expect(isRenderableDetection(null)).toBe(false);
  });
});
