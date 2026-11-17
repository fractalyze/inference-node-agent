"""The files a node operator runs: compose files, target files, counter list.

Static checks, plus `docker compose config` and `alloy fmt` where the tools
exist. tests/test_push.py runs the Alloy config end to end.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import yaml
from absl.testing import absltest, parameterized

import tools

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "alloy/config.alloy"
COMPOSE = ROOT / "compose.yml"
COMPOSE_DCGM = ROOT / "compose.dcgm.yml"
COMPOSE_FILES = [COMPOSE, COMPOSE_DCGM]
NODE_ENV_EXAMPLE = ROOT / "node.env.example"
DCGM_CSV = ROOT / "dcgm/dcgm-counters.csv"

# What the receiving side selects by, and the scrape interval it assumes.
JOBS = {"engine": "15s", "accel": "5s", "node": "15s", "alloy": "15s"}
# Files config.alloy reads under TARGETS_DIR, and where each comes from.
# engine.json and accel.json are the operator's, copied from the examples.
TARGET_SOURCES = {
    "engine.json": "targets/engine.json",
    "accel.json": "targets/accel.json",
    "node.json": "targets/node.json",
}
OPERATOR_FILES = ["node.env", "targets/engine.json", "targets/accel.json"]
# repo:tag@sha256:<digest>
PINNED_IMAGE = re.compile(r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$")
# Values that make every ${VAR:?...} in the compose files resolve.
FILLED_ENV = {
    "NODE_NAME": "n",
    "CLOUD": "c",
    "REGION": "r",
    "POOL": "p",
    "ROLE": "unified",
    "METRICS_PUSH_URL": "https://obs.example.com/api/v1/write",
    "PUSH_PASSWORD": "x",
}


def services(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["services"]


def env_keys(path: Path) -> set[str]:
    return set(re.findall(r"^([A-Z_]+)=", path.read_text(), re.MULTILINE))


class ComposeTest(parameterized.TestCase):
    def test_every_image_is_pinned_by_digest(self):
        for path in COMPOSE_FILES:
            for name, service in services(path).items():
                self.assertRegex(service["image"], PINNED_IMAGE, f"{path.name}: {name}")

    def test_alloy_gets_every_variable_the_config_reads(self):
        read = set(re.findall(r'sys\.env\("([A-Z_]+)"\)', CONFIG.read_text()))
        self.assertEqual(read, set(services(COMPOSE)["alloy"]["environment"]))

    def test_node_env_example_lists_every_required_variable(self):
        required = set(re.findall(r"\$\{([A-Z_]+):\?", COMPOSE.read_text()))
        self.assertEqual(required, set(FILLED_ENV))
        self.assertEqual(required, env_keys(NODE_ENV_EXAMPLE))

    def test_alloy_mounts_every_target_file(self):
        mounts = {
            v["target"]: v["source"]
            for v in services(COMPOSE)["alloy"]["volumes"]
            if v["type"] == "bind"
        }
        for name, source in TARGET_SOURCES.items():
            self.assertEqual(mounts.get(f"/etc/alloy/targets/{name}"), f"./{source}")

    def test_bind_sources_are_in_the_repo_or_made_from_an_example(self):
        for path in COMPOSE_FILES:
            for name, service in services(path).items():
                for volume in service.get("volumes", []):
                    source = volume["source"]
                    if volume["type"] != "bind" or not source.startswith("./"):
                        continue
                    present = ROOT / source
                    example = ROOT / f"{source}.example"
                    self.assertTrue(
                        present.exists() or example.exists(), f"{path.name}: {name}: {source}"
                    )

    def test_no_port_is_published_beyond_loopback(self):
        for path in COMPOSE_FILES:
            for name, service in services(path).items():
                self.assertNotEqual(service.get("network_mode"), "host", name)
                for port in service.get("ports", []):
                    self.assertStartsWith(port, "127.0.0.1:", f"{path.name}: {name}")

    def test_docker_socket_is_not_mounted(self):
        for path in COMPOSE_FILES:
            self.assertNotIn("docker.sock", path.read_text(), path.name)

    def test_operator_files_are_git_ignored(self):
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        for name in OPERATOR_FILES:
            self.assertIn(name, ignored)

    def test_compose_config_accepts_a_filled_environment(self):
        docker = tools.require(self, "DOCKER", "docker")
        cmd = [docker, "compose", "--project-directory", str(ROOT)]
        for path in COMPOSE_FILES:
            cmd += ["-f", str(path)]
        result = subprocess.run(
            cmd + ["config", "-q"],
            env={**os.environ, **FILLED_ENV},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ConfigTest(parameterized.TestCase):
    @parameterized.parameters(*JOBS.items())
    def test_job_name_and_interval(self, job, interval):
        block = re.search(
            rf'prometheus\.scrape "{job}" \{{(.*?)\n\}}', CONFIG.read_text(), re.DOTALL
        )
        self.assertIsNotNone(block, job)
        self.assertRegex(block.group(1), rf'job_name\s+= "{job}"')
        self.assertRegex(block.group(1), rf'scrape_interval\s+= "{interval}"')

    def test_no_other_job(self):
        self.assertCountEqual(re.findall(r'prometheus\.scrape "(\w+)"', CONFIG.read_text()), JOBS)

    def test_alloy_fmt_accepts_the_config(self):
        alloy = tools.require(self, "ALLOY", "alloy")
        result = subprocess.run([alloy, "fmt", str(CONFIG)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), CONFIG.read_text().strip())


class TargetFilesTest(parameterized.TestCase):
    @parameterized.parameters(
        "targets/engine.json.example", "targets/accel.json.example", "targets/node.json"
    )
    def test_file_sd_format(self, name):
        groups = json.loads((ROOT / name).read_text())
        self.assertNotEmpty(groups)
        for group in groups:
            self.assertNotEmpty(group["targets"])
            for target in group["targets"]:
                self.assertRegex(target, r"^[\w.-]+:\d+$")

    def test_default_targets_are_the_compose_services(self):
        accel = json.loads((ROOT / "targets/accel.json.example").read_text())
        node = json.loads((ROOT / "targets/node.json").read_text())
        self.assertEqual(accel[0]["targets"], ["accel-exporter:9400"])
        self.assertIn("accel-exporter", services(COMPOSE_DCGM))
        self.assertEqual(node[0]["targets"], ["node-exporter:9100"])
        self.assertIn("node-exporter", services(COMPOSE))


class VersionsTest(absltest.TestCase):
    def test_ci_tests_the_alloy_version_compose_runs(self):
        image = services(COMPOSE)["alloy"]["image"]
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        version = re.search(r"ALLOY_VERSION: (\S+)", ci).group(1)
        self.assertStartsWith(image, f"grafana/alloy:v{version}@")


class DcgmCountersTest(absltest.TestCase):
    def test_every_line_is_a_field_a_type_and_a_help(self):
        rows = [
            [cell.strip() for cell in line.split(",", 2)]
            for line in DCGM_CSV.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertNotEmpty(rows)
        for row in rows:
            self.assertLen(row, 3, row)
            self.assertRegex(row[0], r"^DCGM_FI_[A-Z0-9_]+$")
            self.assertIn(row[1], ("gauge", "counter"))
            self.assertNotEmpty(row[2])
        fields = [row[0] for row in rows]
        self.assertCountEqual(fields, set(fields))


if __name__ == "__main__":
    absltest.main()
