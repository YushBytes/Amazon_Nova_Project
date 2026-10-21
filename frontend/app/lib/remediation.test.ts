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

  it("names the step an approved proposal is waiting on", () => {
    // Phase 9: what it is waiting for is a replacement image, because no
    // provider can generate one. The label says which step is outstanding
    // rather than only what has not happened.
    expect(remediationLabel("approved")).toMatch(/replacement image/i);
  });

  it("says an asset does not mean the video changed", () => {
    expect(remediationLabel("asset_ready")).toMatch(/video unchanged/i);
  });

  it("says the original is untouched while a new video is built", () => {
    expect(remediationLabel("reconstructing")).toMatch(/original unchanged/i);
  });

  it("says a built video has not been checked", () => {
    // The most load-bearing label in the file. An output exists and nothing
    // has analysed it, so anything resembling "corrected" would be a claim
    // nobody has verified.
    const label = remediationLabel("applied").toLowerCase();
    expect(label).toMatch(/not yet checked/);
    expect(label).not.toContain("corrected");
    expect(label).not.toContain("verified:");
  });

  it("names a failed check as the finding still being there", () => {
    expect(remediationLabel("verification_failed")).toMatch(/still there/i);
  });

  it("claims compliance in no label at all", () => {
    // Not even the verified one: `verified` means the TARGETED finding is
    // gone, which is not the same as the video being compliant. The default
    // policy does not require a clean output, so that word must never appear.
    for (const state of REMEDIATION_STATES) {
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

  it("treats a built-but-unchecked video as neutral, not a success", () => {
    expect(toneForRemediation("applied")).toBe("neutral");
  });

  it("treats a reconstruction in flight as active", () => {
    expect(toneForRemediation("reconstructing")).toBe("active");
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

  it("allows building only from asset_ready", () => {
    // Phase 9: an approved proposal with no replacement image cannot be
    // built, and the backend refuses it too.
    expect(canExecute(record("asset_ready"))).toBe(true);
    for (const state of REMEDIATION_STATES.filter((s) => s !== "asset_ready")) {
      expect(canExecute(record(state))).toBe(false);
    }
  });

  it("allows withdrawal only before anything is being built", () => {
    expect(canCancel(record("proposed"))).toBe(true);
    expect(canCancel(record("approved"))).toBe(true);
    // An attached asset is still withdrawable; a worker holding the record is
    // not, and neither is a record with an output.
    expect(canCancel(record("asset_ready"))).toBe(true);
    for (const state of [
      "reconstructing",
      "applied",
      "verified",
      "rejected",
    ] as const) {
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
