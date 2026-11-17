"""alloy/config.alloy end to end: Alloy scrapes a stand-in engine and pushes
to a real Prometheus through a proxy that records the credentials.

Checks what the receiving side depends on: series names with their colon,
the job names, the node identity labels, and the basic-auth user.
"""

import base64
import contextlib
import http.server
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from absl.testing import absltest
from prometheus_client import CollectorRegistry, MetricsHandler, parser

import tools

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "alloy/config.alloy"
FIXTURE = Path(__file__).resolve().parent / "fixtures/sglang.metrics.txt"

IDENTITY = {"node": "gpu-1", "cloud": "provider", "region": "site-a", "pool": "p", "role": "unified"}
PASSWORD = "pa$$word"
# Seconds to wait for a server's readiness URL.
READY_TIMEOUT = 60
# Seconds to wait for the engine's series in the receiving Prometheus: the
# first scrape lands within one 15s interval, then remote_write batches for
# up to 5s.
ARRIVAL_TIMEOUT = 60


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def started(name: str, cmd: list[str], ready_url: str, env: dict[str, str] | None = None):
    """Run `cmd` until `ready_url` answers 200; yields a function returning
    the process's output so far. The process is killed on exit: a graceful
    Alloy stop can wait a minute flushing queued samples."""
    with tempfile.TemporaryFile("w+") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)

        def output() -> str:
            fd = log.fileno()
            return os.pread(fd, os.fstat(fd).st_size, 0).decode(errors="replace")

        try:
            deadline = time.monotonic() + READY_TIMEOUT
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f"{name} exited (code {proc.returncode}):\n{output()}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{name} not ready after {READY_TIMEOUT} s:\n{output()}")
                try:
                    with urllib.request.urlopen(ready_url, timeout=1) as resp:
                        if resp.status == 200:
                            break
                except (urllib.error.URLError, OSError):
                    pass
                time.sleep(0.2)
            yield output
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


