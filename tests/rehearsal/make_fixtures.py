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
import os
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
import urllib.parse
import uuid
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

# The gateway advertises one URL per companion service, and reads each from the
# environment -- which systemd supplies through EnvironmentFile=. When a
# variable is missing it falls back to loopback, exactly as the real portal
# does, and that fallback IS the v1.4.0 failure: every catalog link worked from
# the server and from nowhere else. The rehearsal can only assert that the
# environment file reached the process if the fixture reproduces it.
CATALOG = @CATALOG@

# Startup diagnostics in the shape the real hydride service emits them. Both
# outcomes are reachable, because a deployment check that has only ever seen
# the happy line is not a check.
REQUIRES_MODELS = @REQUIRES_MODELS@
if REQUIRES_MODELS:
    if os.path.exists(os.path.join("frozen_checkpoints", "model_registry.json")):
        sys.stderr.write("Model preload finished (1 model)\\n")
    else:
        sys.stderr.write(
            "Warm load failed for hydride_ml: model reference could not be "
            "resolved from the registry\\n"
        )
    if not os.path.isdir("test_library") or not os.listdir("test_library"):
        sys.stderr.write(
            "Image library is unavailable at ./test_library; falling back to "
            "the 2 configured example image(s)\\n"
        )
    sys.stderr.flush()


# The admin console, reproduced only as far as its session and CSRF mechanics.
#
# health_check.sh asserts the GET -> POST login lifecycle, because "/health is
# 200" was exactly the check that passed while nobody could sign in. A stub that
# serves no /admin/login would make that assertion untestable here, so the
# gateway fixture issues a real session cookie, renders a real token into the
# form, and refuses a POST whose token does not match the cookie -- giving the
# same two distinguishable answers the portal gives.
IS_GATEWAY = SERVICE == "gateway"
ADMIN_SESSIONS = {}
# Mirrors the portal in plain-HTTP intranet mode: NOT Secure, because a Secure
# cookie is never returned over HTTP and that is the failure being guarded.
ADMIN_COOKIE = "session={value}; HttpOnly; Path=/; SameSite=Lax"


def admin_login_page(token):
    return (
        "<html><body><h1>Administrator sign in</h1>"
        "<form method='post' action='/admin/login'>"
        '<input type="hidden" name="csrf_token" value="' + token + '">'
        "<input type='password' name='password'>"
        "</form></body></html>"
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{SERVICE} {fmt % args}\\n")

    def _send(self, code, body, content_type="text/html", cookie=None):
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def _cookies(self):
        jar = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                name, _, value = part.strip().partition("=")
                jar[name] = value
        return jar

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not (IS_GATEWAY and path == "/admin/login"):
            self.send_error(404, "no such route in the fixture")
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        session_id = self._cookies().get("session", "")
        submitted = (form.get("csrf_token") or [""])[0]
        expected = ADMIN_SESSIONS.get(session_id)
        if not expected or not submitted or submitted != expected:
            # The production symptom, reproduced: the token could not be tied
            # back to the session that rendered it.
            self._send(200, "<html><body>This form expired.</body></html>")
            return
        # The token round-tripped and only the password is wrong, which is what
        # a healthy deployment answers and what health_check.sh reads as a pass.
        self._send(200, "<html><body>Incorrect password.</body></html>")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]

        if IS_GATEWAY and path == "/admin/login":
            session_id = uuid.uuid4().hex
            token = uuid.uuid4().hex
            ADMIN_SESSIONS[session_id] = token
            self._send(
                200,
                admin_login_page(token),
                cookie=ADMIN_COOKIE.format(value=session_id),
            )
            return

        body = ROUTES.get(path)

        if CATALOG and path == "/api/catalog":
            body = json.dumps({"tools": [
                {"id": tool["id"], "url": os.environ.get(tool["env"], tool["fallback"])}
                for tool in CATALOG
            ]})

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


def catalog_for(service_id: str, manifest: dict) -> list[dict]:
    """The gateway's catalog entries: which variable each URL comes from.

    Empty for every other service, so only the gateway serves a catalog.
    """
    if service_id != "gateway":
        return []
    entries = []
    for name, service in manifest["services"].items():
        variable = service.get("public_url_env")
        if not variable:
            continue
        entries.append({
            "id": name,
            "env": variable,
            "fallback": f"http://127.0.0.1:{service.get('port')}",
        })
    return entries


