import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  apiBaseUrl,
  uploadReel,
  startAnalysis,
  fetchViolations,
  isRenderableDetection,
  ApiError,
} from "./api";

/**
 * Contract tests for the backend client (Phase 2, defect C).
 *
 * The response shapes asserted here were read off backend/app.py. If the
 * backend contract changes, these fail rather than the UI silently
 * misinterpreting a response.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("apiBaseUrl", () => {
  beforeEach(() => {
    delete process.env.NEXT_PUBLIC_API_URL;
  });

  it("falls back to the local backend when unset", () => {
    expect(apiBaseUrl()).toBe("http://localhost:8000");
  });

  it("uses NEXT_PUBLIC_API_URL when set", () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    expect(apiBaseUrl()).toBe("https://api.example.test");
  });
});

describe("uploadReel", () => {
  it("POSTs multipart form data and returns the upload metadata", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        video_id: "vid_abc12345",
        video_url: "http://backend.test/uploads/vid_abc12345.mp4",
        filename: "vid_abc12345.mp4",
        status: "UPLOADED",
        message: "Payload ready for analysis.",
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["data"], "reel.mp4", { type: "video/mp4" });
    const result = await uploadReel(file);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://backend.test/upload-reel");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    // The backend reads the part named "file".
    expect((init.body as FormData).get("file")).toBe(file);

    expect(result.video_id).toBe("vid_abc12345");
    expect(result.status).toBe("UPLOADED");
  });

  it("throws ApiError carrying the backend detail on failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "File too large" }, 413))
    );

    const file = new File(["x"], "reel.mp4", { type: "video/mp4" });
    await expect(uploadReel(file)).rejects.toMatchObject({
      name: "ApiError",
      status: 413,
      message: "File too large",
    });
  });

  it("rejects a 200 response that omits video_id", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "UPLOADED" })));
    const file = new File(["x"], "reel.mp4", { type: "video/mp4" });
    await expect(uploadReel(file)).rejects.toBeInstanceOf(ApiError);
  });
});

describe("startAnalysis", () => {
  it("POSTs to /analyze/{video_id} and returns the accepted status", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        video_id: "vid_abc12345",
        status: "ANALYZING",
        message: "Neural analysis initiated.",
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await startAnalysis("vid_abc12345");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://backend.test/analyze/vid_abc12345");
    expect(init.method).toBe("POST");
    expect(result.status).toBe("ANALYZING");
  });

  it("surfaces the backend 404 when the payload is missing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: "Video payload not found." }, 404)
      )
    );

    await expect(startAnalysis("vid_missing")).rejects.toMatchObject({
      status: 404,
      message: "Video payload not found.",
    });
  });

  it("url-encodes the video id", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ video_id: "a/b", status: "ANALYZING" }));
    vi.stubGlobal("fetch", fetchMock);

    await startAnalysis("a/b");
    expect(fetchMock.mock.calls[0][0]).toBe("http://backend.test/analyze/a%2Fb");
  });
});

describe("fetchViolations", () => {
  it("returns detections and frame_count", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          video_id: "vid_abc12345",
          detections: [
            {
              category: "competitor_logo",
              confidence: 0.91,
              bounding_box: [100, 200, 300, 400],
              timestamp: 2.0,
            },
          ],
          frame_count: 7,
        })
      )
    );

    const data = await fetchViolations("vid_abc12345");
    expect(data.frame_count).toBe(7);
    expect(data.detections).toHaveLength(1);
    expect(data.detections[0].category).toBe("competitor_logo");
  });

  it("normalizes a response missing its fields instead of throwing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({})));

    const data = await fetchViolations("vid_abc12345");
    expect(data.detections).toEqual([]);
    expect(data.frame_count).toBe(0);
  });

  it("throws on a server error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "boom" }, 500))
    );
    await expect(fetchViolations("vid_abc12345")).rejects.toMatchObject({
      status: 500,
    });
  });
});

describe("isRenderableDetection", () => {
  it("accepts a well-formed bounding box", () => {
    expect(
      isRenderableDetection({ bounding_box: [1, 2, 3, 4], confidence: 0.5 })
    ).toBe(true);
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["missing box", { category: "x" }],
    ["short box", { bounding_box: [1, 2, 3] }],
    ["non-numeric box", { bounding_box: [1, 2, 3, "4"] as unknown as number[] }],
    ["NaN in box", { bounding_box: [1, 2, 3, NaN] }],
  ])("rejects %s so the overlay never renders NaN", (_label, input) => {
    expect(isRenderableDetection(input as never)).toBe(false);
  });
});