@contextlib.contextmanager
def serving(handler):
    """Serve `handler` on a loopback port in a thread; yields host:port."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "%s:%d" % server.server_address[:2]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def engine_handler(fixture: Path):
    """Serves `fixture` at /metrics through prometheus_client, as the engine
    does: the client, not this test, answers each Accept header (OpenMetrics
    or text, and the name escaping they ask for)."""
    families = list(parser.text_string_to_metric_families(fixture.read_text()))

    class Fixture:
        def collect(self):
            return families

    registry = CollectorRegistry(auto_describe=False)
    registry.register(Fixture())

    class Quiet(MetricsHandler):
        def log_message(self, format, *args):
            pass

    return Quiet.factory(registry)


def recording_proxy(upstream: str, credentials: set[tuple[str, str]]):
    """Forwards each POST to `upstream`, adding its basic-auth user and
    password to `credentials`."""

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            scheme, _, encoded = self.headers.get("Authorization", "").partition(" ")
            if scheme == "Basic":
                credentials.add(tuple(base64.b64decode(encoded).decode().split(":", 1)))
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = urllib.request.Request(
                f"http://{upstream}{self.path}",
                data=body,
                headers={k: v for k, v in self.headers.items() if k.lower() != "host"},
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as resp:
                    status = resp.status
            except urllib.error.HTTPError as err:
                status = err.code
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    return Proxy


def series(prometheus: str, selector: str) -> list[dict[str, str]]:
    """Label sets of the series matching `selector`."""
    query = urllib.parse.urlencode({"match[]": selector})
    with urllib.request.urlopen(f"http://{prometheus}/api/v1/series?{query}", timeout=5) as resp:
        return json.load(resp)["data"]


def alloy_metric_names(alloy: str) -> set[str]:
    with urllib.request.urlopen(f"http://{alloy}/metrics", timeout=5) as resp:
        text = resp.read().decode()
    return {family.name for family in parser.text_string_to_metric_families(text)}


class PushTest(absltest.TestCase):
    """One Alloy run for every test: each waits on a 15s scrape otherwise."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        probe = cls()
        alloy = tools.require(probe, "ALLOY", "alloy")
        prometheus = tools.require(probe, "PROMETHEUS", "prometheus")
        stack = contextlib.ExitStack()
        cls.addClassCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))

        cls.prometheus = f"127.0.0.1:{free_port()}"
        (tmp / "prometheus.yml").write_text("")
        stack.enter_context(
            started(
                "prometheus",
                [
                    prometheus,
                    f"--config.file={tmp / 'prometheus.yml'}",
                    f"--storage.tsdb.path={tmp / 'tsdb'}",
                    f"--web.listen-address={cls.prometheus}",
                    "--web.enable-remote-write-receiver",
                ],
                f"http://{cls.prometheus}/-/ready",
            )
        )
        cls.credentials = set()
        proxy = stack.enter_context(serving(recording_proxy(cls.prometheus, cls.credentials)))
        engine = stack.enter_context(serving(engine_handler(FIXTURE)))

        targets = tmp / "targets"
        targets.mkdir()
        (targets / "engine.json").write_text(json.dumps([{"targets": [engine]}]))
        # No GPU or host exporter runs here.
        (targets / "accel.json").write_text("[]")
        (targets / "node.json").write_text("[]")

        cls.alloy = f"127.0.0.1:{free_port()}"
        env = {
            **os.environ,
            "NODE_NAME": IDENTITY["node"],
            "CLOUD": IDENTITY["cloud"],
            "REGION": IDENTITY["region"],
            "POOL": IDENTITY["pool"],
            "ROLE": IDENTITY["role"],
            "METRICS_PUSH_URL": f"http://{proxy}/api/v1/write",
            "PUSH_PASSWORD": PASSWORD,
            "TARGETS_DIR": str(targets),
        }
        cls.alloy_output = stack.enter_context(
            started(
                "alloy",
                [
                    alloy,
                    "run",
                    f"--server.http.listen-addr={cls.alloy}",
                    f"--storage.path={tmp / 'alloy'}",
                    str(CONFIG),
                ],
                f"http://{cls.alloy}/-/ready",
                env,
            )
        )
        deadline = time.monotonic() + ARRIVAL_TIMEOUT
        while not (series(cls.prometheus, '{job="engine"}') and series(cls.prometheus, '{job="alloy"}')):
            if time.monotonic() > deadline:
                raise RuntimeError(f"no engine and alloy series arrived:\n{cls.alloy_output()}")
            time.sleep(1)

    def test_engine_series_keep_their_colon(self):
        names = {s["__name__"] for s in series(self.prometheus, '{job="engine"}')}
        self.assertContainsSubset(
            {"sglang:num_running_reqs", "sglang:num_queue_reqs", "sglang:test_pool_usage"}, names
        )
        self.assertEmpty({n for n in names if n.startswith("sglang_")})

    def test_every_series_carries_the_node_identity(self):
        pushed = series(self.prometheus, '{__name__=~".+"}')
        self.assertNotEmpty(pushed)
        for labels in pushed:
            self.assertDictContainsSubset(IDENTITY, labels)

    def test_only_the_expected_jobs_push(self):
        jobs = {s["job"] for s in series(self.prometheus, '{__name__=~".+"}')}
        self.assertEqual(jobs, {"engine", "alloy"})

    def test_a_scraped_pool_label_is_kept_as_exported_pool(self):
        pools = sorted(
            s["exported_pool"] for s in series(self.prometheus, '{__name__="sglang:test_pool_usage"}')
        )
        self.assertEqual(pools, ["kv", "swa"])

    def test_push_authenticates_as_the_node(self):
        self.assertEqual(self.credentials, {(IDENTITY["node"], PASSWORD)})

    def test_alloy_exposes_the_metrics_the_readme_checks(self):
        self.assertContainsSubset(
            {"prometheus_remote_storage_samples", "prometheus_remote_storage_samples_failed"},
            alloy_metric_names(self.alloy),
        )


if __name__ == "__main__":
    absltest.main()
