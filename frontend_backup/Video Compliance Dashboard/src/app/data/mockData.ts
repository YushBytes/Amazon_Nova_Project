import { Video, Violation } from "../types";

export const mockVideos: Video[] = [
  {
    id: "vid-001",
    name: "Product Launch Video - Q1 2026",
    duration: 45,
    originalUrl: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
    violations: [
      {
        id: "v1",
        timestamp: 14,
        type: "logo",
        description: "Pepsico Logo",
        confidence: 98,
        severity: "high",
        boundingBox: { x: 120, y: 80, width: 100, height: 60 },
        status: "detected",
        strategy: "Inpainting via Nova Canvas",
      },
      {
        id: "v2",
        timestamp: 23,
        type: "logo",
        description: "Nike Swoosh",
        confidence: 95,
        severity: "medium",
        boundingBox: { x: 300, y: 150, width: 80, height: 50 },
        status: "detected",
        strategy: "Object Removal + Background Fill",
      },
      {
        id: "v3",
        timestamp: 38,
        type: "safety",
        description: "Electrical Hazard Warning",
        confidence: 87,
        severity: "high",
        boundingBox: { x: 200, y: 200, width: 120, height: 80 },
        status: "detected",
        strategy: "Content Overlay",
      },
    ],
    status: "scanning",
    uploadedAt: new Date("2026-03-10T10:30:00"),
  },
  {
    id: "vid-002",
    name: "Training Video - Safety Compliance",
    duration: 120,
    originalUrl: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ElephantsDream.mp4",
    correctedUrl: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
    violations: [
      {
        id: "v4",
        timestamp: 45,
        type: "logo",
        description: "Amazon Logo",
        confidence: 92,
        severity: "low",
        boundingBox: { x: 150, y: 100, width: 90, height: 40 },
        status: "fixed",
        strategy: "AI Inpainting",
      },
    ],
    status: "complete",
    grade: "A",
    uploadedAt: new Date("2026-03-09T14:20:00"),
  },
];

export const calculateGrade = (violations: Violation[]): string => {
  const highCount = violations.filter((v) => v.severity === "high").length;
  const mediumCount = violations.filter((v) => v.severity === "medium").length;
  const lowCount = violations.filter((v) => v.severity === "low").length;

  if (highCount > 2) return "F";
  if (highCount > 0 || mediumCount > 3) return "C";
  if (mediumCount > 0 || lowCount > 2) return "B";
  return "A";
};
