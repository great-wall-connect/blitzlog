"""Regression: a project's `bootstrap` task must be able to install its
own build dependencies inside the agent container.

Issue #174 ("the agent cannot bootstrap — the read-only rootfs breaks
the documented apt-based bootstrap task") is the follow-up to #164,
whose second slice gave the container a non-root uid and a read-only
root filesystem.

`docs/BOOTSTRAP.md` documents

    apt-get update && apt-get install -y build-essential pkg-config libssl-dev

as THE way a project declares its build dependencies, and every target
repo follows it — e.g. great-wall-connect/blitzlog-example:

    [tasks.bootstrap]
    run = '''
    apt-get update && apt-get install -y --no-install-recommends \\
        gcc g++ make pkg-config libssl-dev ca-certificates
    '''

With a read-only rootfs, as an unprivileged uid, that cannot work: dpkg
has to write /var/lib/dpkg, unpack files into /usr and run maintainer
scripts as root. The first run after #164 died with

    E: List directory /var/lib/apt/lists/partial is missing.
       - Acquire (30: Read-only file system)
    [bootstrap] ERROR task failed

and the agent never started.

The fix keeps the container's *capability* hardening (the agent still
runs with no capabilities at all) and gives the project back the two
things dpkg needs: root and a writable rootfs. These tests pin that
contract, because both halves regress silently:

  * a missing --read-only in the contract is invisible until a project
    tries to install a package;
  * a compiler baked into the agent image would work, but it is the
    PROJECT's job to provide its toolchain — the image has to stay
    lean, and the container's package manager is what makes the
    project's own toolchain available.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "packages" / "images" / "agent"
CONTRACT = AGENT_DIR / "container-hardening.env"
DOCKERFILE = AGENT_DIR / "Dockerfile"
SMOKE_TEST = AGENT_DIR / "smoke-test.sh"
BOOTSTRAP_DOC = REPO_ROOT / "docs" / "BOOTSTRAP.md"
DOCKER_DOC = REPO_ROOT / "docs" / "DOCKER.md"

# The packages the documented recipes ask for. A project's bootstrap
# task names them individually (gcc, g++, make …) or as the
# build-essential metapackage.
TOOLCHAIN_PACKAGES = ("build-essential", "pkg-config", "libssl-dev")

# The exact package list the target repos ask for.
EXAMPLE_REPO_PACKAGES = (
    "gcc",
    "g++",
    "make",
    "pkg-config",
    "libssl-dev",
    "ca-certificates",
)


def _read(path):
    return path.read_text(encoding="utf-8")


def _source_contract(expr):
    """Source the contract under /bin/sh and return the value of `expr`."""
    script = f". {CONTRACT}\nprintf '%s' {expr}\n"
    return subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, check=True
    ).stdout


class TestContainerCanInstallPackages(unittest.TestCase):
    """The container has to be able to install a package."""

    def test_contract_does_not_make_the_rootfs_read_only(self):
        """`--read-only` is what broke every project's bootstrap task."""
        flags = _source_contract('"$BLITZLOG_HARDENING_FLAGS"')
        self.assertNotIn(
            "--read-only",
            flags,
            "the container's rootfs must stay writable: dpkg cannot "
            "unpack a package into a read-only /usr or write its "
            "database into a read-only /var/lib/dpkg (issue #174)",
        )

    def test_contract_still_drops_every_capability(self):
        """uid 0 is only safe because the capability set is empty."""
        flags = _source_contract('"$BLITZLOG_HARDENING_FLAGS"')
        self.assertIn("--cap-drop=ALL", flags)
        self.assertIn("--security-opt=no-new-privileges:true", flags)

    def test_dockerfile_declares_no_non_root_user(self):
        """dpkg needs root; a `USER` directive would break it."""
        users = re.findall(r"(?m)^USER\s+(\S+)", _read(DOCKERFILE))
        for user in users:
            self.assertIn(
                user,
                ("root",),
                "the container must not declare a non-root USER: dpkg "
                "needs root to install a project's build dependencies",
            )

    def test_apt_sandbox_is_pinned_in_the_runtime_stage(self):
        """apt drops privileges to `_apt`, which needs the capabilities
        the contract drops. The image pins the sandbox to root so the
        download methods run as the container's uid — and that pin has
        to be in the RUNTIME stage, because the runtime stage is the
        one that has to install packages."""
        dockerfile = _read(DOCKERFILE)
        runtime_marker = "# ===== Stage 2: runtime ====="
        self.assertIn(runtime_marker, dockerfile)
        runtime = dockerfile.split(runtime_marker, 1)[1]
        self.assertIn("99-no-sandbox", runtime)
        self.assertIn('APT::Sandbox::User "root"', runtime)

    def test_runtime_stage_keeps_the_apt_lists_directory(self):
        """apt needs `<lists>/partial` to exist.

        The image build removes the downloaded indexes to stay small
        (`rm -rf /var/lib/apt/lists/*`), and apt does NOT recreate the
        `partial` subdirectory when the whole directory is gone. If
        the parent directory disappears with it, every runtime
        `apt-get update` fails with:

            E: List directory /var/lib/apt/lists/partial is missing.
        """
        dockerfile = _read(DOCKERFILE)
        runtime_marker = "# ===== Stage 2: runtime ====="
        runtime = dockerfile.split(runtime_marker, 1)[1]
        self.assertIn("rm -rf /var/lib/apt/lists/*", runtime)
        # The parent must survive: `rm -rf /var/lib/apt/lists/*` removes
        # the CONTENTS, not the directory itself.
        self.assertNotIn(
            "rm -rf /var/lib/apt/lists", runtime.replace("/var/lib/apt/lists/*", "")
        )


