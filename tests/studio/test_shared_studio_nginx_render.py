from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


class SharedStudioNginxRenderTests(unittest.TestCase):
    def test_gateway_access_log_omits_query_strings_and_cookies(self) -> None:
        root = Path(__file__).resolve().parents[2]
        launcher = (root / "deploy" / "shared-studio" / "nginx.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(r"\$request_method \$uri \$server_protocol", launcher)
        self.assertNotIn(r"\$request_uri", launcher)
        self.assertNotIn(r"\$http_cookie", launcher)
        self.assertIn("access.log optpilot_safe", launcher)
        self.assertIn("limit_req_zone $binary_remote_addr", launcher)

    def test_every_listener_is_tls_allowlisted_and_fail_closed(self) -> None:
        root = Path(__file__).resolve().parents[2]
        renderer = root / "deploy" / "shared-studio" / "render_nginx.py"
        environment = {
            **os.environ,
            "PUBLIC_BIND_IP": "127.0.0.2",
            "PUBLIC_HOST": "studio.example.edu",
            "PUBLIC_SERVER_NAME": "studio.example.edu",
            "STUDIO_PORT": "28666",
            "TLS_CERTIFICATE": "/private/test/fullchain.pem",
            "TLS_CERTIFICATE_KEY": "/private/test/privkey.pem",
            "ALLOWED_CIDRS": "127.0.0.1/32 166.111.0.0/16",
            "WORKSPACE_RUNTIME_PORT_START": "28766",
            "WORKSPACE_RUNTIME_PORT_COUNT": "3",
            "PREVIEW_PORT_OFFSET": "1000",
        }
        completed = subprocess.run(
            [sys.executable, str(renderer)],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        rendered = completed.stdout
        self.assertEqual(rendered.count("server {"), 7)
        self.assertEqual(rendered.count(" ssl;"), 7)
        self.assertEqual(
            rendered.count("if ($host != studio.example.edu) { return 444; }"),
            7,
        )
        self.assertIn("X-OptPilot-Target-Kind studio", rendered)
        self.assertEqual(
            rendered.count("limit_req zone=optpilot_login_per_ip"), 2
        )
        self.assertIn("location = /register", rendered)
        self.assertIn("location = /api/auth/register", rendered)
        self.assertEqual(rendered.count("X-OptPilot-Target-Kind code"), 3)
        self.assertEqual(rendered.count("X-OptPilot-Target-Kind presentation"), 3)
        self.assertEqual(rendered.count("auth_request /__optpilot_auth;"), 8)
        self.assertEqual(
            rendered.count("error_page 401 = @login_required;"), 7
        )
        self.assertEqual(rendered.count("location @login_required"), 7)
        self.assertIn('proxy_set_header Cookie "";', rendered)
        self.assertIn("proxy_hide_header Set-Cookie;", rendered)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", rendered)
        self.assertNotIn("$proxy_add_x_forwarded_for", rendered)
        self.assertNotIn("auth_basic", rendered)
        self.assertNotIn("28681", rendered)
        self.assertNotIn("0.0.0.0", rendered)
        self.assertEqual(rendered.count("deny all;"), 13)

    def test_overlapping_port_ranges_are_rejected(self) -> None:
        root = Path(__file__).resolve().parents[2]
        renderer = root / "deploy" / "shared-studio" / "render_nginx.py"
        environment = {
            **os.environ,
            "PUBLIC_BIND_IP": "127.0.0.2",
            "PUBLIC_HOST": "studio.example.edu",
            "STUDIO_PORT": "28767",
            "TLS_CERTIFICATE": "/private/test/fullchain.pem",
            "TLS_CERTIFICATE_KEY": "/private/test/privkey.pem",
            "ALLOWED_CIDRS": "127.0.0.1/32",
            "WORKSPACE_RUNTIME_PORT_START": "28766",
            "WORKSPACE_RUNTIME_PORT_COUNT": "3",
            "PREVIEW_PORT_OFFSET": "2",
        }
        completed = subprocess.run(
            [sys.executable, str(renderer)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must not overlap", completed.stderr)

    def test_wildcard_or_backend_bind_address_is_rejected(self) -> None:
        root = Path(__file__).resolve().parents[2]
        renderer = root / "deploy" / "shared-studio" / "render_nginx.py"
        base_environment = {
            **os.environ,
            "PUBLIC_HOST": "studio.example.edu",
            "STUDIO_PORT": "28666",
            "TLS_CERTIFICATE": "/private/test/fullchain.pem",
            "TLS_CERTIFICATE_KEY": "/private/test/privkey.pem",
            "ALLOWED_CIDRS": "127.0.0.1/32",
            "WORKSPACE_RUNTIME_PORT_START": "28766",
            "WORKSPACE_RUNTIME_PORT_COUNT": "3",
            "PREVIEW_PORT_OFFSET": "1000",
        }
        for bind_ip in ("0.0.0.0", "127.0.0.1"):
            with self.subTest(bind_ip=bind_ip):
                completed = subprocess.run(
                    [sys.executable, str(renderer)],
                    env={**base_environment, "PUBLIC_BIND_IP": bind_ip},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
