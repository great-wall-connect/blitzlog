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

import re
import subprocess
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


if __name__ == "__main__":
    unittest.main()
