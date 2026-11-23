#!/usr/bin/env python3
"""Name every path that differs between two container file systems (`docker export` tarballs).

    scripts/rootfs_diff.py <old.tar> <new.tar>

Prints one line per path, in path order: `changed <path>`, `added <path>` or `removed <path>`. A path is
changed if its type, mode, owner, group, link target or content (sha256) differs; file times are not compared.
Exit 0 whether or not anything differs (the caller decides which differences are expected), 2 if an archive
cannot be read or holds no files: a comparison that sees nothing proves nothing.
"""
import argparse
import hashlib
import posixpath
import sys
import tarfile


def clean(name):
    return posixpath.normpath(name).lstrip("/")


def entries(path):
    """Return {path: (type, mode, uid, gid, link target, sha256 of the content)} for every member of a tarball."""
    found = {}
    with tarfile.open(path) as tar:
        for member in tar:
            digest = ""
            if member.isreg():
                sha = hashlib.sha256()
                source = tar.extractfile(member)
                for block in iter(lambda: source.read(1 << 20), b""):
                    sha.update(block)
                digest = sha.hexdigest()
            link = clean(member.linkname) if member.islnk() else member.linkname
            found[clean(member.name)] = (member.type, member.mode, member.uid, member.gid, link, digest)
    if not found:
        raise ValueError(f"{path} holds no files")
    return found


def compare(old, new):
    """Return the sorted (path, word) differences between two entries() results."""
    out = []
    for name in sorted(set(old) | set(new)):
        if name not in old:
            out.append((name, "added"))
        elif name not in new:
            out.append((name, "removed"))
        elif old[name] != new[name]:
            out.append((name, "changed"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("old")
    ap.add_argument("new")
    args = ap.parse_args(argv)
    try:
        differences = compare(entries(args.old), entries(args.new))
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"ROOTFS DIFF ERROR: {exc}")
        return 2
    for name, word in differences:
        print(f"{word} {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
