from __future__ import annotations

import unittest

from scripts.build_agent_policies import build_output


class PolicyCompilerTests(unittest.TestCase):
    def test_build_output_has_generated_header(self) -> None:
        rendered = build_output("AGENTS.md", ["docs/policy/core.md", "docs/policy/agents.overlay.md"])

        self.assertIn("GENERATED FILE. DO NOT EDIT", rendered)
        self.assertIn("checksum: sha256:", rendered)
        self.assertIn("# Core agent policy", rendered)
