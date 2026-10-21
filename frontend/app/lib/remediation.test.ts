import { describe, it, expect } from "vitest";
import type { Remediation, RemediationState } from "./api";
import {
  REMEDIATION_LABELS,
  REMEDIATION_STATES,
  awaitsDecision,
  canCancel,
  canExecute,
  claimsCompliance,
  gateLabel,
  remediationLabel,
  toneForGate,
  toneForRemediation,
} from "./remediation";

/**
 * The wording and tone rules for a remediation record.
 *
 * Unit-tested directly because they decide what a user believes about whether
 * their video changed, and a regression here is a dishonest screen even when
 * every component test still passes.
 */

function record(
  state: RemediationState,
  overrides: Partial<Remediation> = {}
): Remediation {
  return {
    remediation_id: "rem_abc",
    video_id: "vid_abcd1234",
    frame_number: 1,
    detection_index: 0,
    state,
    stage: "x",
    is_terminal: false,
    is_active: false,
    is_compliance_claim: state === "verified",
    source_video_modified: false,
    finding: { frame_number: 1, detection_index: 0, category: "logo" },
    proposal: {
      action: "remove_region",
      summary: "s",
      rationale: "r",
      model_claim: "m",
      target_description: "t",
      limitations: "l",
      requires_approval: true,
      confidence_gate: "pass",
      gate_explanation: "g",
    },
    approved_by: null,
    approved_at: null,
    decided_by: null,
    decided_at: null,
    decision_note: null,
    provider: null,
    provider_model_id: null,
    has_asset: false,
    asset_bytes: null,
    asset_content_type: null,
    output_video_id: null,
    verification_video_id: null,
    verified_at: null,
    error_code: null,
    error_summary: null,
    attempt: 0,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

describe("labels", () => {
  it("covers every state the backend defines", () => {
    for (const state of REMEDIATION_STATES) {
      expect(REMEDIATION_LABELS[state]).toBeTruthy();
    }
  });

  it("says explicitly that an approved proposal generated nothing", () => {
    // The thing a reader would otherwise assume is exactly what has not
    // happened, so the label names it.
    expect(remediationLabel("approved")).toBe("Approved, not generated");
  });

  it("says an asset does not mean the video changed", () => {
    expect(remediationLabel("asset_ready")).toMatch(/video unchanged/i);
  });

  it("says an applied asset is not verified", () => {
    expect(remediationLabel("applied")).toMatch(/not verified/i);
  });

  it("names a failed verification as not confirming anything", () => {
    expect(remediationLabel("verification_failed")).toMatch(/did not confirm/i);
  });

  it("claims compliance in no label but the verified one", () => {
    for (const state of REMEDIATION_STATES) {
      if (state === "verified") continue;
      const label = REMEDIATION_LABELS[state].toLowerCase();
      expect(label).not.toContain("compliant");
      expect(label).not.toContain("corrected");
      expect(label).not.toContain("fixed");
      expect(label).not.toContain("resolved");
    }
  });

  it("renders an unknown state as unknown rather than guessing", () => {
    expect(remediationLabel("something_new")).toBe("Unknown");
  });
});

describe("tones", () => {
  it("treats approval as neutral, not a success", () => {
    // Agreeing to a change is not the change happening.
    expect(toneForRemediation("approved")).toBe("neutral");
  });

  it("treats an applied asset as neutral, not a success", () => {
    expect(toneForRemediation("applied")).toBe("neutral");
  });

  it("reserves success for the verified state", () => {
    expect(toneForRemediation("verified")).toBe("success");
    const successes = REMEDIATION_STATES.filter(
      (s) => toneForRemediation(s) === "success"
    );
    expect(successes).toEqual(["verified"]);
  });

  it("marks a failed verification as critical", () => {
    expect(toneForRemediation("verification_failed")).toBe("critical");
    expect(toneForRemediation("failed")).toBe("critical");
  });

  it("marks in-flight work as active", () => {
    expect(toneForRemediation("processing")).toBe("active");
    expect(toneForRemediation("verifying")).toBe("active");
  });
});

describe("claimsCompliance", () => {
  it("defers to the server's field", () => {
    expect(claimsCompliance(record("verified"))).toBe(true);
    expect(claimsCompliance(record("approved"))).toBe(false);
  });

  it("treats a missing field as no", () => {
    // Absent is not permission to claim compliance.
    const missing = record("verified", {
      is_compliance_claim: undefined as unknown as boolean,
    });
    expect(claimsCompliance(missing)).toBe(false);
  });

  it("does not infer compliance from the state string", () => {
    const inconsistent = record("verified", { is_compliance_claim: false });
    expect(claimsCompliance(inconsistent)).toBe(false);
  });
});

describe("available actions", () => {
  it("awaits a decision only while proposed", () => {
    expect(awaitsDecision(record("proposed"))).toBe(true);
    for (const state of REMEDIATION_STATES.filter((s) => s !== "proposed")) {
      expect(awaitsDecision(record(state))).toBe(false);
    }
  });

  it("allows execution only from approved", () => {
    expect(canExecute(record("approved"))).toBe(true);
    for (const state of REMEDIATION_STATES.filter((s) => s !== "approved")) {
      expect(canExecute(record(state))).toBe(false);
    }
  });

  it("allows withdrawal only before anything consequential", () => {
    expect(canCancel(record("proposed"))).toBe(true);
    expect(canCancel(record("approved"))).toBe(true);
    for (const state of ["asset_ready", "applied", "verified", "rejected"] as const) {
      expect(canCancel(record(state))).toBe(false);
    }
  });
});

describe("the confidence gate", () => {
  it("names each outcome", () => {
    expect(gateLabel("pass")).toMatch(/met the confidence threshold/i);
    expect(gateLabel("below_threshold")).toMatch(/below the confidence/i);
    expect(gateLabel("not_reported")).toMatch(/no confidence reported/i);
  });

  it("does not report an absent gate as a pass", () => {
    expect(gateLabel(undefined)).toBe("Confidence unknown");
    expect(toneForGate(undefined)).not.toBe("neutral");
  });
});
