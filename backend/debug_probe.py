import ffmpeg
import sys
import json

def debug_probe(file_path):
    try:
        probe = ffmpeg.probe(file_path)
        print(json.dumps(probe, indent=2))
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        if video_stream:
            print(f"Video Stream found: {json.dumps(video_stream, indent=2)}")
            duration = video_stream.get('duration')
            print(f"Duration from stream: {duration}")
            if duration is None:
                # Check format duration
                duration = probe.get('format', {}).get('duration')
                print(f"Duration from format: {duration}")
        else:
            print("No video stream found.")
    except Exception as e:
        print(f"Probe Error: {e}")

if __name__ == "__main__":
    debug_probe(sys.argv[1])