def build(out_dir: Path, version: str) -> Path:
    import json as json_module  # noqa: F401  (used for the seeded model registry)

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
    # The suite target is suffixed for the same reason the units are: a
    # rehearsal must not create, enable or restart the real ml-platform.target
    # on a machine that has a live deployment.
    runtime = manifest.setdefault("runtime", {})
    if runtime.get("systemd_target"):
        runtime["systemd_target"] = runtime["systemd_target"].replace(
            ".target", f"{UNIT_SUFFIX}.target")
    # Stub services stay on loopback; there is no reason to expose a rehearsal
    # to the network, whatever the real deployment does. The advertised address
    # matches, which is the honest answer for a deployment that really is
    # reachable only from this machine -- and it keeps health_check.sh's
    # catalog assertion correctly skipped rather than failing a correct answer.
    runtime["bind_host"] = "127.0.0.1"
    runtime["intranet_host"] = "127.0.0.1"

    for service in manifest["services"].values():
        # Ordering references other units by name, so they need the suffix too.
        for key in ("after", "wants"):
            if service.get(key):
                service[key] = [u.replace(".service", f"{UNIT_SUFFIX}.service")
                                for u in service[key]]
        if service.get("unit"):
            service["unit"] = service["unit"].replace(".service", f"{UNIT_SUFFIX}.service")
        if isinstance(service.get("port"), int):
            service["port"] = service["port"] + PORT_OFFSET

    if out_dir.exists():
        shutil.rmtree(out_dir)
    sources = out_dir / "sources"
    sources.mkdir(parents=True)

    # A stand-in for the trained checkpoints, which are not in git and never
    # travel in an archive. update.sh refuses to deploy without them, so the
    # rehearsal has to supply them the same way the office server does: from a
    # path outside the release that the manifest names as a seed source.
    seed_models = (out_dir / "seed" / "hydride_models").resolve()
    (seed_models / "promoted").mkdir(parents=True)
    (seed_models / "model_registry.json").write_text(
        json_module.dumps({"models": {"hydride_ml": {"checkpoint": "promoted/fixture.pt"}}}, indent=2),
        encoding="utf-8", newline="\n",
    )
    (seed_models / "promoted" / "fixture.pt").write_bytes(b"fixture checkpoint\n")

    for seed in manifest.get("seeds") or []:
        if seed.get("id") == "hydride-models":
            seed["sources"] = [str(seed_models)]

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
            .replace("@CATALOG@", repr(catalog_for(service_id, manifest)))
            .replace("@REQUIRES_MODELS@", repr(bool(service.get("requires_models"))))
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

        # The documentation build is real machinery -- the commit stamp, the
        # atomic swap, the marker check, the refusal to fail a deployment --
        # and all of it is exercised here against a command that finishes
        # instantly. Running Sphinx in a rehearsal would take longer than every
        # other scenario put together and would be testing Sphinx.
        #
        # ML_FIXTURE_DOCS_FAIL makes the build fail on purpose, which is how the
        # "a broken build cannot fail a deployment" scenario is written.
        if service.get("docs_build"):
            service["docs_build"] = {
                **service["docs_build"],
                "requirements": [],
                "timeout_seconds": 60,
                "command": (
                    'if [ -n "${ML_FIXTURE_DOCS_FAIL:-}" ]; then exit 3; fi; '
                    "mkdir -p {target} && "
                    "printf '<html>fixture docs for %s</html>' \"$(cat src/MARKER)\" "
                    "> {target}/index.html"
                ),
            }


    # The real configuration schema, carried into the gateway fixture.
    #
    # update.sh validates and migrates shared/config/config.intranet.json with
    # the code from the release being installed, so a fixture whose gateway has
    # no ml_server package silently skips that step -- and the rehearsal would
    # then prove nothing about the migration that actually runs in the office.
    # These two modules import nothing but the standard library, so copying them
    # costs nothing and keeps the rehearsal honest. The schema is not duplicated
    # here: it is the very file the portal uses.
    #
    # When no ml_server checkout is at hand the copy is skipped and the
    # config_migration scenario skips itself, rather than testing a stale copy.
    gateway_dir = manifest["services"]["gateway"].get("dir", "ml_server")
    source_root = Path(
        os.environ.get("ML_SERVER_SOURCE") or (REPO_ROOT.parent / "ml_server")
    )
    package = source_root / "src" / "ml_server"
    if (package / "config_cli.py").is_file():
        target = sources / gateway_dir / "src" / "ml_server"
        target.mkdir(parents=True, exist_ok=True)
        (target / "__init__.py").write_text("", encoding="utf-8", newline="\n")
        for name in ("config_schema.py", "config_cli.py"):
            # Bytes, not text: the file must reach the Ubuntu fixture exactly as
            # the release archive would deliver it, LF endings included.
            (target / name).write_bytes((package / name).read_bytes())
        print(f"fixture config schema: from {package}", file=sys.stderr)
    else:
        print(
            f"fixture config schema: SKIPPED, no ml_server checkout at {source_root}",
            file=sys.stderr,
        )

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