class TestImageStaysLean(unittest.TestCase):
    """The build toolchain belongs to the project, not to the image."""

    def test_runtime_stage_does_not_bake_a_compiler(self):
        runtime = _read(DOCKERFILE).split("# ===== Stage 2: runtime =====", 1)[1]
        for package in TOOLCHAIN_PACKAGES:
            self.assertNotIn(
                f"\n        {package} \\",
                runtime,
                f"{package} must not be baked into the agent image — the "
                "project installs its own toolchain in its bootstrap task",
            )

    def test_smoke_test_asserts_the_image_ships_no_compiler(self):
        smoke = _read(SMOKE_TEST)
        self.assertIn(
            "the image ships no compiler",
            smoke,
            "the smoke test must assert that the agent image does not "
            "carry a compiler — that is the project's job",
        )
        for tool in ("gcc", "g++", "cc", "make"):
            self.assertIn(tool, smoke)


class TestSmokeTestProvesTheInstallPath(unittest.TestCase):
    """The container itself has to prove that packages install."""

    def test_smoke_test_installs_a_package(self):
        smoke = _read(SMOKE_TEST)
        self.assertIn(
            "apt-get install -y --no-install-recommends",
            smoke,
            "the smoke test must install a package inside the container — "
            "that is the regression this issue is about, and a static "
            "assertion cannot see it",
        )

    def test_smoke_test_refreshes_the_package_lists_first(self):
        smoke = _read(SMOKE_TEST)
        self.assertIn("apt-get update", smoke)

    def test_smoke_test_asserts_the_installed_binary_is_usable(self):
        """`apt-get install` reporting success is not enough — the
        binary has to be on PATH, and the `cc` alternative (which Rust
        looks for) has to work."""
        smoke = _read(SMOKE_TEST)
        self.assertIn("command -v gcc", smoke)
        self.assertIn("cc --version", smoke)


class TestDocsDescribeTheModel(unittest.TestCase):
    """The docs are the contract — they have to tell the truth."""

    def test_bootstrap_doc_says_the_container_can_install_packages(self):
        doc = _read(BOOTSTRAP_DOC)
        self.assertIn(
            "apt-get install",
            doc,
            "the bootstrap doc must keep documenting the apt recipe — it "
            "is the documented way to declare build dependencies",
        )
        self.assertIn(
            "toolchain",
            doc,
            "the bootstrap doc must explain that the toolchain is the "
            "project's responsibility",
        )

    def test_docker_doc_documents_the_runtime_posture(self):
        doc = _read(DOCKER_DOC)
        self.assertIn("--cap-drop=ALL", doc)
        self.assertNotIn(
            "| `--read-only` |",
            doc,
            "the docker doc must not advertise --read-only any more — the "
            "container needs a writable rootfs to install a project's "
            "build dependencies (issue #174)",
        )


if __name__ == "__main__":
    unittest.main()
