#!/usr/bin/env python3
"""Build a fixture suite that mimics the real one for deployment rehearsals.

The rehearsal is testing the deployment machinery -- checksums, extraction,
atomic activation, restart selection, health gating, rollback -- not the
scientific code.  Installing torch and OpenCV into every rehearsal would add
gigabytes and many minutes for no additional coverage of the thing under test.

So each component is replaced by a stdlib-only HTTP stub that answers the same
health path on the same port as the real service, and the gateway stub also
serves the catalog, the mounted companion mounts, and the vendored MathJax
asset that health_check.sh asserts on.  The manifest keeps its real shape:
same service ids, ports, units, shared links and in-process relationships.

Two knobs let scenarios inject failure:

    ML_FIXTURE_FAIL_HEALTH=<service>   that service returns 500 from its health path
    ML_FIXTURE_CRASH=<service>         that service exits immediately at startup

    python tests/rehearsal/make_fixtures.py --out /tmp/fixtures --version 1.0.0
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# Keep fixture units and ports clear of any real deployment on the same host.
# These must match the values in run.sh.
UNIT_SUFFIX = "-rehearsal"
PORT_OFFSET = 2000

STUB = '''#!/usr/bin/env python3
"""Stand-in for the @SERVICE@ service in a deployment rehearsal.

Stdlib only, so it starts without any dependency installation and a rehearsal
can run with the package mirror deliberately switched off.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

SERVICE = "@SERVICE@"
VERSION = "@VERSION@"
COMMIT = "@COMMIT@"

# Scenario hooks. A rehearsal sets these to make a specific service misbehave
# so that the failure and rollback paths are exercised for real.
if os.environ.get("ML_FIXTURE_CRASH") == SERVICE:
    sys.stderr.write(f"{SERVICE}: ML_FIXTURE_CRASH set, exiting immediately\\n")
    sys.exit(3)

FAIL_HEALTH = os.environ.get("ML_FIXTURE_FAIL_HEALTH") == SERVICE

# Written as a Python literal rather than an embedded JSON string: quoting JSON
# inside a quoted template double-escapes every inner quote and the stub then
# fails to parse its own routing table.
ROUTES = @ROUTES@


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{SERVICE} {fmt % args}\\n")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = ROUTES.get(path)

        if body is None and path.rstrip("/") in ROUTES:
            body = ROUTES[path.rstrip("/")]
        if body is None and path + "/" in ROUTES:
            body = ROUTES[path + "/"]

        if body is None:
            self.send_error(404, "no such route in the fixture")
            return

        if FAIL_HEALTH and path in HEALTH_PATHS:
            payload = json.dumps({"status": "unhealthy", "service": SERVICE}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if body.startswith("{") else "text/html")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


HEALTH_PATHS = set(@HEALTH_PATHS@)


