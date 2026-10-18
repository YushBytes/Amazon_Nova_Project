import { useState, useRef, useEffect } from "react";
import { Violation } from "../types";
import { Button } from "./ui/button";
import { Slider } from "./ui/slider";
import { Play, Pause, SkipBack, SkipForward } from "lucide-react";
import { motion, AnimatePresence } from "motion/react";

interface AuditMirrorProps {
  originalUrl: string;
  correctedUrl?: string;
  violations: Violation[];
  currentTime: number;
  onTimeUpdate: (time: number) => void;
  onManualFlag: (x: number, y: number) => void;
}

export function AuditMirror({
  originalUrl,
  correctedUrl,
  violations,
  currentTime,
  onTimeUpdate,
  onManualFlag,
}: AuditMirrorProps) {
  const [isPlaying, setIsPlaying] = useState(false);
  const [showComparison, setShowComparison] = useState(false);
  const [dividerPosition, setDividerPosition] = useState(50);
  const videoRef = useRef<HTMLVideoElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (videoRef.current) {
      if (isPlaying) {
        videoRef.current.play();
      } else {
        videoRef.current.pause();
      }
    }
  }, [isPlaying]);

  const handleTimeUpdate = () => {
    if (videoRef.current) {
      onTimeUpdate(videoRef.current.currentTime);
    }
  };

  const handleSeek = (value: number[]) => {
    if (videoRef.current) {
      videoRef.current.currentTime = value[0];
      onTimeUpdate(value[0]);
    }
  };

  const handleVideoClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect();
      const x = ((e.clientX - rect.left) / rect.width) * 100;
      const y = ((e.clientY - rect.top) / rect.height) * 100;
      onManualFlag(x, y);
    }
  };

  // Get violations visible at current time (within 2 seconds)
  const visibleViolations = violations.filter(
    (v) => Math.abs(v.timestamp - currentTime) < 2
  );

  const duration = videoRef.current?.duration || 100;

  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-lg overflow-hidden">
      {/* Header */}
      <div className="border-b border-slate-700 bg-slate-900/50 px-4 py-3">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-sm">Audit Mirror</h3>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant={showComparison ? "default" : "outline"}
              onClick={() => setShowComparison(!showComparison)}
              disabled={!correctedUrl}
              className={
                showComparison
                  ? "bg-gradient-to-r from-cyan-500 to-violet-500"
                  : "border-slate-600"
              }
            >
              {showComparison ? "Comparison View" : "Original Only"}
            </Button>
          </div>
        </div>
      </div>

      {/* Video Player */}
      <div className="relative bg-black aspect-video" ref={containerRef} onClick={handleVideoClick}>
        {/* Original Video */}
        <video
          ref={videoRef}
          src={originalUrl}
          className="w-full h-full"
          onTimeUpdate={handleTimeUpdate}
          onEnded={() => setIsPlaying(false)}
        />

        {/* Comparison Overlay */}
        {showComparison && correctedUrl && (
          <div
            className="absolute inset-0 overflow-hidden"
            style={{ clipPath: `inset(0 0 0 ${dividerPosition}%)` }}
          >
            <video
              src={correctedUrl}
              className="w-full h-full"
              style={{ objectFit: "cover" }}
            />
          </div>
        )}

        {/* Divider for comparison */}
        {showComparison && correctedUrl && (
          <motion.div
            className="absolute inset-y-0 w-1 bg-cyan-400 cursor-ew-resize z-10"
            style={{ left: `${dividerPosition}%` }}
            drag="x"
            dragConstraints={containerRef}
            dragElastic={0}
            onDrag={(e, info) => {
              if (containerRef.current) {
                const rect = containerRef.current.getBoundingClientRect();
                const newPosition = ((info.point.x - rect.left) / rect.width) * 100;
                setDividerPosition(Math.max(0, Math.min(100, newPosition)));
              }
            }}
          >
            <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-cyan-400 rounded-full p-2">
              <div className="flex gap-1">
                <div className="w-1 h-4 bg-white rounded"></div>
                <div className="w-1 h-4 bg-white rounded"></div>
              </div>
            </div>
          </motion.div>
        )}

        {/* Labels for comparison */}
        {showComparison && correctedUrl && (
          <>
            <div className="absolute top-4 left-4 bg-red-500/80 text-white text-xs font-bold px-3 py-1 rounded">
              ORIGINAL
            </div>
            <div className="absolute top-4 right-4 bg-green-500/80 text-white text-xs font-bold px-3 py-1 rounded">
              CORRECTED
            </div>
          </>
        )}

        {/* Bounding Boxes */}
        <AnimatePresence>
          {visibleViolations.map((violation) => (
            <motion.div
              key={violation.id}
              initial={{ opacity: 0, scale: 0.8 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.8 }}
              className="absolute border-2 border-cyan-400 rounded pointer-events-none"
              style={{
                left: `${violation.boundingBox.x}px`,
                top: `${violation.boundingBox.y}px`,
                width: `${violation.boundingBox.width}px`,
                height: `${violation.boundingBox.height}px`,
                boxShadow: "0 0 20px rgba(6, 182, 212, 0.6)",
              }}
            >
              <div className="absolute -top-6 left-0 bg-cyan-400 text-black text-xs font-bold px-2 py-1 rounded">
                [{violation.boundingBox.x}, {violation.boundingBox.y}]
              </div>
              <div className="absolute -bottom-6 left-0 bg-slate-900 text-cyan-400 text-xs px-2 py-1 rounded border border-cyan-400">
                {violation.description}
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>

      {/* Controls */}
      <div className="p-4 space-y-4">
        {/* Timeline */}
        <div className="space-y-2">
          <Slider
            value={[currentTime]}
            max={duration}
            step={0.1}
            onValueChange={handleSeek}
            className="w-full"
          />
          <div className="flex justify-between text-xs text-slate-400">
            <span>
              {Math.floor(currentTime / 60)}:{String(Math.floor(currentTime % 60)).padStart(2, "0")}
            </span>
            <span>
              {Math.floor(duration / 60)}:{String(Math.floor(duration % 60)).padStart(2, "0")}
            </span>
          </div>
        </div>

        {/* Playback Controls */}
        <div className="flex items-center justify-center gap-2">
          <Button
            size="sm"
            variant="outline"
            className="border-slate-600"
            onClick={() => handleSeek([Math.max(0, currentTime - 5)])}
          >
            <SkipBack className="w-4 h-4" />
          </Button>
          
          <Button
            size="sm"
            className="bg-gradient-to-r from-cyan-500 to-violet-500 hover:from-cyan-600 hover:to-violet-600"
            onClick={() => setIsPlaying(!isPlaying)}
          >
            {isPlaying ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
          </Button>

          <Button
            size="sm"
            variant="outline"
            className="border-slate-600"
            onClick={() => handleSeek([Math.min(duration, currentTime + 5)])}
          >
            <SkipForward className="w-4 h-4" />
          </Button>
        </div>

        {/* Manual Flag Hint */}
        <div className="text-center text-xs text-slate-500">
          Click anywhere on the video to manually flag a violation
        </div>
      </div>
    </div>
  );
}
