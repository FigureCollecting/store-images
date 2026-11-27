"""The VictoriaTraces rebuild pins the fixed Go and klauspost/compress, and the repository holds only what it builds."""
import glob
import io
import os
import re
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import upstream_watch  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
# The lowest versions that clear the findings of the scan gate on 2026-11-20:
# CVE-2026-97054 (Go stdlib, fixed in 1.27.4) and CVE-2026-116591 (klauspost/compress, fixed in v1.20.2).
GO_FLOOR = (1, 27, 4)
COMPRESS = "github.com/klauspost/compress"
COMPRESS_FLOOR = (1, 20, 2)


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def version(text):
    return tuple(int(x) for x in text.lstrip("v").split("."))


def definitions():
    return sorted(os.path.basename(p)[: -len(".env")] for p in glob.glob(os.path.join(ROOT, "images", "*.env")))


def dockerfile_of(name):
    """The Dockerfile an image definition builds from (scripts/build.sh: DOCKERFILE, default Dockerfile)."""
    return upstream_watch.read_env(os.path.join(ROOT, "images", f"{name}.env")).get("DOCKERFILE", "Dockerfile")


def current_grafana_releases():
    """Today's releases for every repository the definitions with a release feed watch (grafana, tailscale, node-npm
    and valkey; psql has none): each at the tag its recipe pins."""
    releases = {}
    for name in ("grafana", "tailscale", "node-npm", "valkey"):
        env = upstream_watch.read_env(os.path.join(ROOT, "images", f"{name}.env"))
        releases.update({upstream_watch.GITHUB.match(env[f"{key}_REPO"]).group(1): [{"tag_name": env[f"{key}_TAG"]}]
                         for key in upstream_watch.watched(env)})
    return releases


class Recorder:
    """A fetch (or read_gomod) stand-in that records its calls and answers from a dict."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, *args):
        self.calls.append(args if len(args) > 1 else args[0])
        return self.answers[args if len(args) > 1 else args[0]]


def stage(dockerfile, name):
    return dockerfile.split(f" AS {name}\n", 1)[1].split("\nFROM ", 1)[0]


DIRECTIVE = re.compile(r"^#\s*([a-zA-Z][a-zA-Z0-9]*)\s*=\s*(.+?)\s*$")
CONTINUATION = re.compile(r"\\[ \t]*$")


def instructions(text):
    """(parser directives, [(KEYWORD, arguments)]) as BuildKit's parser reads the text with the default escape:
    directives only at the top; comment lines dropped, also inside a continuation (indented ones too); empty
    continuation lines dropped; continuations joined; the first line's indentation stripped; the keyword
    upper-cased, since Dockerfile keywords are case-insensitive. Heredocs are not parsed (a test forbids them)."""
    lines = text.lstrip("﻿").splitlines()
    directives = {}
    while lines and DIRECTIVE.match(lines[0]):
        key, value = DIRECTIVE.match(lines.pop(0)).groups()
        directives[key.lower()] = value
    out, current = [], None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        current = raw.lstrip() if current is None else current + raw
        if CONTINUATION.search(current):
            current = CONTINUATION.sub("", current)
            continue
        out.append(split_instruction(current))
        current = None
    if current is not None:  # a continuation that runs into the end of the file still ends the instruction
        out.append(split_instruction(current))
    return directives, out


def split_instruction(line):
    keyword, *arguments = re.split(r"\s+", line.strip(), maxsplit=1)
    return keyword.upper(), "".join(arguments)


def parsed_stage(text, name):
    """The instructions of the stage named `name` (case-insensitive, as BuildKit names stages), FROM excluded."""
    found, ins = None, instructions(text)[1]
    for i, (keyword, arguments) in enumerate(ins):
        if keyword == "FROM":
            if found is not None:
                return ins[found:i]
            if re.search(rf"(?i)\sAS\s+{re.escape(name)}$", arguments):
                found = i + 1
    return ins[found:] if found is not None else None


# The whole build stage as reviewed: every instruction after `FROM ${GO_IMAGE} AS build`, compared after parsing,
# so a lower-case, indented or continued spelling of an extra ENV, ARG, SHELL or RUN is still an extra instruction.
BUILD_STAGE = r"""
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG APP_NAME UPSTREAM_REPO UPSTREAM_TAG UPSTREAM_COMMIT UPSTREAM_DESCRIBE UPSTREAM_GO_VERSION \
    UPSTREAM_PATCH_MODULE UPSTREAM_PATCH_VERSION GO_VERSION BUILD_DATE FC_REVISION
