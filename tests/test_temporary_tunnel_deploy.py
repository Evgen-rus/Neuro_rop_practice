from __future__ import annotations

import unittest
from pathlib import Path


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = _normalize((ROOT / "deploy" / "temporary-tunnel.sh").read_text(encoding="utf-8"))
WORKFLOW = _normalize(
    (ROOT / ".github" / "workflows" / "deploy-main.yml").read_text(encoding="utf-8")
)


class TemporaryTunnelDeployTests(unittest.TestCase):
    def test_script_keeps_unix_shebang_and_tunnel_name(self) -> None:
        self.assertTrue(SCRIPT.startswith("#!/usr/bin/env bash\n"))
        self.assertIn('TUNNEL_CONTAINER="neuro-rop-tunnel"', SCRIPT)
        self.assertIn('API_CONTAINER="neuro-rop-api"', SCRIPT)
        self.assertIn('WEB_CONTAINER="neuro-rop-web"', SCRIPT)

    def test_regular_deploy_recreates_only_application_containers(self) -> None:
        self.assertIn(
            'docker rm --force "${WEB_CONTAINER}" "${API_CONTAINER}"',
            SCRIPT,
        )
        self.assertIn('--restart unless-stopped', SCRIPT)
        self.assertIn("--show-url", SCRIPT)

    def test_regular_deploy_does_not_stop_or_recreate_tunnel(self) -> None:
        forbidden_commands = (
            'docker rm --force "${TUNNEL_CONTAINER}"',
            "docker rm --force ${TUNNEL_CONTAINER}",
            'docker stop "${TUNNEL_CONTAINER}"',
            'docker restart "${TUNNEL_CONTAINER}"',
            'docker kill "${TUNNEL_CONTAINER}"',
            "docker compose down",
            "docker-compose down",
        )
        for command in forbidden_commands:
            with self.subTest(command=command):
                self.assertNotIn(command, SCRIPT)

        force_remove_lines = [
            line.strip()
            for line in SCRIPT.splitlines()
            if "docker rm --force" in line
        ]
        self.assertEqual(len(force_remove_lines), 1)
        self.assertNotIn("TUNNEL_CONTAINER", force_remove_lines[0])

    def test_workflow_calls_script_and_does_not_touch_tunnel(self) -> None:
        self.assertIn("./deploy/temporary-tunnel.sh", WORKFLOW)
        self.assertNotIn("docker compose down", WORKFLOW)
        self.assertNotIn("docker-compose down", WORKFLOW)
        self.assertNotIn("docker rm --force neuro-rop-tunnel", WORKFLOW)
        self.assertNotIn("docker restart neuro-rop-tunnel", WORKFLOW)
