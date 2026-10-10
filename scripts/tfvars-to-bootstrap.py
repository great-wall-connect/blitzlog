#!/usr/bin/env python3
"""Convert infra/dev/terraform.tfvars + infra/prod/terraform.tfvars to
``infra/bootstrap/terraform.dev.tfvars`` and
``infra/bootstrap/terraform.prod.tfvars``.

The operator's per-env tfvars (one file per env) is the source of
truth for the values the bootstrap module needs. This script reads
both files, groups every variable under a single map per env (``dev``
and ``prod``), and writes one bootstrap-shaped tfvars per env. The
operator then runs::

    cd infra/bootstrap
    terraform apply \\
        -var-file=terraform.dev.tfvars \\
        -var-file=terraform.prod.tfvars

Why two output files instead of one merged tfvars:

- **Human readability.** Each bootstrap tfvars is scoped to a
  single env. ``terraform.dev.tfvars`` contains one ``dev = { ... }``
  block; ``terraform.prod.tfvars`` contains one ``prod = { ... }``
  block. You can diff/edit one without touching the other.
- **No variable-name collisions.** The bootstrap module declares
  ``var.dev`` and ``var.prod`` as two separate ``map(string)``
  variables; the two files don't fight over variable names because
  each owns its own map.
- **The bootstrap can cycle through ``["dev", "prod"]`` in a
  single for_each** (see infra/bootstrap/secrets.tf) instead of
  materialising two parallel structures.

The output is a one-time artifact. The script is kept in the repo
so the operator can re-run it when values change; the *output*
files are gitignored.

Key transformation
-------------------

The per-env tfvars uses HCL variable names (``aws_region``,
``vpc_id``, … — underscores). The SSM leaf names use kebab-case
(``aws-region``, ``vpc-id``, … — hyphens). The script converts
underscores to hyphens in the output map keys so the bootstrap's
``local.env_values[env][leaf]`` lookup works directly.

Heredoc / list / bool / int values are passed through verbatim. The
bootstrap module's ``variables.tf`` is the source of truth for the
type; if a per-env value is ``false`` and the bootstrap variable is
``type = bool``, the script emits ``false`` and Terraform coerces at
parse time.

Usage
-----

::

    scripts/tfvars-to-bootstrap.py \\
        infra/dev/terraform.tfvars \\
        infra/prod/terraform.tfvars \\
        --out-dir infra/bootstrap

    cd infra/bootstrap
    terraform apply \\
        -var-file=terraform.dev.tfvars \\
        -var-file=terraform.prod.tfvars

Requires
--------

- Python 3.10+ (no third-party dependencies).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def parse_tfvars(text: str) -> dict[str, str]:
    """Parse a ``terraform.tfvars`` file into ``{key: raw_value}``.

    The HCL subset blitzlog's tfvars actually uses:

    - ``key = "value"`` (with optional inline ``#`` comment)
    - ``key = <<EOT ... EOT`` (heredoc, preserves newlines)
    - ``key = ["a", "b"]`` (HCL list; written back as-is)
    - ``key = true | false``
    - ``key = 123``

    ``raw_value`` is the literal HCL right-hand side. The caller
    writes it back to the output tfvars with no transformation.
    Anything more exotic (maps, function calls, ``for`` expressions)
    raises ``ValueError`` -- the script is not a general HCL parser.
    """
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        m = re.match(r"^([a-z_][a-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            raise ValueError(
                f"could not parse line {i + 1}: {line!r}\n"
                '(supported: key = "value" | key = <<EOT ... EOT | '
                "key = [..] | key = bool | key = int)"
            )
        key, rest = m.group(1), m.group(2)
        heredoc_m = re.match(r"^<<-?\s*(\S+)\s*$", rest)
        if heredoc_m:
            delim = heredoc_m.group(1)
            buf: list[str] = []
            i += 1
            while i < n and lines[i].strip() != delim:
                buf.append(lines[i])
                i += 1
            if i >= n:
                raise ValueError(
                    f"unterminated heredoc starting at line {i + 1 - len(buf)} "
                    f"(delimiter {delim!r} not found)"
                )
            i += 1  # skip the delimiter line
            out[key] = "\n".join(buf)
            continue
        comment_idx = _find_inline_comment(rest)
        if comment_idx is not None:
            rest = rest[:comment_idx].rstrip()
        out[key] = rest
        i += 1
    return out


def _find_inline_comment(s: str) -> int | None:
    """Return the index of the first HCL ``#``-comment, or ``None``.

    Comments start with ``#`` outside of string literals. We do a
    simple state machine: track whether we're inside a double-quoted
    string; an out-of-string ``#`` preceded by whitespace is a
    comment.
    """
    in_string = False
    for idx, ch in enumerate(s):
        if ch == '"':
            in_string = not in_string
        elif ch == "#" and not in_string and (idx == 0 or s[idx - 1].isspace()):
            return idx
    return None


def _to_leaf_key(tfvars_key: str) -> str:
    """Convert a per-env tfvars key (``aws_region``) to an SSM leaf
    (``aws-region``).

    HCL variable names use underscores; SSM leaf names use hyphens
    in this project. The mapping is mechanical.
    """
    return tfvars_key.replace("_", "-")


def _pick_heredoc_marker(value: str) -> str:
    """Return a heredoc marker not present on any line of ``value``.

    Tries ``BLITZLOG_BODY``, ``BLITZLOG_BODY_2``, …; raises
    ``ValueError`` if none of the first 1000 markers are absent
    (effectively impossible for real input, but a guard against
    pathological cases).
    """
    for i in range(1, 1001):
        marker = "BLITZLOG_BODY" if i == 1 else f"BLITZLOG_BODY_{i}"
        if not any(line.strip() == marker for line in value.splitlines()):
            return marker
    raise ValueError(
        "could not pick a unique heredoc marker in the first 1000 attempts; "
        "refusing to emit ambiguous output"
    )


def _render_entry(env: str, tfvars_key: str, value: str) -> str:
    """Render one ``<leaf> = <value>`` line inside the env map.

    Multi-line values are re-emitted as a heredoc; single-line
    values are written verbatim.
    """
    leaf = _to_leaf_key(tfvars_key)
    if "\n" in value:
        marker = _pick_heredoc_marker(value)
        return f"  {leaf} = <<{marker}\n{value}\n{marker}"
    return f"  {leaf} = {value}"


def render_tfvars_map(
    env: str,
    values: dict[str, str],
    source_path: Path,
    region: str,
) -> str:
    """Render one env's bootstrap tfvars (the ``dev = { ... }`` file).

    Output shape::

        # Generated by scripts/tfvars-to-bootstrap.py
        # Source: <source_path>
        # Region: <region>
        # Variables: N
        #
        # This file is gitignored. To regenerate, re-run the script.
        # Apply with:
        #     cd infra/bootstrap
        #     terraform apply \\
        #         -var-file=terraform.dev.tfvars \\
        #         -var-file=terraform.prod.tfvars
        #
        dev = {
          <leaf> = <value>
          ...
        }
    """
    body_lines = [
        "# Generated by scripts/tfvars-to-bootstrap.py",
        f"# Source: {source_path}",
        f"# Region: {region}",
        f"# Variables: {len(values)}",
        "#",
        "# This file is gitignored. To regenerate after editing the",
        f"# source tfvars ({source_path}), re-run the script.",
        "#",
        "# Apply with:",
        "#     cd infra/bootstrap",
        "#     terraform apply \\",
        "#         -var-file=terraform.dev.tfvars \\",
        "#         -var-file=terraform.prod.tfvars",
        "",
        f"{env} = {{",
    ]
    for key, value in values.items():
        body_lines.append(_render_entry(env, key, value))
    body_lines.append("}")
    body_lines.append("")
    return "\n".join(body_lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "dev_tfvars",
        type=Path,
        help="Path to infra/dev/terraform.tfvars",
    )
    parser.add_argument(
        "prod_tfvars",
        type=Path,
        help="Path to infra/prod/terraform.tfvars",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("infra/bootstrap"),
        help="Directory to write terraform.dev.tfvars and "
        "terraform.prod.tfvars into (default: infra/bootstrap). "
        "Both files are overwritten on each run.",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region to record in the output headers "
        "(default: $AWS_REGION or aws cli default).",
    )
    args = parser.parse_args()

    for path in (args.dev_tfvars, args.prod_tfvars):
        if not path.exists():
            print(f"error: {path} does not exist", file=sys.stderr)
            return 1

    try:
        dev_values = parse_tfvars(args.dev_tfvars.read_text())
    except ValueError as exc:
        print(f"error parsing {args.dev_tfvars}: {exc}", file=sys.stderr)
        return 1
    try:
        prod_values = parse_tfvars(args.prod_tfvars.read_text())
    except ValueError as exc:
        print(f"error parsing {args.prod_tfvars}: {exc}", file=sys.stderr)
        return 1

    region = args.region or "unset"

    args.out_dir.mkdir(parents=True, exist_ok=True)

    dev_out_path = args.out_dir / "terraform.dev.tfvars"
    prod_out_path = args.out_dir / "terraform.prod.tfvars"

    dev_out_path.write_text(
        render_tfvars_map("dev", dev_values, args.dev_tfvars, region)
    )
    prod_out_path.write_text(
        render_tfvars_map("prod", prod_values, args.prod_tfvars, region)
    )

    print(f"Wrote {dev_out_path} ({len(dev_values)} variables)")
    print(f"Wrote {prod_out_path} ({len(prod_values)} variables)")
    print()
    print("To apply:")
    print(f"  cd {args.out_dir}")
    print("  terraform apply \\")
    print(f"      -var-file={dev_out_path.name} \\")
    print(f"      -var-file={prod_out_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