ENV GOTOOLCHAIN=local GOFLAGS=-mod=readonly \
    CGO_ENABLED=1 GOOS=linux GOARCH=amd64 GOAMD64=v1 CC=x86_64-linux-gnu-gcc
WORKDIR /src
RUN --network=none test "$(go env GOVERSION)" = "go${GO_VERSION}"
RUN git -c advice.detachedHead=false clone --quiet --depth 1 --branch "${UPSTREAM_TAG}" "${UPSTREAM_REPO}" . \
 && test "$(git rev-parse HEAD)" = "${UPSTREAM_COMMIT}" \
 && h="${UPSTREAM_DESCRIBE##*-g}" \
 && test "${UPSTREAM_DESCRIBE}" = "tags-${UPSTREAM_TAG}-0-g${h}" \
 && test "$(git rev-parse --short="${#h}" HEAD)" = "${h}"
COPY patches/${APP_NAME}.patch /tmp/deps.patch
RUN --network=none git apply --check /tmp/deps.patch && git apply /tmp/deps.patch \
 && test "$(git diff --numstat | tr '\t\n' '  ')" = "1 1 go.mod 2 2 go.sum " \
 && grep -qxP "\t${UPSTREAM_PATCH_MODULE} ${UPSTREAM_PATCH_VERSION}" go.mod
RUN go mod download
COPY --from=upstream /${APP_NAME}-prod /tmp/upstream
RUN --network=none GOPROXY=off go build -trimpath -buildvcs=false \
      -ldflags "-extldflags '-static' -X 'github.com/VictoriaMetrics/VictoriaMetrics/lib/buildinfo.Version=${APP_NAME}-${BUILD_DATE}-${UPSTREAM_DESCRIBE}-${FC_REVISION}'" \
      -tags 'netgo osusergo' \
      -o "/out/${APP_NAME}-prod" "./app/${APP_NAME}" \
 && go version -m "/out/${APP_NAME}-prod" >/tmp/buildinfo && go version -m /tmp/upstream >/tmp/upstream-buildinfo \
 && test "$(head -n 1 /tmp/buildinfo)" = "/out/${APP_NAME}-prod: go${GO_VERSION}" \
 && test "$(head -n 1 /tmp/upstream-buildinfo)" = "/tmp/upstream: go${UPSTREAM_GO_VERSION}" \
 && grep -qP "^\tdep\t${UPSTREAM_PATCH_MODULE}\t${UPSTREAM_PATCH_VERSION}\t" /tmp/buildinfo \
 && grep -cP "^\tdep\t${UPSTREAM_PATCH_MODULE}\t" /tmp/upstream-buildinfo | grep -qx 1 \
 && norm() { tail -n +2 "$1" | grep -vP "^\tdep\t${UPSTREAM_PATCH_MODULE}\t" | cut -f1-4; } \
 && diff <(norm /tmp/upstream-buildinfo) <(norm /tmp/buildinfo) \
 && sections() { readelf -SW "$1" | grep -oP '^\s+\[\s*\d+\]\s\K\S+'; } \
 && sections /tmp/upstream >/tmp/upstream-sections && sections "/out/${APP_NAME}-prod" >/tmp/sections \
 && grep -qx .symtab /tmp/upstream-sections \
 && diff /tmp/upstream-sections /tmp/sections \
 && segments() { readelf -lW "$1" | awk '/^ +[A-Z_]+ +0x/ { f = ""; for (i = 7; i < NF; i++) f = f $i; print $1, f }'; } \
 && segments /tmp/upstream >/tmp/upstream-segments && segments "/out/${APP_NAME}-prod" >/tmp/segments \
 && grep -qx 'GNU_STACK RW' /tmp/upstream-segments \
 && diff /tmp/upstream-segments /tmp/segments \
 && test "$(git status --porcelain --untracked-files=all --ignored | tr '\n' ' ')" = " M go.mod  M go.sum "
"""


class DefinitionSetsEveryBuildArgTest(unittest.TestCase):
    # scripts/build.sh passes only the keys a definition sets, so a missing one would silently build another image.
    SUPPLIED_BY_BUILD_SH = {"FC_COMMIT", "BUILD_DATE", "CREATED"}

    def test_every_arg_without_a_default_is_set_by_the_definition(self):
        for name in definitions():
            env = upstream_watch.read_env(os.path.join(ROOT, "images", f"{name}.env"))
            text = read(dockerfile_of(name)).replace("\\\n", " ")
            declared, defaulted = set(), set()
            for match in re.finditer(r"(?im)^\s*ARG\s+(.+)$", text):
                for word in match.group(1).split():
                    (defaulted if "=" in word else declared).add(word.split("=", 1)[0])
            needed = declared - defaulted - self.SUPPLIED_BY_BUILD_SH
            self.assertTrue(needed, name)
            self.assertEqual(sorted(needed - set(env)), [], f"images/{name}.env does not set these ARGs")


class StaysWordingTest(unittest.TestCase):
    def test_the_rebuild_sentence_reads_as_plain_english(self):
        self.assertEqual(upstream_watch.stays(["a", "b", "c"]), "The a, b and c rebuilds stay.")
        self.assertEqual(upstream_watch.stays(["a", "b"]), "The a and b rebuilds stay.")
        self.assertEqual(upstream_watch.stays(["a"]), "The a rebuild stays.")
        self.assertEqual(upstream_watch.stays([]), "The images/ rebuild stays.")


class TracesPinsTest(unittest.TestCase):
    def setUp(self):
        self.env = upstream_watch.read_env(os.path.join(ROOT, "images", "victoria-traces.env"))

    def test_the_base_is_the_release_the_live_store_runs(self):
        self.assertEqual(self.env["UPSTREAM_TAG"], "v0.13.0")
        self.assertEqual(self.env["UPSTREAM_COMMIT"], "f0295d1f796d2c7880ee2fdaf899d0ba754f4d8a")
        self.assertEqual(self.env["UPSTREAM_DESCRIBE"], "tags-v0.13.0-0-gf0295d1f7")
        self.assertRegex(self.env["UPSTREAM_IMAGE"],
                         r"^docker\.io/victoriametrics/victoria-traces:v0\.13\.0@sha256:[0-9a-f]{64}$")
        self.assertEqual(self.env["UPSTREAM_GO_VERSION"], "1.27.3")

    def test_go_is_at_or_above_the_fixed_patch_release_and_the_builder_matches(self):
        go = self.env["GO_VERSION"]
        self.assertGreaterEqual(version(go), GO_FLOOR)
        self.assertGreater(version(go), version(self.env["UPSTREAM_GO_VERSION"]))
        self.assertRegex(read("Dockerfile"),
                         rf"(?m)^ARG GO_IMAGE=docker\.io/library/golang:{re.escape(go)}@sha256:[0-9a-f]{{64}}$")

    def test_compress_is_at_or_above_the_fixed_version(self):
        self.assertEqual(self.env["UPSTREAM_PATCH_MODULE"], COMPRESS)
        self.assertGreaterEqual(version(self.env["UPSTREAM_PATCH_VERSION"]), COMPRESS_FLOOR)

    def test_the_patch_moves_exactly_compress_in_go_mod_and_go_sum(self):
        patch = read("patches/victoria-traces.patch")
        want = self.env["UPSTREAM_PATCH_VERSION"]
        added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
        removed = [line[1:] for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")]
        self.assertEqual(re.findall(r"(?m)^diff --git a/(\S+) ", patch), ["go.mod", "go.sum"])
        self.assertEqual(len(added), 3, added)
        self.assertEqual(len(removed), 3, removed)
        self.assertEqual(added[0], f"\t{COMPRESS} {want}")
        self.assertRegex(added[1], rf"^{re.escape(COMPRESS)} {re.escape(want)} h1:[A-Za-z0-9+/]{{43}}=$")
        self.assertRegex(added[2], rf"^{re.escape(COMPRESS)} {re.escape(want)}/go\.mod h1:[A-Za-z0-9+/]{{43}}=$")
        for line in removed:
            self.assertTrue(line.strip().startswith(COMPRESS + " "), line)
            self.assertNotIn(want, line)


class BuildGuardsTest(unittest.TestCase):
    def setUp(self):
        self.text = read("Dockerfile")
        self.build = stage(self.text, "build")

    def test_the_build_checks_the_toolchain_and_the_patch_footprint(self):
        self.assertIn('RUN --network=none test "$(go env GOVERSION)" = "go${GO_VERSION}"', self.build)
        self.assertIn("git apply --check /tmp/deps.patch && git apply /tmp/deps.patch", self.build)
        self.assertIn('test "$(git diff --numstat | tr \'\\t\\n\' \'  \')" = "1 1 go.mod 2 2 go.sum "', self.build)
        self.assertIn('grep -qxP "\\t${UPSTREAM_PATCH_MODULE} ${UPSTREAM_PATCH_VERSION}" go.mod', self.build)

    def test_the_build_is_offline_and_from_the_module_cache(self):
        self.assertIn("GOFLAGS=-mod=readonly", self.build)
        self.assertNotIn("-mod=vendor", self.build)
        self.assertRegex(self.build, r"(?m)^RUN go mod download$")
        self.assertIn("RUN --network=none GOPROXY=off go build ", self.build)

    def test_the_go_build_command_is_upstreams_v0_13_0_linux_build(self):
        # -trimpath hides -ldflags, CC and CGO_*FLAGS from `go version -m`, so only this pin sees them
        # (a stripped `-s -w` build passed every other check). Upstream's Makefile, app-via-docker, linux:
        # go build -trimpath -buildvcs=false -ldflags "-extldflags '-static' $(GO_BUILDINFO)" -tags 'netgo osusergo'
        # The pin runs through the package path: a second -ldflags after -o would win (Go uses the last one).
        match = re.search(r"(?s)RUN --network=none GOPROXY=off (go build .*?\"\./app/\$\{APP_NAME\}\")", self.build)
        self.assertIsNotNone(match, "no offline go build of ./app/${APP_NAME} in the build stage")
        self.assertEqual(re.sub(r"\s*\\\n\s*", " ", match.group(1)).strip(),
                         "go build -trimpath -buildvcs=false "
                         "-ldflags \"-extldflags '-static' -X 'github.com/VictoriaMetrics/VictoriaMetrics/lib/buildinfo.Version="
                         "${APP_NAME}-${BUILD_DATE}-${UPSTREAM_DESCRIBE}-${FC_REVISION}'\" "
                         "-tags 'netgo osusergo' -o \"/out/${APP_NAME}-prod\" \"./app/${APP_NAME}\"")

    def test_the_dockerfile_has_no_parser_directive_heredoc_or_onbuild(self):
        # The instruction pins below read the file as BuildKit does only without these: `# escape=` changes the
        # continuation character, `# syntax=` swaps the parser, a heredoc hides lines from the instruction list,
        # and ONBUILD runs instructions in a later stage.
        directives, ins = instructions(self.text)
        self.assertEqual(directives, {})
        self.assertEqual([i for i in ins if "<<" in i[1] or i[0] == "ONBUILD"], [])

    def test_the_stages_are_exactly_the_reviewed_from_sequence(self):
        # No stage can be slipped in under `build` (a parent stage's ENV is inherited by the build).
        self.assertEqual([a for k, a in instructions(self.text)[1] if k == "FROM"],
                         ["${UPSTREAM_IMAGE} AS upstream", "${GO_IMAGE} AS build", "${ROOT_IMAGE} AS runtime",
                          "runtime AS victoria-traces"])

    def test_the_build_stage_is_exactly_the_reviewed_instruction_list(self):
        # -trimpath hides -ldflags, CC and CGO_*FLAGS from `go version -m`: an extra ENV or ARG (CGO_LDFLAGS=-s
        # strips, -Wl,-z,execstack makes the stack executable), a SHELL prefix, or a RUN `go env -w` changes the
        # binary without changing its buildinfo, in any spelling BuildKit accepts (lower-case, indented, continued).
        self.assertEqual(parsed_stage(self.text, "build"), instructions(BUILD_STAGE)[1])

    def test_the_build_fails_unless_the_program_headers_match_upstreams_binary(self):
        # Flags that keep the section names can still change the segments: -z execstack makes GNU_STACK RWE.
        self.assertIn("segments() { readelf -lW \"$1\" | awk '/^ +[A-Z_]+ +0x/ { f = \"\"; "
                      "for (i = 7; i < NF; i++) f = f $i; print $1, f }'; }", self.build)
        self.assertIn("segments /tmp/upstream >/tmp/upstream-segments", self.build)
        self.assertIn("segments \"/out/${APP_NAME}-prod\" >/tmp/segments", self.build)
        self.assertIn("grep -qx 'GNU_STACK RW' /tmp/upstream-segments", self.build)
        self.assertIn("diff /tmp/upstream-segments /tmp/segments", self.build)

    def test_the_build_fails_unless_the_elf_sections_match_upstreams_binary(self):
        # A stripped binary (by any spelling: -s -w, CGO_LDFLAGS=-s) lacks .symtab and the .debug_* sections.
        self.assertIn("sections() { readelf -SW \"$1\" | grep -oP '^\\s+\\[\\s*\\d+\\]\\s\\K\\S+'; }", self.build)
        self.assertIn("sections /tmp/upstream >/tmp/upstream-sections", self.build)
        self.assertIn("sections \"/out/${APP_NAME}-prod\" >/tmp/sections", self.build)
        self.assertIn("grep -qx .symtab /tmp/upstream-sections", self.build)
        self.assertIn("diff /tmp/upstream-sections /tmp/sections", self.build)

    def test_the_binary_differs_from_upstream_only_in_go_and_the_patched_module(self):
        self.assertIn("COPY --from=upstream /${APP_NAME}-prod /tmp/upstream", self.build)
        for guard in ('test "$(head -n 1 /tmp/buildinfo)" = "/out/${APP_NAME}-prod: go${GO_VERSION}"',
                      'test "$(head -n 1 /tmp/upstream-buildinfo)" = "/tmp/upstream: go${UPSTREAM_GO_VERSION}"',
                      'grep -qP "^\\tdep\\t${UPSTREAM_PATCH_MODULE}\\t${UPSTREAM_PATCH_VERSION}\\t" /tmp/buildinfo',
                      'grep -cP "^\\tdep\\t${UPSTREAM_PATCH_MODULE}\\t" /tmp/upstream-buildinfo | grep -qx 1',
                      "diff <(norm /tmp/upstream-buildinfo) <(norm /tmp/buildinfo)",
                      # --ignored: a patch-added file at a git-ignored path (*.tmp in the embedded UI) is listed too
                      'test "$(git status --porcelain --untracked-files=all --ignored | tr \'\\n\' \' \')" = '
                      '" M go.mod  M go.sum "'):
            with self.subTest(guard=guard):
                self.assertIn(guard, self.build)

    def test_the_upstream_image_is_a_digest_pinned_stage(self):
        self.assertIn("FROM ${UPSTREAM_IMAGE} AS upstream\n", self.text)
        env = upstream_watch.read_env(os.path.join(ROOT, "images", "victoria-traces.env"))
        self.assertIn(f"\nARG UPSTREAM_IMAGE={env['UPSTREAM_IMAGE']}\n", self.text)  # the pin check reads defaults
        self.assertRegex(read("scripts/build.sh"), r"\bUPSTREAM_IMAGE\b")
        for key in ("UPSTREAM_GO_VERSION", "UPSTREAM_PATCH_MODULE", "UPSTREAM_PATCH_VERSION"):
            self.assertRegex(read("scripts/build.sh"), rf"\b{key}\b")
        self.assertNotIn("UPSTREAM_GO_FIX_COMMIT", read("scripts/build.sh") + self.text)


class InstructionParserTest(unittest.TestCase):
    def test_keywords_are_case_insensitive_and_indentation_is_ignored(self):
        self.assertEqual(instructions("FROM a AS build\nenv X=1\n  ENV Y=2\narg\tZ=3\n")[1],
                         [("FROM", "a AS build"), ("ENV", "X=1"), ("ENV", "Y=2"), ("ARG", "Z=3")])

    def test_continuations_join_and_skip_comment_and_empty_lines(self):
        self.assertEqual(instructions("RUN a \\  \n  # dropped\n\n && b\n# top-level comment\nRUN c\n")[1],
                         [("RUN", "a  && b"), ("RUN", "c")])

    def test_a_continuation_at_the_end_of_the_file_ends_the_instruction(self):
        self.assertEqual(instructions("RUN a \\\n")[1], [("RUN", "a")])

    def test_parser_directives_are_read_only_at_the_top(self):
        self.assertEqual(instructions("﻿# syntax=x/y\n# Escape = `\nFROM a\n# check=skip=all\n"),
                         ({"syntax": "x/y", "escape": "`"}, [("FROM", "a")]))
        self.assertEqual(instructions("# A comment\n# syntax=x/y\nFROM a\n")[0], {})

    def test_a_stage_is_found_by_its_case_insensitive_name(self):
        text = "FROM a AS gobase\nENV X=1\nfrom gobase as build\nRUN b\nFROM c AS runtime\n"
        self.assertEqual(parsed_stage(text, "build"), [("RUN", "b")])
        self.assertEqual(parsed_stage(text, "runtime"), [])
        self.assertIsNone(parsed_stage(text, "missing"))

    def test_every_s8_spelling_is_an_extra_build_stage_instruction(self):
        # chal3's S8 bypasses: each passed the earlier upper-case line regexes.
        good = read("Dockerfile")
        exe = "CGO_LDFLAGS=-Wl,-z,execstack"
        for name, old, new in (
                ("lower-case env", "WORKDIR /src\n", f"WORKDIR /src\nenv {exe}\n"),
                ("indented ENV", "WORKDIR /src\n", f"WORKDIR /src\n  ENV {exe}\n"),
                ("lower-case arg", "WORKDIR /src\n", f"WORKDIR /src\narg {exe}\n"),
                ("go env -w", "WORKDIR /src\n", f"WORKDIR /src\nRUN --network=none go env -w {exe}\n"),
                ("SHELL prefix", 'SHELL ["/bin/bash"', f'SHELL ["/usr/bin/env", "{exe}", "/bin/bash"'),
                ("continued ENV", "WORKDIR /src\n", f"WORKDIR /src\nENV \\\n  {exe}\n")):
            with self.subTest(name=name):
                bad = good.replace(old, new, 1)
                self.assertNotEqual(bad, good)
                self.assertNotEqual(parsed_stage(bad, "build"), instructions(BUILD_STAGE)[1])
        parent = good.replace("FROM ${GO_IMAGE} AS build\n",
                              f"FROM ${{GO_IMAGE}} AS gobase\nENV {exe}\nFROM gobase AS build\n", 1)
        self.assertIn("gobase AS build", [a for k, a in instructions(parent)[1] if k == "FROM"])


class InventoryTest(unittest.TestCase):
    def test_only_these_images_are_rebuilt_or_wrapped(self):
        # VictoriaLogs v1.54.0 ships Go 1.27.4 and compress v1.20.4 and passes the gate: the store runs upstream's image.
        self.assertEqual(definitions(), ["grafana", "node-npm", "psql", "tailscale", "valkey", "victoria-traces"])
        # This Dockerfile rebuilds only victoria-traces; every other image has its own recipe.
        self.assertEqual([n for n in definitions() if dockerfile_of(n) == "Dockerfile"], ["victoria-traces"])
        for name in ("grafana", "node-npm", "psql", "tailscale", "valkey"):
            self.assertEqual(dockerfile_of(name), f"Dockerfile.{name}")
        self.assertFalse(os.path.exists(os.path.join(ROOT, "patches", "victoria-logs.patch")))

    def test_every_workflow_matrix_and_dockerfile_target_matches_the_definitions(self):
        names = definitions()
        workflow = read(".github/workflows/images.yml")
        matrices = re.findall(r"(?m)^\s+app: \[([^\]]*)\]$", workflow)
        self.assertEqual(len(matrices), 2)
        for matrix in matrices:
            self.assertEqual(sorted(a.strip() for a in matrix.split(",")), names)
        targets = re.findall(r"(?m)^FROM runtime AS (\S+)$", read("Dockerfile"))
        self.assertEqual(sorted(targets), [n for n in names if dockerfile_of(n) == "Dockerfile"])
        # Every definition's target is the last stage of the Dockerfile it names, and no recipe is orphaned.
        for name in names:
            with self.subTest(name=name):
                froms = [a for k, a in instructions(read(dockerfile_of(name)))[1] if k == "FROM"]
                self.assertRegex(froms[-1], rf"(?i)\sAS\s+{re.escape(name)}$")
        recipes = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "Dockerfile*")))
        self.assertEqual(recipes, sorted({dockerfile_of(n) for n in names}))

    def test_victoria_logs_is_watched_but_never_rebuilt(self):
        # The store runs upstream's victoria-logs; a release newer than the pin must still fail the weekly watch.
        env = upstream_watch.read_env(os.path.join(ROOT, "watch", "victoria-logs.env"))
        self.assertEqual(env["UPSTREAM_TAG"], "v1.54.0")
        self.assertNotIn("victoria-logs", definitions())
        fetch = Recorder({"VictoriaMetrics/VictoriaTraces": [{"tag_name": "v0.13.0"}],
                          "VictoriaMetrics/VictoriaLogs": [{"tag_name": "v1.55.0"}, {"tag_name": "v1.54.0"}],
                          **current_grafana_releases()})
        out = io.StringIO()
        with redirect_stdout(out):
            rc = upstream_watch.main([], fetch=fetch, read_gomod=Recorder({}))
        self.assertEqual(rc, 1, out.getvalue())
        # The default run watches every repository of images/ (victoria-traces, grafana and its two data sources)
        # and of watch/ (victoria-logs).
        self.assertEqual(sorted(fetch.calls), sorted(["VictoriaMetrics/VictoriaTraces", "VictoriaMetrics/VictoriaLogs",
                                                      *current_grafana_releases()]))
        self.assertIn("Upstream released victoria-logs v1.55.0", out.getvalue())
        self.assertEqual(out.getvalue().count("::error"), 1, out.getvalue())
        # Archiving this repository on a victoria-logs release would drop the rebuilds the stores run.
        self.assertIn("victoria-logs is watch-only", out.getvalue())
        self.assertIn("bump UPSTREAM_TAG in watch/victoria-logs.env", out.getvalue())
        self.assertIn("The grafana, node-npm, psql, tailscale, valkey and victoria-traces rebuilds stay.",
                      out.getvalue())
        self.assertNotIn("archive this repository", out.getvalue())
        self.assertNotIn("Re-pin images/", out.getvalue())

    def test_a_rebuilt_image_with_a_newer_release_points_at_upstreams_image(self):
        fetch = Recorder({"VictoriaMetrics/VictoriaTraces": [{"tag_name": "v0.14.0"}], **current_grafana_releases()})
        read_gomod = Recorder({})
        out = io.StringIO()
        with redirect_stdout(out):
            rc = upstream_watch.main(["--images-dir", os.path.join(ROOT, "images")], fetch=fetch,
                                     read_gomod=read_gomod)
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("victoria-traces: base v0.13.0, upstream released v0.14.0. Check its image's Go version, "
                      "re-pin to upstream's image, and archive this repository.\n", out.getvalue())
        self.assertNotIn("watch-only", out.getvalue())
        self.assertEqual(out.getvalue().count("::error"), 1, out.getvalue())
        # victoria-traces also sets UPSTREAM_PATCH_MODULE, but it is archived on a release, not rebuilt: grafana's
        # drop-the-server-rebuild advice (which reads the release's go.mod) is not for it.
        self.assertEqual(read_gomod.calls, [])
        self.assertNotIn("server rebuild", out.getvalue())

    def test_the_weekly_watch_passes_against_todays_upstream_releases(self):
        fetch = Recorder({"VictoriaMetrics/VictoriaTraces": [{"tag_name": "v0.13.0"}, {"tag_name": "v0.12.1"}],
                          "VictoriaMetrics/VictoriaLogs": [{"tag_name": "v1.54.0"}, {"tag_name": "v1.53.0"}],
                          **current_grafana_releases()})
        out = io.StringIO()
        with redirect_stdout(out):
            rc = upstream_watch.main([], fetch=fetch, read_gomod=Recorder({}))
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("victoria-traces: base v0.13.0, newest upstream release v0.13.0: no newer release", out.getvalue())
        self.assertIn("victoria-logs: base v1.54.0, newest upstream release v1.54.0: no newer release", out.getvalue())
        env = upstream_watch.read_env(os.path.join(ROOT, "images", "grafana.env"))
        for key in ("UPSTREAM", "VLOGS_DS", "JAEGER_DS"):
            label = "grafana" if key == "UPSTREAM" else f"grafana {key}"
            with self.subTest(key=key):
                self.assertIn(f"{label}: base {env[key + '_TAG']}, newest upstream release {env[key + '_TAG']}: "
                              "no newer release", out.getvalue())
        tailscale = upstream_watch.read_env(os.path.join(ROOT, "images", "tailscale.env"))["UPSTREAM_TAG"]
        self.assertIn(f"tailscale: base {tailscale}, newest upstream release {tailscale}: no newer release",
                      out.getvalue())
        for name in ("node-npm", "valkey"):
            tag = upstream_watch.read_env(os.path.join(ROOT, "images", f"{name}.env"))["UPSTREAM_TAG"]
            self.assertIn(f"{name}: base {tag}, newest upstream release {tag}: no newer release", out.getvalue())
        self.assertIn("psql: no upstream release feed to watch; retire when ", out.getvalue())
        self.assertEqual(len(out.getvalue().splitlines()), 9, out.getvalue())


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.smoke = read("scripts/smoke.sh")

    def test_the_smoke_writes_a_gzip_otlp_span_and_reads_it_back(self):
        self.assertIn("/insert/opentelemetry/v1/traces", self.smoke)
        self.assertIn("Content-Encoding: gzip", self.smoke)
        self.assertIn("/select/jaeger/api/services", self.smoke)
        self.assertRegex(self.smoke, r'check write ')
        self.assertRegex(self.smoke, r'check query ')

    def test_the_smoke_checks_the_patched_module_in_the_binary(self):
        self.assertIn("dep[[:space:]]+${UPSTREAM_PATCH_MODULE}[[:space:]]+${UPSTREAM_PATCH_VERSION}[[:space:]]",
                      self.smoke)

    def test_the_check_helper_does_not_shadow_the_container_name(self):
        # post_gzip runs inside check and needs the global $name (the docker network); a local "name" hid it.
        body = self.smoke.split("check() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotRegex(body, r"\blocal\b[^\n]*\bname=")
        self.assertIn('--network "$name"', self.smoke.split("post_gzip() {", 1)[1].split("\n}\n", 1)[0])

    def test_the_smoke_network_has_no_way_out(self):
        self.assertIn('docker network create --internal "$name"', self.smoke)

    def test_the_smoke_object_names_can_be_prefixed(self):
        self.assertIn('name="${SMOKE_PREFIX:-store-images-smoke}-$app"', self.smoke)


if __name__ == "__main__":
    unittest.main()
