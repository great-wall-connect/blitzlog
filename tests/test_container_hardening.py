"""Regression: the blitzlog-agent container must carry its hardening flags.

Issue #164 ("harden the agent container — non-root, dropped
capabilities, read-only rootfs") turns the container from "a container
with no containment" into an actual boundary. The first half of that —
the inert Tier 1 flags plus the Tier 3 Tailnet exemption — is pinned
here.

The hardening flags live in ONE place:
``packages/images/agent/container-hardening.env``. It is sourced by the
host-side watchdog (baked into the EC2 AMI as
``/usr/local/bin/container-hardening.env``) and by every dev
``docker run`` site in ``mise.toml``.

Why a test rather than a convention: the flags are inert on their own,
so nothing about a missing ``--cap-drop=ALL`` fails loudly at runtime —
the container just quietly keeps Docker's default capability set. The
only thing that catches it is an assertion, and the assertion has to
cover EVERY ``docker run`` site, not just the production one. A dev
container that runs without the flags is a dev container that cannot
exercise the production posture, so the dev and prod paths must stay
in lockstep.

The Tailnet exemption is asserted separately because it is the one
place where a capability IS granted: ``--cap-drop=ALL`` everywhere
else. If someone adds a ``--cap-add`` outside the documented
exemption, this file fails.

The contract file is sourced with ``sh`` (not ``bash``) on purpose: the
container's runtime shell is ``/bin/sh`` -> dash, and the mise dev
tasks must be able to source the file under a POSIX shell too.
"""

import os
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "packages/images/agent/container-hardening.env"
WATCHDOG = REPO_ROOT / "infra/aws/packer/scripts-docker-ubuntu/watchdog.sh"
MISE_TOML = REPO_ROOT / "mise.toml"
PACKER_HCL = REPO_ROOT / "infra/aws/packer/agent-docker-ubuntu.pkr.hcl"
PACKER_DIR = PACKER_HCL.parent
SYSTEMD_SH = REPO_ROOT / "infra/aws/packer/scripts-docker-ubuntu/02-systemd.sh"
SYSTEM_SH = REPO_ROOT / "infra/aws/packer/scripts-docker-ubuntu/01-system.sh"
ENTRYPOINT = REPO_ROOT / "packages/images/agent/entrypoint.sh"
DOCKERFILE = REPO_ROOT / "packages/images/agent/Dockerfile"
SMOKE_TEST = REPO_ROOT / "packages/images/agent/smoke-test.sh"
INSTALLER = REPO_ROOT / "packages/images/agent/install-opencode-plugins.sh"
DOCKER_DOC = REPO_ROOT / "docs/DOCKER.md"
DOCKER_IMAGES_WORKFLOW = REPO_ROOT / ".github/workflows/docker-images.yml"

# The flags every `docker run` of the agent image must carry. Kept as
# an explicit literal (rather than "whatever the contract file says")
# so that changing the contract is a deliberate act that has to
# update this test too — a contract that silently loses --cap-drop
# must not be able to redefine "correct" at the same time.
EXPECTED_HARDENING_FLAGS = [
    "--security-opt=no-new-privileges:true",
    "--cap-drop=ALL",
    "--pids-limit=1024",
    "--init",
    "--read-only",
]

# The one and only capability relaxation in the project.
EXPECTED_TAILSCALE_FLAGS = [
    "--cap-add=NET_ADMIN",
    "--cap-add=NET_RAW",
    "--device",
    "/dev/net/tun",
]

# Every mise task that launches the agent image. Each one MUST source
# the contract and expand the hardening flags. Pinned by name so a new
# dev task that launches the container without the flags fails loudly
# instead of silently running an unhardened container.
EXPECTED_DOCKER_RUN_TASKS = {
    "image-test",
    "image-run-test",
    "image-telegram",
    "image-telegram-live-assisted",
    "image-telegram-live-autonomous",
}


def _read(path):
    return path.read_text(encoding="utf-8")


# Every `${path.root}/...` path the Packer template hands to a
# provisioner. `path.root` is the directory of the input HCL file and
# Packer resolves a relative provisioner path against the directory
# packer runs in — both relative to the same base — so `${path.root}/x`
# always means "x next to the template".
_PROVISIONER_PATH_RE = re.compile(
    r'\b(?:source|script)\s*=\s*"\$\{path\.root\}(?P<rel>[^"]*)"'
)


def _provisioner_paths(hcl_text):
    """The relative paths every Packer provisioner points at."""
    return [m.group("rel") for m in _PROVISIONER_PATH_RE.finditer(hcl_text)]


def _resolve_provisioner_path(rel):
    """Resolve a `${path.root}/<rel>` reference to an absolute path."""
    return (PACKER_DIR / rel.lstrip("/")).resolve()


