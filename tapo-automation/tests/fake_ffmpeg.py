#!/usr/bin/env python3
"""
Stand-in for ffmpeg used by tests/test_cam_recorder.py.

The behaviour is chosen by the input URL (the argument after -i):

    fake://record             write a growing segment until SIGTERM, then exit 0
    fake://exit/<code>        exit immediately with <code>
    fake://hang               run without writing anything, exit 0 on SIGTERM
    fake://stubborn           write, and ignore SIGTERM (has to be SIGKILLed)
    fake://qonly              write, ignore SIGTERM, exit 0 only on 'q' from stdin
    fake://die-after/<secs>   write for <secs> seconds, then exit 1

`fake_ffmpeg.py -version` prints a version line like the real thing, so
`cam_recorder.py --check` works with it.

If FAKE_FFMPEG_ARGV_DIR is set the full argv is dumped there as
<camera dir name>.json so tests can inspect the spawned command.
"""
import json
import os
import signal
import sys
import time


def main():
    argv = sys.argv[1:]
    if argv == ["-version"]:
        print("ffmpeg version fake-1.0 (tests/fake_ffmpeg.py)")
        return 0

    url = argv[argv.index("-i") + 1]
    out_pattern = argv[-1]
    out_dir = os.path.dirname(out_pattern)

    argv_dir = os.environ.get("FAKE_FFMPEG_ARGV_DIR")
    if argv_dir:
        with open(os.path.join(argv_dir, os.path.basename(out_dir) + ".json"), "w") as f:
            json.dump(argv, f)

    mode = url[len("fake://"):] if url.startswith("fake://") else "record"

    if mode.startswith("exit/"):
        sys.stderr.write(f"fake ffmpeg: simulated failure opening {url}\n")
        return int(mode.split("/", 1)[1])

    stop = {"flag": False}

    def on_term(*_):
        stop["flag"] = True

    if mode in ("stubborn", "qonly"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, on_term)

    if mode == "qonly":
        import threading

        def watch_stdin():
            for line in sys.stdin:
                if line.strip() == "q":
                    stop["flag"] = True
                    return

        threading.Thread(target=watch_stdin, daemon=True).start()

    if mode == "hang":
        while not stop["flag"]:
            time.sleep(0.02)
        return 0

    die_after = None
    if mode.startswith("die-after/"):
        die_after = float(mode.split("/", 1)[1])

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, time.strftime(os.path.basename(out_pattern)))
    start = time.time()
    with open(path, "ab") as f:
        while not stop["flag"]:
            f.write(b"x" * 1024)
            f.flush()
            if die_after is not None and time.time() - start >= die_after:
                sys.stderr.write("fake ffmpeg: connection lost\n")
                return 1
            time.sleep(0.02)
        f.write(b"moov")  # stands for the trailer a real ffmpeg writes on a clean stop
    return 0


if __name__ == "__main__":
    sys.exit(main())
