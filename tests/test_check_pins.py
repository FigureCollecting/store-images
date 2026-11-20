"""Tests for scripts/check_pins.py: every base image is pinned by digest, every download by checksum."""
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import check_pins  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
D = "sha256:" + "a" * 64
PINNED = f"ARG BASE=docker.io/grafana/grafana:13.3.1@{D}\nFROM ${{BASE}} AS grafana\nUSER 472\n"


class DockerfileTest(unittest.TestCase):
    def problems(self, text):
        return check_pins.check_dockerfile(text)

    def test_pinned_from_passes(self):
        self.assertEqual(self.problems(f"FROM docker.io/grafana/grafana:13.3.1@{D}\n"), [])
        self.assertEqual(self.problems(PINNED), [])

    def test_unpinned_from_fails(self):
        for ref in ("grafana/grafana", "docker.io/grafana/grafana:13.3.1", "docker.io/grafana/grafana:latest",
                    "grafana/grafana@sha256:abc", f"grafana/grafana@sha256:{'A' * 64}",
                    f"grafana/grafana@sha512:{'a' * 64}", f"grafana/grafana@{D}0"):
            with self.subTest(ref=ref):
                got = self.problems(f"FROM {ref}\n")
                self.assertEqual(len(got), 1, got)
                self.assertIn(f"line 1: FROM {ref} is not pinned by digest", got[0])

    def test_unpinned_arg_default_fails(self):
        got = self.problems("ARG BASE=docker.io/grafana/grafana:13.3.1\nFROM ${BASE} AS grafana\n")
        self.assertEqual(len(got), 1, got)
        self.assertIn("line 2: FROM docker.io/grafana/grafana:13.3.1 is not pinned by digest", got[0])

    def test_arg_without_default_fails(self):
        for text in ("ARG BASE\nFROM ${BASE}\n", "FROM $BASE\n", f"ARG B=x@{D}\nFROM ${{B:-grafana/grafana}}\n",
                     f"ARG B=x@{D}\nARG B\nARG B=\nFROM $B\n", "ARG A\nARG B=$A\nFROM $B\n",
                     f"FROM x@{D} AS a\nCOPY --from=$A /a /a\n", f"FROM x@{D}\nADD $U /a\n"):
            with self.subTest(text=text):
                got = self.problems(text)
                self.assertEqual(len(got), 1, got)
                self.assertIn("cannot be resolved from ARG defaults", got[0])

    def test_a_digest_supplied_by_an_arg_default_is_pinned(self):
        self.assertEqual(self.problems(f"ARG SUM={D}\nARG REF=\"grafana/grafana@$SUM\"\nFROM ${{REF}}\n"), [])
        got = self.problems("ARG SUM=sha256:abc\nFROM grafana/grafana@${SUM}\n")
        self.assertIn("line 2: FROM grafana/grafana@sha256:abc is not pinned by digest", got[0])

    def test_heredoc_bodies_are_not_instructions_and_an_open_one_fails(self):
        text = (f"FROM x@{D}\n"
                "RUN <<EOF\nFROM busybox\nADD https://example.org/x /x\nEOF\n"
                "RUN cat <<-'END' >/f && test $((1 << 2)) = 4 && cat <<<\"FROM y\"\nFROM busybox\nEND\n"
                f"FROM x@{D}\n")
        self.assertEqual(self.problems(text), [])
        got = self.problems(f"FROM x@{D}\nRUN <<EOF\necho\nFROM busybox:latest\n")
        self.assertEqual(got, ["line 2: heredoc EOF is never closed, the rest of the file is unchecked"])
        got = self.problems(f"FROM x@{D}\nRUN <<EOF\necho\nEOF\nFROM busybox:latest\n")
        self.assertIn("line 5: FROM busybox:latest is not pinned by digest", got[0])

    def test_a_later_arg_default_wins(self):
        got = self.problems(f"ARG BASE=grafana/grafana@{D}\nARG BASE=grafana/grafana:latest\nFROM $BASE\n")
        self.assertIn("line 3: FROM grafana/grafana:latest is not pinned by digest", got[0])

    def test_stage_names_scratch_and_flags(self):
        text = (f"FROM --platform=linux/amd64 golang:1.27.3@{D} AS Build\n"
                "FROM build AS mid\n"
                "FROM scratch\n"
                "COPY --from=mid --chown=0:0 /a /a\n"
                "COPY --from=0 /b /b\n"
                "COPY /c /c\n")
        self.assertEqual(self.problems(text), [])

    def test_a_stage_named_after_its_use_is_not_a_stage(self):
        got = self.problems(f"FROM later\nFROM x@{D} AS later\n")
        self.assertEqual(len(got), 1, got)
        self.assertIn("line 1: FROM later is not pinned by digest", got[0])

    def test_copy_from_an_unpinned_image_fails(self):
        got = self.problems(f"FROM x@{D}\nCOPY --from=busybox:1.37 /bin/sh /sh\n")
        self.assertEqual(len(got), 1, got)
        self.assertIn("line 2: COPY --from=busybox:1.37 is not pinned by digest", got[0])
        self.assertEqual(self.problems(f"FROM x@{D}\nCOPY --from=busybox@{D} /bin/sh /sh\n"), [])

    def test_run_mount_from_an_unpinned_image_fails(self):
        got = self.problems(f"FROM x@{D}\nRUN --mount=type=bind,from=busybox:1.37,target=/b true\n")
        self.assertEqual(len(got), 1, got)
        self.assertIn("line 2: RUN --mount from=busybox:1.37 is not pinned by digest", got[0])
        self.assertEqual(self.problems(f"FROM x@{D} AS a\nFROM a\nRUN --mount=type=bind,from=a,target=/b true\n"), [])

    def test_remote_add_needs_a_sha256_checksum(self):
        url = "https://github.com/o/r/releases/download/v1/p.zip"
        got = self.problems(f"FROM x@{D}\nADD {url} /tmp/p.zip\n")
        self.assertEqual(len(got), 1, got)
        self.assertIn(f"line 2: ADD {url} has no --checksum=sha256:<64 hex>", got[0])
        got = self.problems(f"FROM x@{D}\nADD --checksum=sha256:abc {url} /tmp/p.zip\n")
        self.assertEqual(len(got), 1, got)
        self.assertEqual(self.problems(f"FROM x@{D}\nADD --checksum={D} --chmod=0444 {url} /tmp/p.zip\n"), [])
        self.assertEqual(self.problems(f"FROM x@{D}\nADD local.tar /opt/\n"), [])

    def test_remote_add_through_an_arg_is_resolved(self):
        url = "https://example.org/p.zip"
        text = f"ARG URL={url}\nARG SUM={D}\nFROM x@{D}\nARG URL SUM\nADD --checksum=${{SUM}} ${{URL}} /p.zip\n"
        self.assertEqual(self.problems(text), [])
        got = self.problems(f"ARG URL={url}\nFROM x@{D}\nARG URL\nADD ${{URL}} /p.zip\n")
        self.assertIn(f"line 4: ADD {url} has no --checksum", got[0])
        got = self.problems(f"FROM x@{D}\nARG SUM\nADD --checksum=${{SUM}} {url} /p.zip\n")
        self.assertIn(f"line 3: ADD {url} has no --checksum", got[0])

    def test_continuations_comments_and_case(self):
        text = ("# FROM grafana/grafana:latest\n"
                "\n"
                f"from \\\n  x@{D} \\\n  as base\n"
                "RUN echo FROM grafana/grafana:latest \\\n"
                "    # a comment inside a continued command\n"
                " && echo done\n"
                "FROM base\n")
        self.assertEqual(self.problems(text), [])
        got = self.problems("RUN true \\\n && true\nFROM \\\n  busybox\n")
        self.assertIn("line 3: FROM busybox is not pinned by digest", got[0])

    def test_no_from_at_all_is_a_problem(self):
        self.assertEqual(self.problems("# nothing\nARG A=1\n"), ["no FROM instruction"])
        self.assertEqual(self.problems("FROM\n"), ["line 1: FROM names no image"])

    def test_a_stage_index_must_name_an_earlier_stage(self):
        self.assertEqual(self.problems(f"FROM x@{D}\nFROM x@{D}\nCOPY --from=1 /a /a\n"), [])
        for ref in ("2", "7"):
            with self.subTest(ref=ref):
                got = self.problems(f"FROM x@{D}\nFROM x@{D}\nCOPY --from={ref} /a /a\n")
                self.assertEqual(got, [f"line 3: COPY --from={ref} is not pinned by digest (@sha256:<64 hex>)"])
        got = self.problems(f"FROM x@{D}\nRUN --mount=type=bind,from=1,target=/b true\n")
        self.assertEqual(got, ["line 2: RUN --mount from=1 is not pinned by digest (@sha256:<64 hex>)"])

    def test_a_here_string_is_not_a_heredoc(self):
        got = self.problems(f"FROM x@{D}\nRUN cat <<<EOF\nFROM busybox:latest\nEOF\n")
        self.assertEqual(got, ["line 3: FROM busybox:latest is not pinned by digest (@sha256:<64 hex>)"])
        got = self.problems(f"FROM x@{D}\nRUN cat <<<\"$x\" && cat <<<word\nFROM busybox:latest\n")
        self.assertEqual(got, ["line 3: FROM busybox:latest is not pinned by digest (@sha256:<64 hex>)"])

    def test_the_syntax_directive_names_an_image_and_must_be_pinned(self):
        for line in ("# syntax=docker/dockerfile:1", "#syntax = docker/dockerfile:1.7", "# SYNTAX=docker/dockerfile",
                     "#   Syntax=docker.io/docker/dockerfile:1@sha256:abc"):
            with self.subTest(line=line):
                got = self.problems(f"{line}\nFROM x@{D}\n")
                self.assertEqual(len(got), 1, got)
                self.assertRegex(got[0], r"^line 1: # syntax=\S+ is not pinned by digest")
        self.assertEqual(self.problems(f"# syntax=docker/dockerfile:1@{D}\n# escape=\\\nFROM x@{D}\n"), [])
        self.assertEqual(self.problems(f"# escape=`\n# syntax=docker/dockerfile:1\nFROM x@{D}\n")[0][:16], "line 2: # syntax")

    def test_a_syntax_line_after_the_directives_is_only_a_comment(self):
        for text in (f"# a comment\n# syntax=docker/dockerfile:1\nFROM x@{D}\n",
                     f"\n# syntax=docker/dockerfile:1\nFROM x@{D}\n",
                     f"FROM x@{D}\n# syntax=docker/dockerfile:1\n",
                     f"# check=skip=all\n# about syntax=x\nFROM x@{D}\n"):
            with self.subTest(text=text):
                self.assertEqual(self.problems(text), [])

    def test_a_byte_order_mark_does_not_hide_the_syntax_directive(self):
        got = self.problems(f"\ufeff# syntax=docker/dockerfile:1\nFROM x@{D}\n")
        self.assertEqual(got, ["line 1: # syntax=docker/dockerfile:1 is not pinned by digest (@sha256:<64 hex>)"])
        self.assertEqual(self.problems(f"\ufeff# syntax=docker/dockerfile:1@{D}\nFROM x@{D}\n"), [])

    def test_a_file_of_directives_only_has_no_frontend_and_no_from(self):
        self.assertEqual(self.problems("# escape=`\n# check=skip=all\n"), ["no FROM instruction"])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, text):
        with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_main(self, root=None):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = check_pins.main(["--root", root or self.tmp.name])
        return rc, out.getvalue()

    def test_pinned_files_pass(self):
        self.write("Dockerfile", PINNED)
        self.write("Dockerfile.grafana", PINNED)
        self.write("Dockerfile.md", "FROM here on, prose\n")  # still a Dockerfile.* name: checked
        rc, out = self.run_main()
        self.assertEqual(rc, 1, out)
        os.remove(os.path.join(self.tmp.name, "Dockerfile.md"))
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("Dockerfile: 1 FROM, pinned", out)
        self.assertIn("Dockerfile.grafana: 1 FROM, pinned", out)

    def test_one_unpinned_file_fails_and_names_it(self):
        self.write("Dockerfile", PINNED)
        self.write("Dockerfile.grafana", "FROM grafana/grafana:13.3.1\nUSER 472\n")
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("::error file=Dockerfile.grafana::Dockerfile.grafana: line 1: FROM grafana/grafana:13.3.1 "
                      "is not pinned by digest", out)
        self.assertIn("Dockerfile: 1 FROM, pinned", out)

    def test_a_dockerfile_named_by_an_image_definition_is_checked_whatever_its_name(self):
        self.write("Dockerfile", PINNED)
        os.makedirs(os.path.join(self.tmp.name, "images"))
        os.makedirs(os.path.join(self.tmp.name, "recipes"))
        self.write("images/grafana.env", "# x\nAPP_NAME=grafana\nDOCKERFILE=recipes/grafana.build\n")
        self.write("images/other.env", "APP_NAME=other\n")
        self.write("recipes/grafana.build", "FROM grafana/grafana:13.3.1\n")
        rc, out = self.run_main()
        self.assertEqual(rc, 1, out)
        self.assertIn("::error file=recipes/grafana.build::recipes/grafana.build: line 1: FROM grafana/grafana:13.3.1 "
                      "is not pinned by digest", out)
        self.write("recipes/grafana.build", PINNED)
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("recipes/grafana.build: 1 FROM, pinned", out)
        self.assertEqual(out.count("Dockerfile: 1 FROM, pinned"), 1)

    def test_a_definition_naming_a_missing_dockerfile_is_an_error(self):
        self.write("Dockerfile", PINNED)
        os.makedirs(os.path.join(self.tmp.name, "images"))
        self.write("images/grafana.env", "DOCKERFILE=Dockerfile.gone\n")
        rc, out = self.run_main()
        self.assertEqual(rc, 2, out)
        self.assertIn("::error file=Dockerfile.gone::Dockerfile.gone: cannot read", out)

    def test_other_common_dockerfile_names_are_found(self):
        self.write("Dockerfile", PINNED)
        for name in ("grafana.Dockerfile", "x.dockerfile", "Containerfile", "Containerfile.grafana", "dockerfile"):
            with self.subTest(name=name):
                self.write(name, "FROM grafana/grafana:13.3.1\n")
                rc, out = self.run_main()
                self.assertEqual(rc, 1, out)
                self.assertIn(f"::error file={name}::{name}: line 1: FROM grafana/grafana:13.3.1 is not pinned", out)
                os.remove(os.path.join(self.tmp.name, name))
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)

    def test_a_directory_named_like_a_definition_is_skipped(self):
        self.write("Dockerfile", PINNED)
        os.makedirs(os.path.join(self.tmp.name, "images", "odd.env"))
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)

    def test_a_definition_that_names_the_default_dockerfile_does_not_check_it_twice(self):
        self.write("Dockerfile", PINNED)
        os.makedirs(os.path.join(self.tmp.name, "images"))
        self.write("images/a.env", "DOCKERFILE=Dockerfile\n")
        self.write("images/b.env", "DOCKERFILE=./Dockerfile\n")
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count("FROM, pinned"), 1)

    def test_no_dockerfiles_is_an_error(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 2)
        self.assertIn("no Dockerfile", out)

    def test_unreadable_dockerfile_is_an_error(self):
        os.mkdir(os.path.join(self.tmp.name, "Dockerfile"))
        rc, out = self.run_main()
        self.assertEqual(rc, 2)
        self.assertIn("::error file=Dockerfile::Dockerfile: cannot read", out)


