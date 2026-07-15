"""Release packaging + retrieval for the verbatim raw mirror.

The ~40 MB raw mirror (data/raw/{clusters,opinions}/) is distributed as a GitHub Release asset
rather than committed, to keep clones slim. Immutability, inspectability, and tracing are preserved
by a committed checksum ledger (CHECKSUMS.sha256 — one SHA-256 per record plus the archive hash)
alongside the provenance manifest:
  - immutability: the committed hashes pin the exact bytes; a tampered asset fails verification.
  - inspectability: every record path is listed in the ledger; fetch_mirror puts the JSON on disk.
  - tracing: the manifest records what/when/which-code; the ledger binds this commit to the
    snapshot.

Stdlib only (hashlib, tarfile, gzip, urllib).
"""

import gzip
import hashlib
import io
import os
import tarfile
import urllib.request

ARCHIVE_NAME = "scotus-raw-mirror.tar.gz"
MIRROR_SUBDIRS = ("clusters", "opinions")


def iter_record_relpaths(raw_dir):
    """Yield each mirror record's path relative to raw_dir (e.g. 'clusters/10.json'), sorted."""
    for subdir in MIRROR_SUBDIRS:
        path = os.path.join(raw_dir, subdir)
        names = sorted(os.listdir(path)) if os.path.isdir(path) else []
        for name in names:
            if name.endswith(".json"):
                yield f"{subdir}/{name}"


def hash_file(path):
    """Return the SHA-256 hex digest of a file's bytes."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def build_archive(raw_dir, archive_path):
    """Write a DETERMINISTIC tar.gz of the mirror records to archive_path; return its SHA-256.

    Determinism (same records -> byte-identical archive -> stable hash) is required so the
    committed archive checksum is reproducible: members are added sorted with fixed
    mtime/uid/gid/mode, and gzip is written with mtime=0."""
    relpaths = list(iter_record_relpaths(raw_dir))
    with open(archive_path, "wb") as out:
        with gzip.GzipFile(filename="", fileobj=out, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for rel in relpaths:
                    with open(os.path.join(raw_dir, rel), "rb") as f:
                        data = f.read()
                    info = tarfile.TarInfo(rel)
                    info.size = len(data)
                    info.mtime = 0
                    info.mode = 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    tar.addfile(info, io.BytesIO(data))
    return hash_file(archive_path)


def write_checksums(raw_dir, archive_path, checksums_path):
    """Write the committed ledger: one 'sha256  relpath' line per record, plus the archive hash.

    The archive line uses the archive's basename as its path so it is unambiguous."""
    lines = [
        f"{hash_file(os.path.join(raw_dir, rel))}  {rel}" for rel in iter_record_relpaths(raw_dir)
    ]
    lines.append(f"{hash_file(archive_path)}  {ARCHIVE_NAME}")
    with open(checksums_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def read_checksums(checksums_path):
    """Parse a CHECKSUMS.sha256 file into {path: sha256}."""
    out = {}
    with open(checksums_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            digest, path = line.split("  ", 1)
            out[path] = digest
    return out


def verify_records(raw_dir, checksums_path):
    """Return a list of (relpath, reason) where an on-disk record does not match the ledger.

    Checks every record listed in the ledger (missing, changed) and flags any extra record on disk
    not in the ledger. Empty result == the mirror matches the committed checksums exactly."""
    expected = {p: h for p, h in read_checksums(checksums_path).items() if p != ARCHIVE_NAME}
    problems = []
    for rel, digest in expected.items():
        full = os.path.join(raw_dir, rel)
        if not os.path.exists(full):
            problems.append((rel, "missing"))
        elif hash_file(full) != digest:
            problems.append((rel, "hash mismatch"))
    on_disk = set(iter_record_relpaths(raw_dir))
    for rel in sorted(on_disk - set(expected)):
        problems.append((rel, "not in ledger"))
    return problems


def _safe_members(tar):
    """Yield only in-tree regular-file members (guards against path traversal on extract)."""
    for member in tar.getmembers():
        name = member.name
        if member.isfile() and not name.startswith("/") and ".." not in name.split("/"):
            if name.split("/", 1)[0] in MIRROR_SUBDIRS:
                yield member


def unpack_archive(archive_path, raw_dir):
    """Extract a mirror archive into raw_dir (only clusters/ and opinions/ members)."""
    with tarfile.open(archive_path, mode="r:gz") as tar:
        tar.extractall(raw_dir, members=list(_safe_members(tar)))


def download_file(url, dest):
    """Download url to dest (public GitHub Release asset; no auth for a public repo)."""
    req = urllib.request.Request(url, headers={"User-Agent": "scotus-data-bot/2.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        out.write(resp.read())


def fetch_mirror(url, raw_dir, checksums_path, archive_path):
    """Download the Release archive, verify its hash + every record hash, unpack into raw_dir.

    Reproduces the mirror from the Release (not CourtListener) and verifies immutability on the way
    in: a mismatch against the committed ledger raises RuntimeError."""
    ledger = read_checksums(checksums_path)
    download_file(url, archive_path)
    got = hash_file(archive_path)
    if got != ledger.get(ARCHIVE_NAME):
        raise RuntimeError(f"archive hash mismatch: {got} != committed {ledger.get(ARCHIVE_NAME)}")
    unpack_archive(archive_path, raw_dir)
    problems = verify_records(raw_dir, checksums_path)
    if problems:
        raise RuntimeError(f"{len(problems)} record(s) failed verification, e.g. {problems[:3]}")
    return len([p for p in ledger if p != ARCHIVE_NAME])
