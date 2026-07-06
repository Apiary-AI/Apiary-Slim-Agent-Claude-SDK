"""Tests for entrypoint.sh — verify boot-time paths honour CLAUDE_WORKING_DIR."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"

# Minimum agent-core version the entrypoint depends on. Bump this in lockstep
# with any new core API the entrypoint starts depending on.
#
# 0.1.2  — ships the `github_auth` module the entrypoint invokes.
# 0.1.12 — module_setup honours `--skills-dir` (registry skills overlay) AND
#          warms the last-good /registry/resolved cache on a successful fetch
#          (resilient fetch with retry + cache). The entrypoint now passes
#          `--skills-dir`, so the floor must guarantee both.
MIN_CORE_VERSION = (0, 1, 12)


def _parse_lower_bound(specifier: str) -> tuple[int, ...]:
    """Extract the numeric lower bound from a PEP 440 specifier like
    ``~=0.1.2`` or ``>=0.1.2`` so it can be compared against MIN_CORE_VERSION."""
    match = re.search(r"(?:~=|>=|==)\s*(\d+(?:\.\d+)*)", specifier)
    assert match, f"no lower-bound version found in specifier: {specifier!r}"
    return tuple(int(p) for p in match.group(1).split("."))


def _read_entrypoint() -> str:
    return ENTRYPOINT.read_text()


class TestWorkingDirVariable:
    """WORKING_DIR must be derived from CLAUDE_WORKING_DIR with a /workspace default."""

    def test_working_dir_defined(self):
        text = _read_entrypoint()
        assert re.search(
            r'WORKING_DIR="\$\{CLAUDE_WORKING_DIR:-/workspace\}"', text
        ), "entrypoint.sh must define WORKING_DIR from CLAUDE_WORKING_DIR"


class TestSubAgentSyncPaths:
    """The sub-agent sync block must use $WORKING_DIR, not hard-coded /workspace."""

    def _sync_block(self) -> str:
        """Return only the sync_sub_agents invocation block."""
        text = _read_entrypoint()
        match = re.search(
            r"(python3 /app/src/sync_sub_agents\.py\b.*?)(?:\n(?!\s)|\Z)",
            text,
            re.DOTALL,
        )
        assert match, "Could not locate sync_sub_agents.py invocation in entrypoint.sh"
        return match.group(1)

    def test_subagents_dir_uses_working_dir(self):
        block = self._sync_block()
        assert '"$WORKING_DIR/.claude/subagents"' in block

    def test_modules_dir_uses_working_dir(self):
        block = self._sync_block()
        assert '"$WORKING_DIR/.claude/modules"' in block

    def test_skills_dir_uses_working_dir(self):
        block = self._sync_block()
        assert '"$WORKING_DIR/.claude/skills"' in block

    def test_no_hardcoded_workspace_in_sync_block(self):
        block = self._sync_block()
        # After stripping the WORKING_DIR references there should be no
        # remaining /workspace paths in the sync block.
        cleaned = block.replace("$WORKING_DIR", "")
        assert "/workspace" not in cleaned, (
            "Sub-agent sync block still contains hard-coded /workspace paths"
        )


class TestModuleSetupPaths:
    """The module_setup block must use $WORKING_DIR so it agrees with the
    downstream sync block. If module_setup writes to /workspace/.claude/modules
    but sync scans $WORKING_DIR/.claude/modules, an overridden CLAUDE_WORKING_DIR
    silently loses the installed-module context."""

    def _module_setup_block(self) -> str:
        text = _read_entrypoint()
        match = re.search(
            r"(python3 -m superpos_agent_core\.module_setup\b.*?)(?:\n(?!\s)|\Z)",
            text,
            re.DOTALL,
        )
        assert match, "Could not locate module_setup invocation in entrypoint.sh"
        return match.group(1)

    def test_modules_dir_uses_working_dir(self):
        block = self._module_setup_block()
        assert '"$WORKING_DIR/.claude/modules"' in block

    def test_agents_md_uses_working_dir(self):
        block = self._module_setup_block()
        assert '"$WORKING_DIR/CLAUDE.md"' in block

    def test_bin_dir_uses_working_dir(self):
        block = self._module_setup_block()
        assert '"$WORKING_DIR/.claude/modules-bin"' in block

    def test_skills_dir_passed(self):
        """module_setup must receive --skills-dir so the registry SKILLS
        overlay runs (and the last-good /registry/resolved cache is warmed).
        Without it, module_setup overlays registry *modules* only and silently
        skips the skills half, leaving agents on baked-in skills alone — the
        latent bug this fixes and the Beat 4 prerequisite."""
        block = self._module_setup_block()
        assert "--skills-dir" in block, (
            "module_setup must be invoked with --skills-dir so registry skills "
            "are overlaid and the registry-resolved cache is warmed on boot"
        )

    def test_skills_dir_uses_working_dir(self):
        block = self._module_setup_block()
        assert '"$WORKING_DIR/.claude/skills"' in block, (
            "module_setup --skills-dir must derive from $WORKING_DIR so it "
            "agrees with the sync_sub_agents --skills-dir scan target"
        )

    def test_no_hardcoded_workspace_in_module_setup_block(self):
        block = self._module_setup_block()
        cleaned = block.replace("$WORKING_DIR", "")
        assert "/workspace" not in cleaned, (
            "module_setup block still contains hard-coded /workspace paths"
        )


class TestCoreBundledFallback:
    """After module_setup, entrypoint.sh must re-link core-bundled module
    scripts so they survive a module_setup failure (degraded-mode).  Without
    this fallback, tools that only exist in the core package (e.g.
    superpos-knowledge after its workspace copy was removed) vanish from PATH
    when a workspace setup.sh hook fails and module_setup aborts early."""

    def _fallback_block(self) -> str:
        text = _read_entrypoint()
        match = re.search(
            r"(CORE_MODULES_DIR=.*?)(?=\n# \w|\nexec |\Z)",
            text,
            re.DOTALL,
        )
        assert match, (
            "Could not locate core-bundled fallback block in entrypoint.sh"
        )
        return match.group(1)

    def test_fallback_exists_after_module_setup(self):
        text = _read_entrypoint()
        setup_pos = text.find("python3 -m superpos_agent_core.module_setup")
        fallback_pos = text.find("CORE_MODULES_DIR=")
        assert setup_pos != -1, "module_setup invocation not found"
        assert fallback_pos != -1, "core-bundled fallback block not found"
        assert fallback_pos > setup_pos, (
            "core-bundled fallback must appear AFTER module_setup"
        )

    def test_fallback_discovers_core_package_modules(self):
        block = self._fallback_block()
        assert "superpos_agent_core" in block, (
            "fallback must discover modules from the superpos_agent_core package"
        )

    def test_fallback_symlinks_into_working_dir(self):
        block = self._fallback_block()
        assert "$WORKING_DIR/.claude/modules-bin" in block, (
            "fallback must symlink scripts into $WORKING_DIR/.claude/modules-bin"
        )

    def test_fallback_tolerates_errors(self):
        """The fallback itself must not crash the entrypoint if the core
        package is somehow missing or unimportable."""
        block = self._fallback_block()
        assert "|| true" in block, (
            "fallback must tolerate failures (|| true) to avoid crashing the entrypoint"
        )

    def test_fallback_restores_bundled_tools_when_module_setup_aborts_early(
        self, tmp_path
    ):
        """Reproduce the degraded-mode bug: a workspace ``setup.sh`` exits
        non-zero, ``module_setup`` aborts before ``symlink_module_scripts()``
        runs, so ``$WORKING_DIR/.claude/modules-bin/`` never gets created.
        The fallback block in ``entrypoint.sh`` must still restore bundled
        scripts (e.g. ``superpos-knowledge``) onto PATH — which requires it
        to create the bin dir itself before relinking."""
        # Skip if the core package isn't importable in this environment.
        try:
            import superpos_agent_core  # noqa: F401
        except Exception:  # pragma: no cover - environment guard
            pytest.skip("superpos_agent_core not importable in this env")

        core_modules_dir = Path(
            subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import superpos_agent_core; "
                    "print(Path(superpos_agent_core.__file__).parent / 'modules')",
                ],
                text=True,
            ).strip()
        )
        if not (core_modules_dir / "superpos-knowledge" / "scripts" / "superpos-knowledge").exists():
            pytest.skip("bundled superpos-knowledge script not available")

        # Fresh CLAUDE_WORKING_DIR with NO pre-existing modules-bin/.
        working_dir = tmp_path / "workspace"
        modules_dir = working_dir / ".claude" / "modules"
        modules_dir.mkdir(parents=True)
        agents_md = working_dir / "CLAUDE.md"
        agents_md.write_text("# Stub\n")

        # A workspace module whose setup.sh exits non-zero — this makes
        # module_setup raise mid-way (run_setup_scripts uses check=True),
        # so symlink_module_scripts() never runs.
        failing_module = modules_dir / "failing-mod"
        failing_module.mkdir()
        (failing_module / "module.yaml").write_text("name: failing-mod\n")
        setup = failing_module / "setup.sh"
        setup.write_text("#!/bin/bash\nexit 1\n")
        setup.chmod(0o755)

        bin_dir = working_dir / ".claude" / "modules-bin"
        assert not bin_dir.exists(), "precondition: modules-bin must not pre-exist"

        # Extract the actual entrypoint snippet that runs module_setup +
        # the core-bundled fallback, so the test exercises the real shell
        # we ship rather than a paraphrase.
        text = ENTRYPOINT.read_text()
        setup_match = re.search(
            r"(python3 -m superpos_agent_core\.module_setup\b.*?\|\| echo[^\n]*)",
            text,
            re.DOTALL,
        )
        fallback_match = re.search(
            r"(CORE_MODULES_DIR=.*?\nfi)\n", text, re.DOTALL
        )
        assert setup_match and fallback_match, "could not extract entrypoint blocks"

        script = textwrap.dedent(
            f"""
            #!/bin/bash
            set -e
            WORKING_DIR={working_dir!s}
            {setup_match.group(1)}
            {fallback_match.group(1)}
            """
        )
        script_path = tmp_path / "run.sh"
        script_path.write_text(script)
        script_path.chmod(0o755)

        env = os.environ.copy()
        # Ensure the subprocess can import superpos_agent_core the same way
        # the in-test import succeeded (covers editable installs).
        env["PYTHONPATH"] = os.pathsep.join(
            [p for p in (env.get("PYTHONPATH"), *sys.path) if p]
        )
        result = subprocess.run(
            ["bash", str(script_path)],
            env=env,
            capture_output=True,
            text=True,
        )
        # The entrypoint must not crash overall; module_setup is allowed to
        # warn (|| echo ...) and the fallback must succeed regardless.
        assert result.returncode == 0, (
            f"fallback script crashed: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )

        # The bug: without `mkdir -p` in the fallback, this symlink would
        # never get created because modules-bin didn't exist.
        knowledge_link = bin_dir / "superpos-knowledge"
        assert bin_dir.is_dir(), (
            "fallback must create modules-bin/ even when module_setup aborted "
            "before symlink_module_scripts() ran"
        )
        assert knowledge_link.exists(), (
            "fallback must restore bundled superpos-knowledge onto PATH "
            "after module_setup aborts in the degraded path"
        )

    def test_bundled_tools_discoverable_with_overridden_working_dir(
        self, tmp_path
    ):
        """When CLAUDE_WORKING_DIR points to a non-default directory and
        module_setup fails, the bundled tools must still be *discoverable*
        (on PATH), not merely symlinked.  The Dockerfile hard-codes
        ``/workspace/.claude/modules-bin`` on PATH; if CLAUDE_WORKING_DIR
        is something else, the runtime ``export PATH=…`` in entrypoint.sh
        must add the correct directory so ``which <tool>`` succeeds."""
        try:
            import superpos_agent_core  # noqa: F401
        except Exception:  # pragma: no cover - environment guard
            pytest.skip("superpos_agent_core not importable in this env")

        core_modules_dir = Path(
            subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import superpos_agent_core; "
                    "print(Path(superpos_agent_core.__file__).parent / 'modules')",
                ],
                text=True,
            ).strip()
        )
        if not (core_modules_dir / "superpos-knowledge" / "scripts" / "superpos-knowledge").exists():
            pytest.skip("bundled superpos-knowledge script not available")

        # Use a non-default working dir to prove the PATH export is dynamic.
        working_dir = tmp_path / "custom-workdir"
        modules_dir = working_dir / ".claude" / "modules"
        modules_dir.mkdir(parents=True)
        agents_md = working_dir / "CLAUDE.md"
        agents_md.write_text("# Stub\n")

        # A failing workspace module forces the degraded (fallback) path.
        failing_module = modules_dir / "failing-mod"
        failing_module.mkdir()
        (failing_module / "module.yaml").write_text("name: failing-mod\n")
        setup = failing_module / "setup.sh"
        setup.write_text("#!/bin/bash\nexit 1\n")
        setup.chmod(0o755)

        # Extract the real entrypoint blocks we ship.
        text = ENTRYPOINT.read_text()
        setup_match = re.search(
            r"(python3 -m superpos_agent_core\.module_setup\b.*?\|\| echo[^\n]*)",
            text,
            re.DOTALL,
        )
        fallback_match = re.search(
            r"(CORE_MODULES_DIR=.*?\nfi)\n", text, re.DOTALL
        )
        path_export_match = re.search(
            r'(export PATH="\$WORKING_DIR/\.claude/modules-bin:\$PATH")',
            text,
        )
        assert setup_match and fallback_match and path_export_match, (
            "could not extract entrypoint blocks"
        )

        # Build a minimal script that mirrors the entrypoint flow and then
        # checks discoverability via `which`.
        script = textwrap.dedent(
            f"""
            #!/bin/bash
            set -e
            WORKING_DIR={working_dir!s}
            {path_export_match.group(1)}
            {setup_match.group(1)}
            {fallback_match.group(1)}
            # The real assertion: the tool must be discoverable on PATH.
            which superpos-knowledge
            """
        )
        script_path = tmp_path / "run.sh"
        script_path.write_text(script)
        script_path.chmod(0o755)

        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [p for p in (env.get("PYTHONPATH"), *sys.path) if p]
        )
        # Deliberately strip /workspace/.claude/modules-bin from PATH so
        # only the dynamic export can make the tool discoverable.
        env["PATH"] = os.pathsep.join(
            p
            for p in env.get("PATH", "").split(os.pathsep)
            if p != "/workspace/.claude/modules-bin"
        )

        result = subprocess.run(
            ["bash", str(script_path)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"superpos-knowledge not discoverable on PATH after entrypoint "
            f"with overridden CLAUDE_WORKING_DIR: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        # `which` output must point into the custom working dir, not /workspace.
        assert str(working_dir) in result.stdout, (
            f"tool resolved to wrong directory: {result.stdout!r}"
        )


class TestPathContractAgreement:
    """module_setup and sync_sub_agents must point at the same module/skill
    roots, otherwise an overridden CLAUDE_WORKING_DIR causes the sync step to
    scan an empty directory."""

    def test_modules_dir_matches_between_blocks(self):
        text = _read_entrypoint()
        setup_match = re.search(
            r"--modules-dir\s+(\S+)\s*\\?\s*\n[^\n]*--agents-md", text
        )
        sync_match = re.search(
            r"sync_sub_agents\.py\b.*?--modules-dir\s+(\S+)", text, re.DOTALL
        )
        assert setup_match and sync_match
        assert setup_match.group(1) == sync_match.group(1), (
            "module_setup --modules-dir and sync_sub_agents --modules-dir "
            "must agree so an overridden CLAUDE_WORKING_DIR keeps them aligned"
        )

    def test_skills_dir_matches_between_blocks(self):
        text = _read_entrypoint()
        setup_match = re.search(
            r"module_setup\b.*?--skills-dir\s+(\S+)", text, re.DOTALL
        )
        sync_match = re.search(
            r"sync_sub_agents\.py\b.*?--skills-dir\s+(\S+)", text, re.DOTALL
        )
        assert setup_match and sync_match, (
            "both module_setup and sync_sub_agents must pass --skills-dir"
        )
        assert setup_match.group(1) == sync_match.group(1), (
            "module_setup --skills-dir and sync_sub_agents --skills-dir must "
            "agree so the registry overlay materialises skills into the same "
            "dir the sub-agent sync scans"
        )


class TestAgentCoreVersionPin:
    """entrypoint.sh invokes ``superpos_agent_core.github_auth``, which only
    exists in agent-core >= 0.1.2. The declared dependency floor *and* the
    lockfile must therefore resolve to at least 0.1.2 — otherwise a lockfile
    install, a minimum-version resolution, or a cached Docker layer pulls
    0.1.1, where ``github_auth`` is missing and the entrypoint crashes."""

    def test_entrypoint_uses_github_auth(self):
        """Guard the premise: if the entrypoint stops importing github_auth,
        the version-floor assertions below should be revisited."""
        text = _read_entrypoint()
        assert "superpos_agent_core.github_auth" in text, (
            "entrypoint no longer invokes github_auth — revisit MIN_CORE_VERSION"
        )

    def test_requirements_txt_floor(self):
        text = (REPO_ROOT / "requirements.txt").read_text()
        match = re.search(r"^superpos-agent-core\s*(\S+)", text, re.MULTILINE)
        assert match, "superpos-agent-core not pinned in requirements.txt"
        assert _parse_lower_bound(match.group(1)) >= MIN_CORE_VERSION, (
            "requirements.txt must require agent-core >= 0.1.2 for github_auth"
        )

    def test_pyproject_floor(self):
        text = (REPO_ROOT / "pyproject.toml").read_text()
        match = re.search(r'"superpos-agent-core\s*([^"]+)"', text)
        assert match, "superpos-agent-core not pinned in pyproject.toml"
        assert _parse_lower_bound(match.group(1)) >= MIN_CORE_VERSION, (
            "pyproject.toml must require agent-core >= 0.1.2 for github_auth"
        )

    def test_uv_lock_resolves_at_least_min_version(self):
        text = (REPO_ROOT / "uv.lock").read_text()
        match = re.search(
            r'name = "superpos-agent-core"\s*\nversion = "([^"]+)"', text
        )
        assert match, "superpos-agent-core not present in uv.lock"
        locked = tuple(int(p) for p in match.group(1).split("."))
        assert locked >= MIN_CORE_VERSION, (
            f"uv.lock pins agent-core {match.group(1)}, but the entrypoint's "
            "github_auth call needs >= 0.1.2"
        )


class TestGithubPrModule:
    """The github-pr module must clone/push over HTTPS via the credential helper
    (GitHub App connection), not depend on a static GITHUB_TOKEN. Regression
    guard: agents used to read SKILL.md's "GITHUB_TOKEN must be set" and fall
    back to a read-only proxy tarball for cloning."""

    MODULE_DIR = REPO_ROOT / "workspace" / ".claude" / "modules" / "github-pr"
    SCRIPTS_DIR = MODULE_DIR / "scripts"

    def test_scripts_parse(self):
        """Both scripts must parse under bash -n."""
        for script in ("clone-and-branch.sh", "push-and-pr.sh"):
            path = self.SCRIPTS_DIR / script
            result = subprocess.run(
                ["bash", "-n", str(path)],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, (
                f"{script} failed bash -n: {result.stderr}"
            )

    def test_clone_and_branch_registers_credential_helper(self):
        """clone-and-branch.sh must (re-)run github_auth setup before cloning so
        the broker-backed credential helper is guaranteed registered even if
        entrypoint setup was skipped."""
        text = (self.SCRIPTS_DIR / "clone-and-branch.sh").read_text()
        assert "superpos_agent_core.github_auth setup" in text, (
            "clone-and-branch.sh must run github_auth setup before cloning"
        )
        # Must be idempotent/non-fatal — the clone still proceeds if setup fails
        # (e.g. a PAT-only agent with GITHUB_TOKEN already set).
        setup_line = next(
            line for line in text.splitlines()
            if "github_auth setup" in line
        )
        assert "|| true" in setup_line, (
            "github_auth setup must be non-fatal (|| true) so the clone still "
            "proceeds when setup fails on a PAT-only agent"
        )

    def test_clone_uses_plain_https_clone(self):
        """The clone must be a plain HTTPS clone (which the credential helper
        authenticates), not a token-in-URL or proxy fetch."""
        text = (self.SCRIPTS_DIR / "clone-and-branch.sh").read_text()
        assert "git clone" in text and "https://github.com/" in text, (
            "clone-and-branch.sh must clone over plain HTTPS from github.com"
        )

    def test_skill_does_not_require_github_token(self):
        """SKILL.md must not tell agents GITHUB_TOKEN is mandatory — that made
        App-connection agents fall back to the read-only proxy tarball."""
        text = (self.MODULE_DIR / "SKILL.md").read_text()
        assert "GITHUB_TOKEN` must be set" not in text, (
            "SKILL.md still says GITHUB_TOKEN must be set — App-connection "
            "agents will wrongly fall back to the proxy tarball"
        )
        assert "github_auth setup" in text, (
            "SKILL.md must document the github_auth setup credential-helper path"
        )
