#!/usr/bin/env python3
"""Fail if a Dockerfile in this repository uses an image or a download that is not pinned.

Every image a build reads (FROM, COPY --from, RUN --mount from=, the `# syntax=` frontend) must end in
@sha256:<64 hex>, unless it is an earlier build stage or scratch. Every remote ADD must carry
--checksum=sha256:<64 hex>. Variables are resolved from ARG defaults only; a reference that cannot be
resolved is a failure. Checked: files named Dockerfile*, *.Dockerfile or Containerfile* anywhere in the
tree, and whatever file an images/*.env names as its DOCKERFILE.
Exit 1 if anything is unpinned, 2 if there is no Dockerfile or one cannot be read, 0 otherwise.
"""
import argparse
import glob
import os
import re
import sys

DIGEST_SUFFIX = re.compile(r"@sha256:[0-9a-f]{64}$")
CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
REMOTE = re.compile(r"^(?:https?://|git://|git@)")
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*[\"']?(\w+)")
DIRECTIVE = re.compile(r"^#\s*([a-zA-Z][a-zA-Z0-9]*)\s*=\s*(.+?)\s*$")
BOM = chr(0xFEFF)
NAMES = ("Dockerfile*", "dockerfile*", "*.Dockerfile", "*.dockerfile", "Containerfile*")


def instructions(text):
    """Yield (line number, instruction, arguments) with continuations joined; comments and heredoc bodies skipped."""
    pending, start, until, opened = "", 0, None, 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if until:
            until = None if line == until else until
            continue
        if not line or line.startswith("#"):
            continue
        start = start or number
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        words = (pending + line).split()
        heredoc = HEREDOC.search(pending + line)
        until, opened = (heredoc.group(1), start) if heredoc else (None, 0)
        yield start, words[0].upper(), words[1:]
        pending, start = "", 0
    if until:
        yield opened, "UNCLOSED", [until]


def syntax_directive(text):
    """Return (line number, image) of a `# syntax=` parser directive, or None.

    BuildKit pulls that image to parse the file. Directives are the `# key=value` lines at the very top;
    the first line of any other shape ends them, so a later `# syntax=` is an ordinary comment.
    BuildKit ignores a byte-order mark in front of the first line, so it is ignored here too.
    """
    for number, raw in enumerate(text.removeprefix(BOM).splitlines(), 1):
        directive = DIRECTIVE.match(raw)
        if not directive:
            return None
        if directive.group(1).lower() == "syntax":
            return number, directive.group(2)
    return None


def expand(value, args):
    """Substitute $NAME and ${NAME} from ARG defaults; None if anything is left unresolved."""
    out = VARIABLE.sub(lambda m: args.get(m.group(1) or m.group(2)) or "$", value)
    return None if "$" in out else out


def split_flags(words):
    flags = [w for w in words if w.startswith("--")]
    return flags, [w for w in words if not w.startswith("--")]


def flag_values(flags, name):
    return [f.split("=", 1)[1] for f in flags if f.startswith(f"--{name}=")]


def check_dockerfile(text):
    """Return the list of problems in one Dockerfile; empty if every image and download is pinned."""
    problems, args, stages, froms = [], {}, [], 0

    def image(line, what, raw):
        ref = expand(raw, args)
        if ref is None:
            problems.append(f"line {line}: {what}{raw} cannot be resolved from ARG defaults")
        elif ref.lower() not in stages and not (ref.isdigit() and int(ref) < froms) and not DIGEST_SUFFIX.search(ref):
            problems.append(f"line {line}: {what}{ref} is not pinned by digest (@sha256:<64 hex>)")

    frontend = syntax_directive(text)
    if frontend:
        image(frontend[0], "# syntax=", frontend[1])
    for line, name, words in instructions(text):
        flags, rest = split_flags(words)
        if name == "ARG":
            for word in rest:
                key, eq, default = word.partition("=")
                value = expand(default.strip("\"'"), args) if eq else args.get(key)
                args.pop(key, None)
                if value:
                    args[key] = value
        elif name == "FROM":
            if not rest:
                problems.append(f"line {line}: FROM names no image")
                continue
            if rest[0].lower() != "scratch":
                image(line, "FROM ", rest[0])
            froms += 1
            stages.append(rest[2].lower() if len(rest) == 3 and rest[1].upper() == "AS" else "")
        elif name == "COPY":
            for ref in flag_values(flags, "from"):
                image(line, "COPY --from=", ref)
        elif name == "RUN":
            for mount in flag_values(flags, "mount"):
                for ref in (part[5:] for part in mount.split(",") if part.startswith("from=")):
                    image(line, "RUN --mount from=", ref)
        elif name == "ADD":
            sums = [expand(value, args) for value in flag_values(flags, "checksum")]
            pinned = bool(sums) and all(value and CHECKSUM.match(value) for value in sums)
            for raw in rest[:-1]:
                source = expand(raw, args)
                if source is None:
                    problems.append(f"line {line}: ADD {raw} cannot be resolved from ARG defaults")
                elif REMOTE.match(source) and not pinned:
                    problems.append(f"line {line}: ADD {source} has no --checksum=sha256:<64 hex>")
        elif name == "UNCLOSED":
            problems.append(f"line {line}: heredoc {words[0]} is never closed, the rest of the file is unchecked")
    return problems if froms or problems else ["no FROM instruction"]


def dockerfiles(root):
    """Every file to check: the usual names anywhere in the tree, plus any DOCKERFILE an image definition names."""
    found = {os.path.normpath(path) for pattern in NAMES
             for path in glob.glob(os.path.join(root, "**", pattern), recursive=True)}
    for definition in filter(os.path.isfile, glob.glob(os.path.join(root, "images", "*.env"))):
        with open(definition, encoding="utf-8", errors="replace") as fh:
            found.update(os.path.normpath(os.path.join(root, line.split("=", 1)[1].strip()))
                         for line in fh if line.startswith("DOCKERFILE="))
    return sorted(found)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=os.path.join(os.path.dirname(__file__), ".."))
    args = ap.parse_args(argv)
    paths = dockerfiles(args.root)
    if not paths:
        print(f"::error title=Pin check failed::no Dockerfile under {args.root}")
        return 2
    worst = 0
    for path in paths:
        name = os.path.relpath(path, args.root)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, ValueError) as exc:
            print(f"::error file={name}::{name}: cannot read: {exc}")
            worst = 2
            continue
        problems = check_dockerfile(text)
        for problem in problems:
            print(f"::error file={name}::{name}: {problem}")
        if problems:
            worst = max(worst, 1)
        else:
            print(f"{name}: {sum(1 for _, word, _ in instructions(text) if word == 'FROM')} FROM, pinned")
    return worst


if __name__ == "__main__":
    sys.exit(main())
