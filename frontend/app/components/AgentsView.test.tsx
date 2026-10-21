import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import AgentsView from "./AgentsView";

/**
 * Phase 7: this screen showed invented agents and metrics.
 *
 * The regression these tests exist to prevent is the reappearance of data
 * with no backend source. Everything rendered must come from
 * GET /health/worker, and the endpoint's own caveat - that it infers worker
 * health from the queue rather than contacting the worker - must survive to
 * the screen.
 *
 * `fetch` is stubbed; nothing contacts a backend.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const HEALTH_OK = {
  status: "ok",
  execution_mode: "worker",
  counts: { uploaded: 2, queued: 0, completed: 5, failed: 1 },
  active: 0,
  abandoned: 0,
  note: "Inferred from the job queue; the worker process is not contacted.",
};

function stubHealth(factory: () => Response | Promise<Response>) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (url: string) => {
    calls.push(url);
    if (url.includes("/health/worker")) return factory();
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
});

describe("no fabricated infrastructure data", () => {
  it("does not render the invented agents or metrics it used to", async () => {
    stubHealth(() => jsonResponse(HEALTH_OK));
    render(<AgentsView />);

    await waitFor(() => expect(screen.getByText(/jobs by state/i)).toBeTruthy());

    // Every one of these was hardcoded, with no endpoint behind it.
    expect(screen.queryByText(/Core-Act-01/i)).toBeNull();
    expect(screen.queryByText(/Nova-Vision-7/i)).toBeNull();
    expect(screen.queryByText(/logic load/i)).toBeNull();
    expect(screen.queryByText(/vision latency/i)).toBeNull();
    expect(screen.queryByText(/99\.9%/)).toBeNull();
    expect(screen.queryByText(/2\.1ms/)).toBeNull();
    expect(screen.queryByText(/cluster optimal/i)).toBeNull();
    expect(screen.queryByText(/expand cluster/i)).toBeNull();
    expect(screen.queryByText(/purge cache/i)).toBeNull();
  });

  it("loads stock photography for nothing", async () => {
    stubHealth(() => jsonResponse(HEALTH_OK));
    const { container } = render(<AgentsView />);
    await waitFor(() => expect(screen.getByText(/execution mode/i)).toBeTruthy());
    expect(container.querySelectorAll("img")).toHaveLength(0);
  });

  it("reads only the worker health endpoint", async () => {
    const stub = stubHealth(() => jsonResponse(HEALTH_OK));
    render(<AgentsView />);
    await waitFor(() => expect(stub.calls.length).toBeGreaterThan(0));
    expect(stub.calls.every((u) => u.includes("/health/worker"))).toBe(true);
  });
});

describe("real queue data", () => {
  it("renders the counts the backend reported", async () => {
    stubHealth(() => jsonResponse(HEALTH_OK));
    render(<AgentsView />);

    await waitFor(() => expect(screen.getByText(/jobs by state/i)).toBeTruthy());
    expect(screen.getByText(/8 total/)).toBeTruthy();
    expect(screen.getByText("Healthy")).toBeTruthy();
    expect(screen.getByText("worker")).toBeTruthy();
    // Counts render per state tile.
    expect(screen.getByText("Analyzed").closest("div")?.textContent).toMatch(/5/);
  });

  it("carries the endpoint's own caveat to the screen", async () => {
    stubHealth(() => jsonResponse(HEALTH_OK));
    render(<AgentsView />);

    await waitFor(() =>
      expect(screen.getByText(/the worker process is not contacted/i)).toBeTruthy()
    );
    // It must not be presented as measured, live or validated.
    expect(screen.getByText(/nothing here has been observed against live aws/i)).toBeTruthy();
  });

  it("shows an empty queue as empty, not as a healthy cluster", async () => {
    stubHealth(() =>
      jsonResponse({ status: "ok", execution_mode: "inline", counts: {}, active: 0, abandoned: 0 })
    );
    render(<AgentsView />);

    await waitFor(() => expect(screen.getByText(/the queue is empty/i)).toBeTruthy());
  });

  it("surfaces a degraded 503 rather than discarding it", async () => {
    stubHealth(() =>
      jsonResponse(
        { status: "degraded", execution_mode: "worker", counts: { queued: 3 }, active: 0, abandoned: 2 },
        503
      )
    );
    render(<AgentsView />);

    await waitFor(() => expect(screen.getByText("Degraded")).toBeTruthy());
    expect(screen.getByText(/abandoned jobs/i)).toBeTruthy();
  });

  it("names the no-worker signature when the queue is stalled", async () => {
    stubHealth(() =>
      jsonResponse({
        status: "ok",
        execution_mode: "worker",
        counts: { queued: 4 },
        active: 0,
        abandoned: 0,
      })
    );
    render(<AgentsView />);

    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert.textContent).toMatch(/no worker consuming the queue/i);
      // Still honest about how it knows.
      expect(alert.textContent).toMatch(/inferred from the queue/i);
    });
  });

  it("does not claim a stalled queue when work is in flight", async () => {
    stubHealth(() =>
      jsonResponse({
        status: "ok",
        execution_mode: "worker",
        counts: { queued: 4, analyzing: 1 },
        active: 1,
        abandoned: 0,
      })
    );
    render(<AgentsView />);

    await waitFor(() => expect(screen.getByText(/jobs by state/i)).toBeTruthy());
    expect(screen.queryByText(/no worker consuming/i)).toBeNull();
  });
});

describe("loading and error states", () => {
  it("announces that it is loading before any data arrives", async () => {
    let release: (r: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    stubHealth(() => pending);
    render(<AgentsView />);

    expect(screen.getByText(/reading queue health/i)).toBeTruthy();

    await act(async () => {
      release(jsonResponse(HEALTH_OK));
    });
    await waitFor(() => expect(screen.queryByText(/reading queue health/i)).toBeNull());
  });

  it("shows an actionable error when the endpoint is unreachable", async () => {
    stubHealth(() => {
      throw new TypeError("Failed to fetch");
    });
    render(<AgentsView />);

    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert.textContent).toMatch(/could not reach the server/i);
    });
    expect(screen.getByRole("button", { name: /try again/i })).toBeTruthy();
  });

  it("retries on demand after a failure", async () => {
    let attempt = 0;
    const stub = stubHealth(() => {
      attempt += 1;
      if (attempt === 1) throw new TypeError("Failed to fetch");
      return jsonResponse(HEALTH_OK);
    });
    render(<AgentsView />);

    await waitFor(() => expect(screen.getByRole("button", { name: /try again/i })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));

    await waitFor(() => expect(screen.getByText(/8 total/)).toBeTruthy());
    expect(stub.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("stops polling when unmounted", async () => {
    const stub = stubHealth(() => jsonResponse(HEALTH_OK));
    const { unmount } = render(<AgentsView />);

    await waitFor(() => expect(stub.calls.length).toBe(1));
    unmount();

    await new Promise((r) => setTimeout(r, 60));
    expect(stub.calls.length).toBe(1);
  });
});
