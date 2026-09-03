# Bootstrap Task Convention

This document describes the `bootstrap` task convention used by Blitzlog's
runtime to install a project's build dependencies inside the EC2 worker
instance. It is the canonical reference for project owners, contributors,
and the autonomous agent itself.

## Rationale

Blitzlog runs an autonomous agent on a freshly provisioned EC2 spot
instance. The instance image is intentionally minimal — a Python runtime,
the AWS CLI, and `git`. Anything a project needs to **build** native
artifacts (C toolchains, linkers, system headers, Node native module
prerequisites, …) is the project's responsibility, not the agent's.

The `bootstrap` task is the single, declarative hook a project uses to
declare those build dependencies. Declaring them in source control gives
everyone the same environment:

- **The agent** gets a reproducible build on every run.
- **Project owners** get a clear, reviewable manifest of build-time
  system requirements.
- **CI runners and local developers** get the same instructions by
  running `mise run bootstrap` themselves — there is no "works on the
  agent's machine" failure mode.

Critically, the **agent must not self-heal missing build tools.** If a
required tool is absent and the project has no `bootstrap` task, the
agent stops and reports the failure rather than running `apt install`,
`brew install`, `dnf install`, or `pip install` of system packages.
See [AGENTS.md](../AGENTS.md) → *Implementation Standards* for the
behavioral rule and [issue #4][guardrails-issue] for the long-term
diagnostic-permissions fix.

[guardrails-issue]: https://github.com/great-wall-connect/blitzlog/issues/4

## Lifecycle

The bootstrap task is part of the EC2 user-data script synthesised in
[`lambda/handler.py`](../lambda/handler.py). The relevant excerpt:

```bash
log "Installing project toolchains via mise..."
export MISE_NODE_VERIFY=0
mise install -y

log "Running project bootstrap if defined..."
if mise tasks --name-only 2>/dev/null | grep -qx "bootstrap"; then
    mise run bootstrap
fi
```

Concretely, the lifecycle is:

1. **Cloud-init** clones the target repository into `/workspace/repo`.
2. **`mise` is installed** if `mise.toml` or `.tool-versions` is present.
3. **`mise install`** provisions the language toolchains declared in
   `[tools]` (Python, Node, Rust, Go, …).
4. **Bootstrap check** — `mise tasks --name-only` is filtered for an
   exact-match line `bootstrap`.
5. **`mise run bootstrap`** runs the task in the repo root, inheriting
   mise's activated `PATH` so `[tools]` entries are usable.
6. **OpenCode agent starts** with a working build environment.

Conventions:

- The task **must** be named `bootstrap` exactly. The runtime greps for
  the exact token, so `bootstrap-deps` or `setup` will be ignored.
- The task **must** be idempotent. The runtime does not cache its
  result; a retry or a fresh instance will rerun it.
- The task **should** be fast. It runs on every agent invocation before
  the agent begins work.
- The task **may** fail. A non-zero exit code aborts the agent's
  startup, which is the desired signal that the project is misconfigured.

## Recipes

The following `mise.toml` snippets illustrate the convention for each
supported stack. In each case, the project's `[tools]` block already
provisions the language toolchain; the `[tasks.bootstrap]` block
installs the **system-level** dependencies needed to build native
artifacts.

### Rust

Rust's `cargo` is provisioned by `[tools]`. The bootstrap task installs
the C linker and system libraries that `cc` crates link against.

```toml
[tasks.bootstrap]
description = "Install build tools and linkers for Rust crates"
run = """
set -euo pipefail
if command -v dnf >/dev/null 2>&1; then
    dnf install -y gcc gcc-c++ make pkgconfig openssl-devel
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y build-essential pkg-config libssl-dev
else
    echo "Unsupported package manager — please install a C toolchain" >&2
    exit 1
fi
"""
```

This is the recipe that fixes the Blitzlog example repo issue surfaced
in [issue #1](https://github.com/great-wall-connect/blitzlog/issues/1):
`linker 'cc' not found`.

### Node.js native modules

Node and npm are provisioned by `[tools]`. Native modules such as
`better-sqlite3`, `node-gyp` builds, and `sharp` need a C/C++ toolchain.

```toml
[tasks.bootstrap]
description = "Install build tools for Node native modules"
run = """
set -euo pipefail
if command -v dnf >/dev/null 2>&1; then
    dnf install -y gcc gcc-c++ make python3
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y build-essential python3
fi
"""
```

`python3` is included because `node-gyp` shells out to Python during
the build.

### Python C extensions

Python is provisioned by `[tools]`. Wheels cover most extensions, but
building from source (e.g. `pip install --no-binary :all:`) needs the
matching Python development headers and a C compiler.

```toml
[tasks.bootstrap]
description = "Install build tools for Python C extensions"
run = """
set -euo pipefail
if command -v dnf >/dev/null 2>&1; then
    dnf install -y gcc gcc-c++ make python3-devel openssl-devel
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y build-essential python3-dev libssl-dev
fi
"""
```

### Go cgo

Go is provisioned by `[tools]`. `cgo` requires a C toolchain and the
target distribution's libc headers.

```toml
[tasks.bootstrap]
description = "Install build tools for Go cgo"
run = """
set -euo pipefail
if command -v dnf >/dev/null 2>&1; then
    dnf install -y gcc gcc-c++ make glibc-devel
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y build-essential
fi
"""
```

## Minimal example

A project that needs nothing more than the language toolchains
declared in `[tools]` does not need a `bootstrap` task — omitting it is
the valid choice. The following `mise.toml` is sufficient for a pure
Python project whose wheels resolve from PyPI:

```toml
[tools]
python = "3.12"

[tasks.test]
description = "Run unit tests"
run = "pytest tests/ -v"
```

A project that needs one extra toolchain component adds a single
`[tasks.bootstrap]` block alongside its existing `[tools]` entry:

```toml
[tools]
python = "3.12"

[tasks.bootstrap]
description = "Install system headers for cryptography wheels"
run = "dnf install -y gcc openssl-devel || apt-get update && apt-get install -y build-essential libssl-dev"

[tasks.test]
description = "Run unit tests"
run = "pytest tests/ -v"
```

The runtime detects `bootstrap`, runs it, and the agent starts in a
working environment.

## See also

- [AGENTS.md](../AGENTS.md) — agent behavioral rules, including the
  "stop and report, do not install" rule for missing build tools.
- [CHANGELOG.md](../CHANGELOG.md) — `Per-project bootstrap task via mise.toml`.
- [issue #4][guardrails-issue] — deferred: add an explicit `permission`
  block to generated `opencode.json` so the agent can diagnose missing
  tools without self-healing.

[guardrails-issue]: https://github.com/great-wall-connect/blitzlog/issues/4