class RepositoryTest(unittest.TestCase):
    """The recipes in this repository, as committed."""

    def read(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
            return fh.read()

    def test_every_dockerfile_in_the_repository_is_pinned(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = check_pins.main(["--root", ROOT])
        self.assertEqual(rc, 0, out.getvalue())
        # Both recipes, every stage: victoria-traces (upstream, build, runtime, target) and grafana (7 stages).
        self.assertEqual(out.getvalue().splitlines(),
                         ["Dockerfile: 4 FROM, pinned", "Dockerfile.grafana: 7 FROM, pinned"])

    def test_unpinning_any_image_of_the_victoria_traces_recipe_is_caught(self):
        text = self.read("Dockerfile")
        for arg in ("UPSTREAM_IMAGE", "GO_IMAGE", "ROOT_IMAGE"):
            with self.subTest(arg=arg):
                unpinned, n = re.subn(rf"(?m)^(ARG {arg}=\S+?)@sha256:[0-9a-f]{{64}}$", r"\1", text)
                self.assertEqual(n, 1)
                got = check_pins.check_dockerfile(unpinned)
                self.assertTrue(got and all("is not pinned by digest" in p for p in got), got)

    def test_the_grafana_recipe_starts_from_upstream_grafana_by_digest(self):
        text = self.read("Dockerfile.grafana")
        self.assertEqual(check_pins.check_dockerfile(text), [])
        self.assertRegex(text, r"(?m)^ARG GRAFANA_IMAGE=docker\.io/grafana/grafana:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")
        self.assertRegex(text, r"(?m)^FROM \$\{GRAFANA_IMAGE\} AS ")

    def test_unpinning_the_grafana_base_is_caught(self):
        text = self.read("Dockerfile.grafana")
        unpinned, n = re.subn(r"(?m)^(ARG GRAFANA_IMAGE=\S+?)@sha256:[0-9a-f]{64}$", r"\1", text)
        self.assertEqual(n, 1)
        got = check_pins.check_dockerfile(unpinned)
        self.assertTrue(got and all("is not pinned by digest" in p for p in got), got)

    def test_the_grafana_tag_matches_the_image_definition(self):
        text = self.read("Dockerfile.grafana")
        tag = [line.split("=", 1)[1] for line in self.read("images/grafana.env").splitlines()
               if line.startswith("UPSTREAM_TAG=")]
        self.assertEqual(len(tag), 1)
        self.assertIn(f"ARG GRAFANA_IMAGE=docker.io/grafana/grafana:{tag[0].removeprefix('v')}@sha256:", text)


if __name__ == "__main__":
    unittest.main()