def main():
    port = int(os.environ.get("FIXTURE_PORT", "0"))
    for index, argument in enumerate(sys.argv):
        if argument == "--port" and index + 1 < len(sys.argv):
            port = int(sys.argv[index + 1])
    if not port:
        sys.stderr.write("no port given\\n")
        return 2
    server = HTTPServer(("127.0.0.1", port), Handler)
    sys.stderr.write(f"{SERVICE} {VERSION} ({COMMIT[:8]}) listening on 127.0.0.1:{port}\\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def routes_for(service_id: str, manifest: dict) -> tuple[dict[str, str], list[str]]:
    """Return the routes a stub must serve, and which of them are health paths."""
    services = manifest["services"]
    service = services[service_id]
    routes: dict[str, str] = {}
    health_paths: list[str] = []

    own_health = service.get("health")
    if own_health and not service.get("in_process"):
        routes[own_health] = '{"status": "ok", "service": "%s"}' % service_id
        health_paths.append(own_health)

    if service_id != "gateway":
        routes.setdefault("/", "<html><body>%s fixture</body></html>" % service_id)
        return routes, health_paths

    # The gateway additionally answers for everything mounted in-process, plus
    # the catalog and the offline-asset checks health_check.sh performs.
    for name, other in services.items():
        if other.get("in_process"):
            mount = other.get("mount")
            if mount:
                routes[mount] = "<html><body>%s mounted</body></html>" % name
            other_health = other.get("health")
            if other_health:
                routes[other_health] = '{"status": "ok", "service": "%s"}' % name
                health_paths.append(other_health)

    routes["/"] = "<html><body>portal fixture</body></html>"
    routes["/api/catalog"] = '{"tools": [%s]}' % ", ".join(
        '{"id": "%s"}' % name for name in services
    )
    # health_check.sh asserts the MathJax bundle is served locally and that no
    # help page references a CDN. Both are real regressions worth catching.
    routes["/static/vendor/mathjax/tex-chtml-full.js"] = "/* vendored MathJax fixture */"
    routes["/tools/pytex/help"] = (
        "<html><body><h1>PyTex help</h1>"
        "<script src='/static/vendor/mathjax/tex-chtml-full.js'></script>"
        "</body></html>"
    )
    return routes, health_paths


def build(out_dir: Path, version: str) -> Path:
    import json as json_module

    with (REPO_ROOT / "manifest.yml").open(encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)

    manifest["suite_version"] = version

    # The rehearsal environment has no torch and needs none: the stubs are
    # stdlib only. Clearing these keeps the fixture suite self-contained, and
    # keeps the offline scenarios genuinely offline.
    manifest["pip"] = {"extra_index_urls": [], "preinstalled": []}

    # Likewise the system packages: the stubs need none, and requiring sqlite3
    # or poppler here would make the rehearsal depend on what happens to be
    # installed on the developer's machine. The missing_prerequisite scenario
    # puts a requirement back deliberately to test the refusal path.
    manifest["system_requirements"] = []

    # Genuine isolation from any real deployment on the same machine.
    #
    # The harness used to install units under the real names and then delete
    # every ml-platform-*.service on the host between scenarios. On a developer
    # box that is merely untidy; on a server with a live deployment it destroys
    # it, which is exactly what happened here -- a staging deployment lost all
    # five of its unit files to a rehearsal run.
    #
    # Fixture units are suffixed and fixture ports are moved well clear, so a
    # rehearsal and a real deployment cannot touch each other at all.
    for service in manifest["services"].values():
        if service.get("unit"):
            service["unit"] = service["unit"].replace(".service", f"{UNIT_SUFFIX}.service")
        if isinstance(service.get("port"), int):
            service["port"] = service["port"] + PORT_OFFSET

    if out_dir.exists():
        shutil.rmtree(out_dir)
    sources = out_dir / "sources"
    sources.mkdir(parents=True)

    for service_id, service in manifest["services"].items():
        directory = service.get("dir", service_id)
        component = sources / directory
        (component / "src").mkdir(parents=True)

        routes, health_paths = routes_for(service_id, manifest)
        stub = (
            STUB.replace("@SERVICE@", service_id)
            .replace("@VERSION@", version)
            .replace("@COMMIT@", f"fixture-{service_id}")
            .replace("@ROUTES@", repr(routes))
            .replace("@HEALTH_PATHS@", repr(health_paths))
        )
        (component / "app_stub.py").write_text(stub, encoding="utf-8", newline="\n")
        (component / "README.md").write_text(
            f"# {service_id} fixture\n\nRehearsal stand-in for {service.get('repo')}.\n",
            encoding="utf-8", newline="\n",
        )
        # A marker file whose content changes with the version, so scenarios can
        # prove which release a file came from.
        (component / "src" / "MARKER").write_text(
            f"{service_id} {version}\n", encoding="utf-8", newline="\n"
        )

        # Rewrite the service to run the stub instead of the real application.
        if not service.get("in_process"):
            service["start"] = "{venv}/bin/python app_stub.py --port {port}"
        service["requirements"] = []
        service.pop("tests", None)
        service.pop("npm_build", None)

    # The gateway's declared route checks must match what the stub serves.
    manifest["services"]["gateway"]["gateway_checks"] = [
        "/api/catalog",
        "/pdf_tools/",
        "/tabular_ml/",
        "/static/vendor/mathjax/tex-chtml-full.js",
    ]

    manifest_path = out_dir / "manifest.fixture.yml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False),
        encoding="utf-8", newline="\n",
    )

    print(f"fixture sources: {sources}", file=sys.stderr)
    print(f"fixture manifest: {manifest_path}", file=sys.stderr)
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", default="1.0.0")
    args = parser.parse_args()
    build(args.out, args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
