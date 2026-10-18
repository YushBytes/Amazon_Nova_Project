export interface Violation {
  id: string;
  timestamp: number;
  type: "logo" | "safety" | "content";
  description: string;
  confidence: number;
  severity: "low" | "medium" | "high";
  boundingBox: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
  status: "detected" | "pending_approval" | "approved" | "fixed" | "rejected";
  strategy?: string;
}

export interface LogEntry {
  id: string;
  timestamp: string;
  message: string;
  type: "info" | "detection" | "processing" | "success" | "error";
}

export interface Video {
  id: string;
  name: string;
  duration: number;
  originalUrl: string;
  correctedUrl?: string;
  violations: Violation[];
  status: "uploading" | "scanning" | "detected" | "processing" | "review" | "complete";
  grade?: string;
  uploadedAt: Date;
}

export type ProcessingStage = "SCANNING" | "DETECTED" | "GENERATING FIX" | "FINAL REVIEW";
