"""Convert tracks listed as one ``<track path>;<layout>`` entry per line.

    python convert_multiple_tracks.py tracks.txt
"""
import argparse
import os
import subprocess
import sys


HERE = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
BUILD_TRACK = os.path.join(HERE, "build_track.py")


def build_command(track_dir, layout):
    resolved = os.path.abspath(track_dir) if not os.path.isabs(track_dir) else track_dir
    if getattr(sys, "frozen", False):
        command = [sys.executable, "build_track", resolved, "--install", "auto"]
    else:
        command = [sys.executable, BUILD_TRACK, resolved, "--install", "auto"]
    if layout:
        command.extend(["--layout", layout])
    return command


def parse_entry(line):
    """Parse ``<path to track>;<layout name>`` (layout optional) or return None to skip."""
    entry = line.strip()
    if not entry or entry.startswith("#"):
        return None
    track_dir, _separator, layout = entry.partition(";")
    track_dir = track_dir.strip().strip('"')
    layout = layout.strip().strip('"')
    if not track_dir:
        raise ValueError("missing track path")
    return track_dir, layout


def convert_file(input_file):
    """Run ``build_track.py --install auto`` for each entry, skipping errors.

    Returns ``(succeeded, skipped)`` where each item is ``(label, detail)``.
    """
    with open(input_file, encoding="utf-8-sig") as tracks:
        raw_lines = list(enumerate(tracks, 1))

    entries = []
    skipped = []
    for line_number, line in raw_lines:
        try:
            parsed = parse_entry(line)
        except ValueError as exc:
            msg = "%s:%d: %s" % (input_file, line_number, exc)
            print("Skipping %s" % msg, file=sys.stderr, flush=True)
            skipped.append((msg, str(exc)))
            continue
        if parsed is not None:
            entries.append((line_number, parsed[0], parsed[1]))

    succeeded = []
    total = len(entries)
    for index, (line_number, track_dir, layout) in enumerate(entries, 1):
        label = "%s (%s)" % (track_dir, layout) if layout else track_dir
        command = build_command(track_dir, layout)
        print("\n[%d/%d] Converting %s" % (index, total, label), flush=True)
        try:
            result = subprocess.run(command, cwd=HERE)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            reason = "could not start conversion: %s" % exc
            print("Skipping %s (line %d): %s" % (label, line_number, reason),
                  file=sys.stderr, flush=True)
            skipped.append((label, reason))
            continue

        if result.returncode:
            reason = "exit code %d" % result.returncode
            print("Skipping %s (line %d): conversion failed (%s)" %
                  (label, line_number, reason), file=sys.stderr, flush=True)
            skipped.append((label, reason))
        else:
            succeeded.append((label, "ok"))

    return succeeded, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input_file", help="text file with one track path and layout per line")
    args = ap.parse_args(argv)

    try:
        succeeded, skipped = convert_file(args.input_file)
    except OSError as exc:
        print("Could not read %s: %s" % (args.input_file, exc), file=sys.stderr)
        return 1

    print("\nBatch conversion complete: %d succeeded, %d skipped" %
          (len(succeeded), len(skipped)), flush=True)
    if skipped:
        print("Skipped entries:", flush=True)
        for label, reason in skipped:
            print("  - %s: %s" % (label, reason), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