def _source_contract(expr):
    """Source the contract under /bin/sh and return the value of `expr`.

    `expr` is a shell expression (e.g. '"$BLITZLOG_HARDENING_FLAGS"')
    evaluated after the contract is sourced. POSIX sh has no indirect
    expansion, so the caller names the variable it wants rather than
    passing a variable name.

    Uses `sh` rather than `bash` on purpose: the container's runtime
    shell is /bin/sh -> dash, and the mise dev tasks source the file
    under whatever POSIX shell mise picks. A bashism in the contract
    would therefore break the dev path while passing a bash-only
    check.
    """
    result = subprocess.run(
        ["sh", "-c", f'. "$1"; printf "%s" {expr}', "sh", str(CONTRACT)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"could not source {CONTRACT} under /bin/sh: {result.stderr.strip()}"
        )
    return result.stdout


def _doc_paths(marker):
    """Return the backtick-quoted paths in the docs/DOCKER.md table
    delimited by `<!-- {marker}:start -->` / `<!-- {marker}:end -->`.

    The markers exist so this extraction is deterministic: the writable
    -path list is the security contract, and a prose paragraph that
    happened to mention a path must not be able to change it.
    """
    text = _read(DOCKER_DOC)
    start_marker = f"<!-- {marker}:start -->"
    end_marker = f"<!-- {marker}:end -->"
    self_contained = text.split(start_marker, 1)
    if len(self_contained) != 2:
        raise AssertionError(f"docs/DOCKER.md is missing the {start_marker} marker")
    body = self_contained[1].split(end_marker, 1)
    if len(body) != 2:
        raise AssertionError(f"docs/DOCKER.md is missing the {end_marker} marker")
    return re.findall(r"`(/[^`\s]+)`", body[0])


class TestContainerHardeningContract(unittest.TestCase):
    """The canonical contract file and the values it carries."""

    def test_contract_exists_and_is_readable(self):
        self.assertTrue(
            CONTRACT.is_file(),
            f"missing {CONTRACT} — the hardening contract must live next "
            "to the image it describes so the dev tasks and the watchdog "
            "can share it",
        )

    def test_contract_sources_cleanly_under_posix_sh(self):
        """A bashism in the contract breaks the dev tasks (and the
        container's /bin/sh -> dash runtime)."""
        flags = _source_contract('"$BLITZLOG_HARDENING_FLAGS"')
        self.assertIn(
            "--cap-drop=ALL",
            flags,
            "contract did not yield the expected hardening flags under "
            f"/bin/sh (got {flags!r})",
        )

    def test_hardening_flags_match_the_expected_set(self):
        flags = _source_contract('"$BLITZLOG_HARDENING_FLAGS"').split()
        self.assertEqual(
            sorted(flags),
            sorted(EXPECTED_HARDENING_FLAGS),
            "the canonical hardening flags changed — update "
            "EXPECTED_HARDENING_FLAGS in this test deliberately, and "
            "update docs/DOCKER.md's container-boundary section",
        )

    def test_tailscale_exemption_matches_the_expected_set(self):
        flags = _source_contract('"$BLITZLOG_TAILSCALE_FLAGS"').split()
        self.assertEqual(
            sorted(flags),
            sorted(EXPECTED_TAILSCALE_FLAGS),
            "the Tailscale capability exemption changed — it is the only "
            "relaxation of --cap-drop=ALL in the project, so any change "
            "needs to be a deliberate one (and a note in issue #164)",
        )

    def test_contract_does_not_grant_capabilities_outside_the_exemption(self):
        """--cap-drop=ALL means ALL. A stray --cap-add anywhere in the
        contract would be an unreviewed privilege grant."""
        source = _read(CONTRACT)
        add_flags = re.findall(r"--cap-add=\w+", source)
        self.assertEqual(
            sorted(add_flags),
            ["--cap-add=NET_ADMIN", "--cap-add=NET_RAW"],
            "the contract grants capabilities outside the documented "
            "Tailscale exemption",
        )


class TestAptStillWorksUnderDroppedCapabilities(unittest.TestCase):
    """The capability drop must not break package installation.

    apt runs its download methods as the unprivileged `_apt` user, which
    needs CAP_SETUID + CAP_SETGID + CAP_CHOWN. The hardening contract
    drops ALL capabilities, so every project bootstrap that installs
    build dependencies — the documented pattern in docs/BOOTSTRAP.md —
    died with:

        E: setgroups 0 failed - setgroups (1: Operation not permitted)
        E: Method https has died unexpectedly!

    The image therefore pins apt's sandbox to root. Re-granting
    SETUID/SETGID/CHOWN to every run instead would put the
    capabilities back on every agent run, including the ones that never
    install a package.
    """

    def test_image_pins_the_apt_sandbox_to_root(self):
        src = _read(DOCKERFILE)
        for setting in ('APT::Sandbox::User "root"', 'APT::Sandbox::Group "root"'):
            self.assertIn(
                setting,
                src,
                "the image must pin apt's sandbox to root — apt's own "
                "privilege drop needs CAP_SETUID/CAP_SETGID/CAP_CHOWN, "
                "which --cap-drop=ALL removes. Without this the agent "
                "cannot install the toolchain it is supposed to install.",
            )

    def test_apt_override_lands_in_the_runtime_stage(self):
        src = _read(DOCKERFILE)
        self.assertIn(
            "# ===== Stage 2: runtime =====",
            src,
            "the Dockerfile's runtime stage marker moved — update this test",
        )
        runtime = src.split("# ===== Stage 2: runtime =====", 1)[-1]
        self.assertIn(
            "/etc/apt/apt.conf.d/99-no-sandbox",
            runtime,
            "the apt override must be installed in the RUNTIME stage — the "
            "build stage is discarded, so writing it there would never "
            "reach the container that runs the agent",
        )


class TestDockerRunSitesCarryTheHardeningFlags(unittest.TestCase):
    """Every `docker run` of the agent image — production AND dev —
    must carry the hardening flags."""

    @classmethod
    def setUpClass(cls):
        cls.watchdog_src = _read(WATCHDOG)
        cls.mise = tomllib.loads(_read(MISE_TOML))
        cls.tasks = cls.mise.get("tasks", {})

    def _tasks_launching_the_container(self):
        return {
            name
            for name, task in self.tasks.items()
            if "docker run" in task.get("run", "")
        }

    def test_expected_dev_tasks_exist(self):
        found = self._tasks_launching_the_container()
        self.assertEqual(
            found,
            EXPECTED_DOCKER_RUN_TASKS,
            "the set of mise tasks that launch the agent image changed. "
            "Every one of them must source the hardening contract and "
            "expand the flags — update EXPECTED_DOCKER_RUN_TASKS here "
            "and check the new task carries the flags.",
        )

    def test_every_dev_task_sources_the_contract(self):
        for name in sorted(self._tasks_launching_the_container()):
            run = self.tasks[name].get("run", "")
            self.assertIn(
                ". packages/images/agent/container-hardening.env",
                run,
                f"mise task '{name}' launches the agent image but does not "
                "source the hardening contract — it would run without "
                "--cap-drop=ALL and friends",
            )

    def test_every_dev_task_expands_the_hardening_flags(self):
        for name in sorted(self._tasks_launching_the_container()):
            run = self.tasks[name].get("run", "")
            self.assertIn(
                "$BLITZLOG_HARDENING_FLAGS",
                run,
                f"mise task '{name}' launches the agent image but does not "
                "expand $BLITZLOG_HARDENING_FLAGS into its `docker run`",
            )

    def test_watchdog_sources_the_contract(self):
        self.assertIn(
            "source /usr/local/bin/container-hardening.env",
            self.watchdog_src,
            "the watchdog must source the canonical hardening contract; "
            "without it the production container runs unhardened",
        )

    def test_watchdog_splats_the_hardening_args_into_docker_run(self):
        run_block = self.watchdog_src.split("sudo docker run", 1)
        self.assertEqual(
            len(run_block),
            2,
            "could not find the `sudo docker run` invocation in watchdog.sh",
        )
        docker_run = run_block[1]
        self.assertIn(
            '"${HARDENING_ARGS[@]}"',
            docker_run,
            "the watchdog's `docker run` does not expand the hardening "
            "array — the production container would run unhardened",
        )

    def test_watchdog_expands_the_contract_into_arrays(self):
        self.assertIn(
            'read -r -a HARDENING_ARGS <<<"$BLITZLOG_HARDENING_FLAGS"',
            self.watchdog_src,
            "the watchdog must expand the contract's flag string into an "
            "array before splatting it into `docker run`",
        )


class TestTailscaleExemptionIsScoped(unittest.TestCase):
    """The Tailnet exemption is granted only on Tailnet-enrolled runs."""

    @classmethod
    def setUpClass(cls):
        cls.watchdog_src = _read(WATCHDOG)
        cls.mise = tomllib.loads(_read(MISE_TOML))
        cls.tasks = cls.mise.get("tasks", {})

    def test_watchdog_grants_the_exemption_only_with_an_auth_key(self):
        self.assertIn(
            'if [ -n "${TAILSCALE_AUTH_KEY:-}" ]; then',
            self.watchdog_src,
            "the watchdog must gate the Tailscale exemption on " "TAILSCALE_AUTH_KEY",
        )
        self.assertIn(
            'TUNNEL_FLAGS=("${TAILSCALE_ARGS[@]}")',
            self.watchdog_src,
            "the watchdog must grant the exemption from the canonical "
            "contract (BLITZLOG_TAILSCALE_FLAGS), not from a "
            "hand-rolled copy",
        )

    def test_dev_tasks_gate_the_exemption_on_the_auth_key(self):
        for name in ("image-telegram-live-assisted", "image-telegram-live-autonomous"):
            run = self.tasks[name].get("run", "")
            with self.subTest(task=name):
                self.assertIn(
                    'TUNNEL_FLAGS="$BLITZLOG_TAILSCALE_FLAGS"',
                    run,
                    f"mise task '{name}' must grant the Tailscale "
                    "exemption from the canonical contract",
                )
                self.assertRegex(
                    run,
                    r"if grep -qE '\^TAILSCALE_AUTH_KEY=\.\+'",
                    f"mise task '{name}' must gate the Tailscale "
                    "exemption on TAILSCALE_AUTH_KEY",
                )

    def test_no_cap_add_outside_the_contract(self):
        """`--cap-add` must only ever come from the canonical contract."""
        for path in (WATCHDOG, MISE_TOML, ENTRYPOINT):
            with self.subTest(path=path.name):
                found = re.findall(r"--cap-add=\w+", _read(path))
                self.assertEqual(
                    found,
                    [],
                    f"{path.name} grants capabilities directly; the only "
                    "sanctioned source is "
                    "packages/images/agent/container-hardening.env",
                )

    def test_host_creates_the_tun_device(self):
        """`--device /dev/net/tun` needs the device to exist on the
        AMI. A minimal Ubuntu image has no /dev/net/tun until the
        `tun` module is loaded."""
        src = _read(SYSTEM_SH)
        self.assertIn(
            "modprobe tun",
            src,
            "01-system.sh must load the `tun` module so /dev/net/tun "
            "exists on the AMI",
        )
        self.assertIn(
            "mknod /dev/net/tun c 10 200",
            src,
            "01-system.sh must create the /dev/net/tun device node "
            "explicitly (udev is not guaranteed to run on the minimal "
            "AMI at bake time)",
        )

    def test_entrypoint_keeps_tailscale_state_on_a_writable_path(self):
        """The container's rootfs is read-only, so tailscaled's state
        and socket cannot live under /var."""
        src = _read(ENTRYPOINT)
        self.assertIn(
            'TAILSCALE_DIR="/tmp/tailscale"',
            src,
            "the entrypoint must keep the tailscaled state under the "
            "writable /tmp tmpfs",
        )
        self.assertIn(
            '--socket="$TAILSCALE_SOCKET"',
            src,
            "the entrypoint must pass --socket to tailscaled (the "
            "default /var/run/tailscale is not writable)",
        )
        self.assertNotIn(
            "/var/lib/tailscale",
            src,
            "the entrypoint still writes tailscaled state under "
            "/var/lib/tailscale, which is not writable once the rootfs "
            "is read-only",
        )


class TestContractIsWiredIntoTheAmi(unittest.TestCase):
    """The AMI must actually carry the contract, or the watchdog's
    `source` fails and the agent never starts."""

    def test_packer_uploads_the_contract(self):
        src = _read(PACKER_HCL)
        self.assertIn(
            "packages/images/agent/container-hardening.env",
            src,
            "the Packer build must upload the hardening contract to the " "AMI",
        )
        self.assertIn(
            'destination = "/tmp/blitzlog-container-hardening.env"',
            src,
            "the Packer build must upload the hardening contract to "
            "/tmp/blitzlog-container-hardening.env (02-systemd.sh copies "
            "it into /usr/local/bin)",
        )

    def test_packer_provisioner_paths_resolve(self):
        src = _read(PACKER_HCL)
        rels = _provisioner_paths(src)
        self.assertTrue(
            rels,
            "the Packer template must reference its provisioner scripts "
            "through ${path.root}",
        )
        for rel in rels:
            resolved = _resolve_provisioner_path(rel)
            self.assertTrue(
                resolved.is_file(),
                f"the Packer provisioner path ${{path.root}}/{rel} resolves "
                f"to {resolved}, which does not exist — the AMI bake dies "
                "at the `file` provisioner's prepare step with 'Bad "
                "source' before a single instance is created",
            )

    def test_packer_contract_source_resolves_to_the_contract(self):
        src = _read(PACKER_HCL)
        resolved = [_resolve_provisioner_path(rel) for rel in _provisioner_paths(src)]
        self.assertIn(
            CONTRACT.resolve(),
            resolved,
            "the Packer build must upload the hardening contract from "
            f"{CONTRACT} — the provisioner's source path has to resolve to "
            "the file the watchdog sources, not just mention its name",
        )

    def test_systemd_setup_installs_the_contract(self):
        src = _read(SYSTEMD_SH)
        self.assertIn(
            "sudo cp /tmp/blitzlog-container-hardening.env "
            "/usr/local/bin/container-hardening.env",
            src,
            "02-systemd.sh must copy the contract to "
            "/usr/local/bin/container-hardening.env — that is where the "
            "watchdog sources it from",
        )


class TestWritablePathContract(unittest.TestCase):
    """The writable-path list is the security contract of the read-only
    rootfs. It must be explicit, documented, and asserted — a
    --read-only rootfs with an implicit hole is worse than no flag at
    all."""

    def test_writable_paths_match_the_documented_table(self):
        contract = set(_source_contract('"$BLITZLOG_WRITABLE_PATHS"').split())
        documented = set(_doc_paths("writable-paths"))
        self.assertEqual(
            contract,
            documented,
            "the writable-path list in "
            "packages/images/agent/container-hardening.env does not match "
            "the table in docs/DOCKER.md. A path that can be written but "
            "is not documented (or vice versa) is exactly the implicit "
            "hole this contract exists to prevent.",
        )

    def test_readonly_mounts_match_the_documented_table(self):
        contract = set(_source_contract('"$BLITZLOG_READONLY_MOUNTS"').split())
        documented = set(_doc_paths("readonly-paths"))
        self.assertEqual(
            contract,
            documented,
            "the read-only mount list does not match the table in " "docs/DOCKER.md",
        )

    def test_the_two_lists_do_not_overlap(self):
        writable = set(_source_contract('"$BLITZLOG_WRITABLE_PATHS"').split())
        readonly = set(_source_contract('"$BLITZLOG_READONLY_MOUNTS"').split())
        self.assertEqual(
            writable & readonly,
            set(),
            "a path is listed as both writable and read-only — the "
            "container's actual behaviour would be one or the other",
        )

    def test_tmpfs_mounts_cover_every_non_bind_writable_path(self):
        """The tmpfs mounts are what back the writable paths that are
        not host bind mounts (/tmp and the agent's HOME)."""
        tmpfs = _source_contract('"$BLITZLOG_TMPFS_MOUNTS"')
        self.assertIn("--tmpfs /tmp:", tmpfs)
        self.assertIn("/home/blitzlog:", tmpfs)
        # The tmpfs must be owned by the container's unprivileged uid,
        # otherwise the agent cannot write its own HOME.
        self.assertIn("uid=1000,gid=1000", tmpfs)

    def test_agent_config_dir_has_its_own_uid_owned_tmpfs(self):
        """$HOME/.config needs its OWN tmpfs, not just the $HOME one.

        Docker creates the missing parent of a bind mount as
        `root:root 0755`. The gh CLI config is bind-mounted at
        `$HOME/.config/gh`, so Docker creates `$HOME/.config` itself —
        owned by root, inside the $HOME tmpfs. The agent runs as uid
        1000 and then cannot create `$HOME/.config/opencode` under it:

            mkdir: cannot create directory '/home/blitzlog/.config/opencode':
            Permission denied

        which aborted the entrypoint before the agent ever started.
        Mounting a tmpfs at $HOME/.config with the agent's own uid/gid
        makes the mountpoint uid-owned; Docker then mounts the
        `$HOME/.config/gh` bind INSIDE it (parents before children).
        """
        tmpfs = _source_contract('"$BLITZLOG_TMPFS_MOUNTS"')
        self.assertIn(
            "--tmpfs /home/blitzlog/.config:",
            tmpfs,
            "$HOME/.config MUST have its own tmpfs. Docker creates the "
            "missing parent of the $HOME/.config/gh bind mount as "
            "root:root 0755, and the agent (uid 1000) then cannot "
            "create $HOME/.config/opencode — the entrypoint aborts with "
            "'Permission denied' before the agent starts.",
        )
        # ...and it must be owned by the agent's uid, not root.
        config_tmpfs = tmpfs.split("--tmpfs /home/blitzlog/.config:")[1]
        self.assertIn(
            "uid=1000,gid=1000",
            config_tmpfs,
            "the $HOME/.config tmpfs must be owned by the container's "
            "unprivileged uid (1000), or the agent cannot write its own "
            "config dir",
        )

    def test_smoke_test_asserts_the_config_dir_is_writable(self):
        """The image's smoke test must assert $HOME/.config is writable.

        This assertion is only meaningful when the container actually
        has a Docker-created $HOME/.config — i.e. when a gh config is
        bind-mounted under it, the way production does it. Without
        both halves the failure mode is invisible: CI was green while
        the first real production run died on 'Permission denied'.
        """
        smoke = _read(SMOKE_TEST)
        self.assertIn(
            '$HOME/.config"',
            smoke,
            "the smoke test must assert that $HOME/.config is writable "
            "by the runtime uid — that is the invariant the non-root "
            "read-only-rootfs design depends on",
        )
        for path, label in (
            (DOCKER_IMAGES_WORKFLOW, "the CI smoke-test step"),
            (MISE_TOML, "the local mise image-* tasks"),
        ):
            with self.subTest(path=path.name):
                src = _read(path)
                self.assertIn(
                    ".config/gh:ro",
                    src,
                    f"{label} must bind-mount a gh config at "
                    "$HOME/.config/gh — that mount is what makes Docker "
                    "create $HOME/.config as root, which is the topology "
                    "the smoke test must be exercised against",
                )

    def test_every_docker_run_expands_the_tmpfs_mounts(self):
        """A --read-only rootfs without its tmpfs mounts would leave
        the agent unable to write anything at all."""
        for path in (WATCHDOG, MISE_TOML):
            with self.subTest(path=path.name):
                self.assertIn(
                    "$BLITZLOG_TMPFS_MOUNTS",
                    _read(path),
                    f"{path.name} does not expand the tmpfs mounts — a "
                    "read-only rootfs with no writable paths would break "
                    "the agent",
                )


class TestContainerRunsAsNonRoot(unittest.TestCase):
    """The image must not run the agent as uid 0."""

    @classmethod
    def setUpClass(cls):
        cls.dockerfile = _read(DOCKERFILE)
        cls.entrypoint = _read(ENTRYPOINT)
        cls.smoke = _read(SMOKE_TEST)

    def test_dockerfile_declares_a_non_root_user(self):
        users = re.findall(r"(?m)^USER\s+(\S+)", self.dockerfile)
        self.assertTrue(users, "the Dockerfile has no USER directive")
        for user in users:
            self.assertNotEqual(
                user,
                "root",
                "the image's USER directive must not be root",
            )
        self.assertEqual(
            users[-1],
            "blitzlog",
            "the image's final USER directive must be the unprivileged "
            "`blitzlog` user",
        )

    def test_dockerfile_creates_the_blitzlog_user_with_the_contract_ids(self):
        uid = _source_contract('"$BLITZLOG_CONTAINER_UID"').strip()
        gid = _source_contract('"$BLITZLOG_CONTAINER_GID"').strip()
        self.assertIn(f"--uid {uid}", self.dockerfile)
        self.assertIn(f"--gid {gid}", self.dockerfile)
        self.assertIn("useradd", self.dockerfile)
        self.assertIn("groupadd", self.dockerfile)

    def test_dockerfile_sets_home_to_the_contract_home(self):
        home = _source_contract('"$BLITZLOG_CONTAINER_HOME"').strip()
        self.assertIn(
            f"HOME={home}",
            self.dockerfile,
            "the Dockerfile must set HOME to the contract's container home",
        )

    def test_dockerfile_installs_pip_into_the_writable_home(self):
        """A repo's `bootstrap` task does a system-wide
        `pip install`, which fails with EROFS on a read-only rootfs.
        PIP_USER=1 sends it to the user site-packages on the writable
        HOME tmpfs instead."""
        self.assertIn(
            "PIP_USER=1",
            self.dockerfile,
            "the image must set PIP_USER=1 — a repo's bootstrap task "
            "would otherwise fail to install its Python dependencies on "
            "the read-only rootfs",
        )

    def test_dockerfile_bakes_the_opencode_components_outside_home(self):
        """They must NOT be baked under $HOME — HOME is a tmpfs and
        would shadow them."""
        home = _source_contract('"$BLITZLOG_CONTAINER_HOME"').strip()
        self.assertIn(
            "COPY opencode/plugins/ /opt/blitzlog/opencode/plugins/", self.dockerfile
        )
        self.assertIn(
            "COPY opencode/tools/ /opt/blitzlog/opencode/tools/", self.dockerfile
        )
        self.assertIn("COPY opencode/lib/ /opt/blitzlog/opencode/lib/", self.dockerfile)
        self.assertNotIn(
            f"COPY opencode/plugins/ {home}/",
            self.dockerfile,
            "the opencode components are baked under $HOME, which is a "
            "tmpfs — they would be shadowed at runtime",
        )

    def test_entrypoint_installs_the_baked_components_into_home(self):
        self.assertIn(
            "/usr/local/bin/install-opencode-plugins.sh",
            self.entrypoint,
            "the entrypoint must install the baked opencode components "
            "into the agent's HOME",
        )

    def test_installer_is_copied_into_the_image_and_executable(self):
        self.assertIn(
            "COPY install-opencode-plugins.sh /usr/local/bin/install-opencode-plugins.sh",
            self.dockerfile,
        )
        self.assertIn(
            "/usr/local/bin/install-opencode-plugins.sh",
            self.dockerfile,
            "the installer must be made executable in the image",
        )

    def test_installer_uses_cp_r_not_cp_a(self):
        """`cp -a` preserves ownership, which a non-root user cannot
        do — GNU cp exits non-zero when it fails to, and the entrypoint
        runs under `set -e`."""
        src = _read(INSTALLER)
        self.assertIn("cp -R ", src)
        self.assertNotIn("cp -a ", src)

    def test_smoke_test_asserts_a_non_root_uid(self):
        self.assertIn(
            "AGENT_UID=$(id -u)",
            self.smoke,
            "the image's smoke test must assert the runtime uid",
        )
        self.assertIn(
            'if [ "$AGENT_UID" = "0" ]',
            self.smoke,
            "the image's smoke test must fail when the container runs as " "root",
        )

    def test_smoke_test_does_not_mkdir_on_a_read_only_rootfs(self):
        """`mkdir -p` on a read-only rootfs fails with EROFS — the
        smoke test must assert existence, not create it."""
        self.assertNotIn(
            'mkdir -p "$d"',
            self.smoke,
            "the smoke test creates directories, which fails on the "
            "read-only rootfs — it should assert they exist",
        )


class TestCredentialMountsAreReadOnly(unittest.TestCase):
    """The host owns the GitHub credentials; the agent may read them
    but must not be able to tamper with the host's copy."""

    @classmethod
    def setUpClass(cls):
        cls.watchdog_src = _read(WATCHDOG)

    def test_credential_bind_mounts_are_read_only(self):
        for mount in (
            (
                '-v "/root/.git-credentials.d:${BLITZLOG_CONTAINER_HOME}'
                '/.git-credentials.d:ro"'
            ),
            (
                '-v "/root/gh-config/${INSTANCE_ID}:${BLITZLOG_CONTAINER_HOME}'
                '/.config/gh:ro"'
            ),
        ):
            with self.subTest(mount=mount):
                self.assertIn(
                    mount,
                    self.watchdog_src,
                    "the GitHub credential bind mounts must be read-only — "
                    "the host is the only writer of those files",
                )

    def test_watchdog_chowns_the_credential_paths_to_the_container_uid(self):
        self.assertIn(
            'chown -R "$BLITZLOG_CONTAINER_UID:$BLITZLOG_CONTAINER_GID" \\\n'
            '            /root/.git-credentials.d "/root/gh-config/${INSTANCE_ID}"',
            self.watchdog_src,
            "the watchdog must chown the credential paths to the "
            "container's uid — a bind mount preserves the source's "
            "ownership, so a root-owned 0600 file is unreadable from "
            "inside a non-root container",
        )

    def test_watchdog_chowns_the_writable_bind_mounts(self):
        for path in ("/workspace", "/var/log/blitzlog"):
            with self.subTest(path=path):
                self.assertIn(
                    f'chown -R "$BLITZLOG_CONTAINER_UID:$BLITZLOG_CONTAINER_GID" {path}',
                    self.watchdog_src,
                    f"the watchdog must chown {path} to the container's "
                    "uid, or the agent's first write fails with EACCES",
                )

    def test_github_token_is_not_passed_to_the_container(self):
        """The token reaches the agent through the read-only mounts —
        it must not also sit in the container's environment, where
        `docker inspect` and every same-uid process could read it."""
        self.assertNotIn(
            '-e "GITHUB_TOKEN=$GITHUB_TOKEN"',
            self.watchdog_src,
            "the GitHub token must not be passed to the container as an "
            "env var — the host owns the credential files",
        )

    def test_entrypoint_does_not_write_the_credential_mount_unconditionally(self):
        """The write is the local-dev fallback; the production path
        finds the file already mounted read-only."""
        entrypoint = _read(ENTRYPOINT)
        self.assertIn(
            'if [ ! -d "$HOME/.git-credentials.d" ] || '
            '[ -w "$HOME/.git-credentials.d" ]; then',
            entrypoint,
            "the entrypoint must only materialise the credential file "
            "when the target is not a read-only host mount",
        )
        self.assertIn(
            'if [ ! -f "$GH_HOSTS_FILE" ]; then',
            entrypoint,
            "the entrypoint must only run `gh auth login` when the host "
            "has not already mounted a hosts.yml",
        )


class TestCredentialHelperIsReadOnly(unittest.TestCase):
    """git must not try to WRITE the host's credential file.

    The credential file is a read-only bind mount, and
    `git credential-store` is not read-only in practice: after a
    successful authentication git calls the helper's `store` operation,
    and credential-store rewrites the file under a lock:

        fatal: unable to get credential storage lock in 1000 ms: Read-only file system

    git ignores a helper's exit status, so the clone still succeeds —
    but every run logs a spurious "fatal:" that reads like a failed
    clone, and the credential can never be updated. The container
    therefore ships a get-only helper instead.
    """

    @classmethod
    def setUpClass(cls):
        cls.helper = REPO_ROOT / "packages/images/agent/blitzlog-credential-helper"
        cls.entrypoint = _read(ENTRYPOINT)
        cls.dockerfile = _read(DOCKERFILE)
        cls.smoke_test = _read(SMOKE_TEST)

    def test_helper_is_baked_into_the_image(self):
        self.assertTrue(
            self.helper.is_file(),
            f"missing {self.helper} — the container needs a get-only "
            "credential helper to use the host's read-only credential file",
        )
        self.assertIn(
            "COPY blitzlog-credential-helper /usr/local/bin/blitzlog-credential-helper",
            self.dockerfile,
            "the credential helper must be baked into the image — the "
            "rootfs is read-only, so it cannot be written at runtime",
        )
        self.assertIn(
            "/usr/local/bin/blitzlog-credential-helper",
            self.dockerfile.split("chmod +x")[-1],
            "the credential helper must be chmod +x in the image",
        )

    def test_entrypoint_points_git_at_the_read_only_helper(self):
        self.assertTrue(
            "'!/usr/local/bin/blitzlog-credential-helper'" in self.entrypoint,
            "the entrypoint must configure the get-only credential helper",
        )
        self.assertNotIn(
            'credential.helper "store --file',
            self.entrypoint,
            "the entrypoint must NOT use `git credential-store` against "
            "the host's read-only credential file — credential-store "
            "rewrites the file under a lock when git approves a "
            "credential, which fails with 'unable to get credential "
            "storage lock ... Read-only file system'",
        )

    def test_helper_only_answers_get(self):
        """`store` and `erase` must be no-ops."""
        helper_src = _read(self.helper)
        self.assertIn(
            '[ "$op" = "get" ] || exit 0',
            helper_src,
            "the helper must ignore every operation except `get` — the "
            "credential file is a read-only mount",
        )

    def test_helper_never_writes_the_credential_file(self):
        helper_src = _read(self.helper)
        # Strip the comments so the prose cannot trip the check, then
        # look for anything that could write: a redirection (other than
        # /dev/null) or a command that mutates a file.
        code = "\n".join(
            line
            for line in helper_src.splitlines()
            if not line.lstrip().startswith("#")
        )
        code = code.replace(">/dev/null", "").replace("2>&1", "")
        for forbidden in (
            ">",
            "rm ",
            "mv ",
            "cp ",
            "dd ",
            "tee ",
            "truncate",
            "chmod ",
            "install ",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(
                    forbidden,
                    code,
                    "the credential helper must never write the "
                    "credential file — that is the whole point of it",
                )

    def test_helper_returns_the_token(self):
        """Functional: the helper must hand git the token."""
        with tempfile.TemporaryDirectory() as tmp:
            creds = Path(tmp) / "github"
            creds.write_text(
                "https://x-access-token:ghp_TESTTOKEN123@github.com\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "sh",
                    str(self.helper),
                    "get",
                ],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "BLITZLOG_GIT_CREDENTIAL_FILE": str(creds),
                },
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("username=x-access-token", result.stdout)
            self.assertIn("password=ghp_TESTTOKEN123", result.stdout)

    def test_helper_does_not_touch_the_file_on_store_or_erase(self):
        """Functional: the write paths must leave the file byte-identical."""
        with tempfile.TemporaryDirectory() as tmp:
            creds = Path(tmp) / "github"
            original = "https://x-access-token:ghp_TESTTOKEN123@github.com\n"
            creds.write_text(original, encoding="utf-8")
            for op in ("store", "erase"):
                with self.subTest(op=op):
                    result = subprocess.run(
                        ["sh", str(self.helper), op],
                        input=(
                            "protocol=https\nhost=github.com\n"
                            "username=x-access-token\npassword=other\n\n"
                        ),
                        capture_output=True,
                        text=True,
                        env={
                            **os.environ,
                            "BLITZLOG_GIT_CREDENTIAL_FILE": str(creds),
                        },
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(
                        creds.read_text(encoding="utf-8"),
                        original,
                        f"the helper modified the credential file on {op}",
                    )

    def test_smoke_test_exercises_the_helper(self):
        self.assertIn(
            "blitzlog-credential-helper",
            self.smoke_test,
            "the image smoke test must exercise the credential helper — a "
            "helper that cannot read the host's file would only fail at "
            "clone time, in production",
        )


class TestMiseIsRunnableAsAnUnprivilegedUid(unittest.TestCase):
    """mise's installer only makes the binary executable when it runs as
    root, and the container no longer runs as root (issue #164).

    Without the exec bit the first `mise` call fails with

        /usr/local/bin/entrypoint.sh: 653: mise: Permission denied

    and dash exits 126 — the container dies before the agent starts.
    """

    @classmethod
    def setUpClass(cls):
        cls.installer = REPO_ROOT / "packages/images/agent/install-mise.sh"
        cls.entrypoint = _read(ENTRYPOINT)
        cls.dockerfile = _read(DOCKERFILE)
        cls.smoke_test = _read(SMOKE_TEST)

    def test_installer_is_baked_into_the_image(self):
        self.assertTrue(
            self.installer.is_file(),
            f"missing {self.installer} — the entrypoint needs a mise "
            "installer that works as an unprivileged uid",
        )
        self.assertIn(
            "COPY install-mise.sh /usr/local/bin/install-mise.sh",
            self.dockerfile,
            "the mise installer must be baked into the image — the "
            "rootfs is read-only, so it cannot be written at runtime",
        )
        self.assertIn(
            "/usr/local/bin/install-mise.sh",
            self.dockerfile.split("chmod +x")[-1],
            "the mise installer must be chmod +x in the image",
        )

    def test_entrypoint_uses_the_installer(self):
        self.assertTrue(
            "/usr/local/bin/install-mise.sh" in self.entrypoint,
            "the entrypoint must install mise through install-mise.sh — "
            "mise's own installer only chmods the binary as root",
        )

    def test_installer_makes_the_binary_executable(self):
        installer = _read(self.installer)
        self.assertIn(
            'chmod +x "$MISE_BIN"',
            installer,
            "the installer must chmod +x the mise binary — mise's own "
            "installer only does that when it runs as root",
        )

    def test_installer_verifies_that_mise_actually_runs(self):
        installer = _read(self.installer)
        self.assertIn(
            '"$MISE_BIN" --version',
            installer,
            "the installer must probe that mise actually runs, so a "
            "broken toolchain fails with a diagnosable message instead "
            "of an exec error three lines later",
        )

    def test_entrypoint_calls_mise_by_absolute_path(self):
        """A PATH lookup would turn a missing/unusable mise into the
        same cryptic 'Permission denied'."""
        self.assertNotIn(
            "        mise install -y\n",
            self.entrypoint,
            "mise must be invoked through $MISE_BIN, not through a PATH " "lookup",
        )
        self.assertIn('"$MISE_BIN" install -y', self.entrypoint)

    def test_installer_makes_a_non_executable_binary_runnable(self):
        """Functional: the exact bug this fixes.

        A binary that is present but NOT executable is what mise's
        installer leaves behind when it runs as a non-root user. The
        installer must make it runnable.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fake_mise = Path(tmp) / "mise"
            fake_mise.write_text('#!/bin/sh\necho "mise 0.0.0"\n', encoding="utf-8")
            fake_mise.chmod(0o644)
            self.assertFalse(
                os.access(fake_mise, os.X_OK),
                "the fake mise should not be executable before the fix",
            )
            result = subprocess.run(
                ["sh", str(self.installer), str(fake_mise)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(fake_mise))
            self.assertTrue(
                os.access(fake_mise, os.X_OK),
                "the installer must make a non-executable mise runnable",
            )

    def test_smoke_test_exercises_the_mise_installer(self):
        self.assertIn(
            "install-mise.sh",
            self.smoke_test,
            "the image smoke test must exercise the mise installer — a "
            "non-executable mise only failed at runtime, in production",
        )


if __name__ == "__main__":
    unittest.main()
