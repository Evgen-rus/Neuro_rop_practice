from __future__ import annotations

import unittest
from pathlib import Path


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = _normalize((ROOT / "deploy" / "deploy-production.sh").read_text(encoding="utf-8"))
WORKFLOW = _normalize(
    (ROOT / ".github" / "workflows" / "deploy-main.yml").read_text(encoding="utf-8")
)


class ProductionDeployTests(unittest.TestCase):
    def test_script_keeps_runtime_and_loopback_contract(self) -> None:
        self.assertTrue(SCRIPT.startswith("#!/usr/bin/env bash\n"))
        self.assertIn('API_CONTAINER="neuro-rop-api"', SCRIPT)
        self.assertIn('WEB_CONTAINER="neuro-rop-web"', SCRIPT)
        self.assertIn('NETWORK="neuro-rop-practice-net"', SCRIPT)
        self.assertIn("127.0.0.1:", SCRIPT)
        self.assertIn("18081", SCRIPT)
        self.assertIn('--restart unless-stopped', SCRIPT)

    def test_deploy_recreates_only_application_containers(self) -> None:
        self.assertIn(
            'docker rm --force "${WEB_CONTAINER}" "${API_CONTAINER}"',
            SCRIPT,
        )
        forbidden = (
            "neuro-rop-tunnel",
            "neurorop-demo",
            "docker compose down",
            "docker-compose down",
            "docker system prune",
            "docker image prune",
            "certbot",
            "systemctl",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, SCRIPT)

    def test_workflow_uses_production_backend_and_keeps_safety_checks(self) -> None:
        self.assertIn("./deploy/deploy-production.sh", WORKFLOW)
        self.assertNotIn("./deploy/temporary-tunnel.sh", WORKFLOW)
        self.assertNotIn("trycloudflare.com", WORKFLOW)
        self.assertIn("https://neurorop.leadrecordwh.ru", WORKFLOW)
        self.assertIn("StrictHostKeyChecking=yes", WORKFLOW)
        self.assertIn("git merge --ff-only", WORKFLOW)
        self.assertIn("flock -n 9", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
