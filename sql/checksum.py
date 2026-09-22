"""Checksum the canonical schema file so the portal's copy cannot drift.

The first line of the SQL file is `-- schema-checksum: <sha256>`, taken
over every byte AFTER that line. The portal repo's hand-written Prisma
migration carries the same value.

Usage:
  python -m sql.checksum                 # print expected vs actual
  python -m sql.checksum --write         # rewrite the header
"""

import hashlib
import pathlib
import sys

HEADER_PREFIX = "-- schema-checksum:"
DEFAULT_PATH = pathlib.Path(__file__).resolve().parent / "001_unifi_schema.sql"


def _split(path):
    lines = pathlib.Path(path).read_text().splitlines(keepends=True)
    if lines and lines[0].startswith(HEADER_PREFIX):
        return lines[0], "".join(lines[1:])
    return None, "".join(lines)


def compute(path=DEFAULT_PATH) -> str:
    _, body = _split(path)
    return hashlib.sha256(body.encode()).hexdigest()


def header_value(path=DEFAULT_PATH):
    header, _ = _split(path)
    if header is None:
        return None
    return header[len(HEADER_PREFIX):].strip()


def write(path=DEFAULT_PATH) -> str:
    path = pathlib.Path(path)
    _, body = _split(path)
    digest = hashlib.sha256(body.encode()).hexdigest()
    path.write_text(f"{HEADER_PREFIX} {digest}\n{body}")
    return digest


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--write"]
    target = pathlib.Path(args[0]) if args else DEFAULT_PATH
    if "--write" in sys.argv[1:]:
        print(f"{target}: {write(target)}")
    else:
        expected, actual = compute(target), header_value(target)
        print(f"expected: {expected}\nheader:   {actual}")
        sys.exit(0 if expected == actual else 1)
