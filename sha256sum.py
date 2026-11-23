"""Calculate SHA-256 checksum(s) for one or more input files.

    python sha256sum.py <file> [<file> ...]
"""
import argparse
import hashlib
import sys

CHUNK_SIZE = 1024 * 1024


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Calculate SHA-256 checksum of input file(s).")
    ap.add_argument("files", nargs="+", help="path(s) to input file(s)")
    args = ap.parse_args(argv)

    failed = False
    for path in args.files:
        try:
            checksum = sha256_file(path)
        except OSError as exc:
            print("sha256sum: %s: %s" % (path, exc), file=sys.stderr)
            failed = True
            continue
        print("%s  %s" % (checksum, path))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
