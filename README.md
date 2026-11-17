# inference-node-agent

Metrics collectors for a GPU node that serves a model with SGLang on behalf
of Fractalyze. One [Grafana Alloy](https://grafana.com/docs/alloy/) container
scrapes the node and pushes what it reads to Fractalyze's metrics endpoint by
Prometheus remote-write.

This repo ships configuration, not software: the compose files run upstream
images (Alloy, node-exporter, dcgm-exporter), pinned by digest.

- [How it works](#how-it-works)
- [What leaves the node](#what-leaves-the-node)
- [Before you start](#before-you-start)
- [Deploy](#deploy)
- [Operate](#operate)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Upgrade](#upgrade)
- [Remove](#remove)
- [Repository layout](#repository-layout)
- [Tests](#tests)

## How it works

```
 GPU node (yours)                                         Fractalyze
┌──────────────────────────────────────────────┐
│  SGLang  /metrics ──────────┐                │
│  (your engine)              │ 15 s           │
│                             ▼                │
│  dcgm-exporter ──5 s──▶  ┌───────┐  HTTPS    │        ┌──────────────┐
│  (bundled or yours)      │ Alloy │ ──────────┼──────▶ │ metrics      │
│                          └───────┘ remote-   │        │ endpoint     │
│  node-exporter ──15 s──▶    ▲      write     │        └──────────────┘
│  (bundled)                  │                │
│                     node.env, targets/*.json │
└──────────────────────────────────────────────┘
```

Alloy pulls metrics from three sources on the node, adds the node's identity
labels, and pushes them out. The connection is always opened from the node:
Fractalyze never connects in. Dashboards and alerting run on Fractalyze's
side; nothing on the node depends on the endpoint being reachable.

## What leaves the node

Metrics only, as numeric time series, over one outbound HTTPS connection:

| Job | Source | Content | Interval |
|---|---|---|---|
| `engine` | SGLang's `/metrics` | Queue depth, token throughput, latency histograms, cache usage | 15 s |
| `accel` | dcgm-exporter | Per-GPU utilization, memory, power, temperature, clocks, ECC and XID errors, throttling ([`dcgm/dcgm-counters.csv`](dcgm/dcgm-counters.csv)) | 5 s |
| `node` | node-exporter | Host CPU, memory, filesystems, disks, network | 15 s |
| `alloy` | Alloy itself | The collector's own health: scrape and push counters | 15 s |

Every series is labelled with the node's identity from `node.env` (`node`,
`cloud`, `region`, `pool`, `role`).

Not collected: logs, prompts, completions, or any other request content. The
collectors do not mount the Docker socket and accept no inbound connection:
the only published port is Alloy's UI on the host's loopback.

What the containers can see on the host:

| Container | Access |
|---|---|
| `alloy` | This repo's `alloy/` and `targets/` (read-only), its own data volume, the network |
| `node-exporter` | The host's root filesystem read-only and the host PID namespace, to read `/proc` and `/sys` |
| `accel-exporter` | Every GPU through the NVIDIA runtime, with the `SYS_ADMIN` capability (see [GPU exporter](#3-choose-the-gpu-exporter)) |

## Before you start

From Fractalyze, per node:

| Value | Example | Goes in |
|---|---|---|
| Node name | `acme-h100-01` | `NODE_NAME` |
| Provider name | `acme` | `CLOUD` |
| Region | `us-east-1` | `REGION` |
| Pool | `qwen3-8b` | `POOL` |
| Role | `unified` | `ROLE` |
| Push URL | `https://obs.example.com/api/v1/write` | `METRICS_PUSH_URL` |
| Push password | one per node | `PUSH_PASSWORD` |

On the node:

- Docker Engine 20.10 or later with the Compose v2 plugin (`docker compose
  version`).
- NVIDIA GPUs with the NVIDIA Container Toolkit, for the bundled
  dcgm-exporter. Not needed if the node already runs dcgm-exporter.
- Outbound HTTPS (TCP 443) to the push URL's host.
- SGLang started with `--enable-metrics`.

To send to Fractalyze: the SGLang version the node runs. Fractalyze's
dashboards are built on SGLang's metric names, which change between versions.

## Deploy

Repeat on every node. Each node has its own `NODE_NAME` and push password.

### 1. Get the repo

```sh
git clone https://github.com/fractalyze/inference-node-agent.git
cd inference-node-agent
```

Every command below runs from this directory.

### 2. Fill in `node.env`

```sh
cp node.env.example node.env
chmod 600 node.env   # it holds the push password
```

Set every key to the value Fractalyze provided
([reference](#nodeenv)). Put the password in single quotes:
`PUSH_PASSWORD='ab$cd9'`. Compose expands `$` in unquoted values.

### 3. Choose the GPU exporter

**The node runs no dcgm-exporter (default).** Use the bundled one:

```sh
cp targets/accel.json.example targets/accel.json
```

and keep `-f compose.dcgm.yml` in the `agent` function of step 5. The
bundled exporter collects the fields in
[`dcgm/dcgm-counters.csv`](dcgm/dcgm-counters.csv) every 5 s. It runs with
the `SYS_ADMIN` capability, which the four `DCGM_FI_PROF_*` fields (NVLink
and PCIe throughput) need; removing `cap_add` from `compose.dcgm.yml` leaves
those four series empty and affects nothing else.

**The node already runs dcgm-exporter.** Use that one: write its address to
`targets/accel.json` and drop `-f compose.dcgm.yml` in step 5.

```sh
echo '[{"targets": ["host.docker.internal:9400"]}]' > targets/accel.json
```

That exporter must export the fields in `dcgm/dcgm-counters.csv` with
dcgm-exporter's default `gpu` and `modelName` labels. Extra fields and labels
are harmless. A collection interval longer than 5 s works, with coarser GPU
graphs.

### 4. Point Alloy at the engine

Start SGLang with `--enable-metrics`; it then serves `/metrics` on its API
port. Check from the host (30000 is SGLang's default port):

```sh
curl -s http://127.0.0.1:30000/metrics | grep -c '^sglang:'
```

A count above 0 means metrics are on. Then list every SGLang server of the
node in `targets/engine.json`, in Prometheus
[file_sd](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#file_sd_config)
format:

```sh
cp targets/engine.json.example targets/engine.json
```

```json
[{"targets": ["host.docker.internal:30000", "host.docker.internal:30001"]}]
```

Which address to write depends on how the engine runs:

| Engine runs as | Address | Requirement |
|---|---|---|
| A process on the host | `host.docker.internal:<port>` | SGLang started with `--host 0.0.0.0` (or the Docker bridge gateway address), not `127.0.0.1` |
| A container with a published port | `host.docker.internal:<published port>` | The port is published on `0.0.0.0` or the bridge gateway, not `127.0.0.1` only |
| A container, no published port | `<container name>:<port>` | The container joins the network `inference-node-agent_default`, which exists after the first `up` |

`host.docker.internal` is the host as seen from the Alloy container.

Optional: `--enable-mfu-metrics` adds SGLang's estimated FLOP and memory
traffic counters.

### 5. Start

Define a shorthand for the compose command (bash or zsh; add it to your shell
profile to keep it). Leave out `-f compose.dcgm.yml` if the node uses its own
dcgm-exporter:

```sh
agent() { docker compose --env-file node.env -f compose.yml -f compose.dcgm.yml "$@"; }
```

```sh
agent config -q   # checks node.env: names any missing value
agent pull
agent up -d
```

The containers restart on their own after a crash, and after a reboot if
Docker starts at boot.

### 6. Verify

```sh
agent ps
```

`alloy`, `node-exporter` and (if bundled) `accel-exporter` are `running`.

```sh
curl -s http://127.0.0.1:12345/metrics | grep -E '^prometheus_remote_storage_samples_(total|failed_total)'
```

`samples_total` grows within a minute of start, and `samples_failed_total`
stays at 0.

Then open Alloy's UI, which lists each scrape job with the health of its
targets. It listens on the node's loopback only, so from a workstation:

```sh
ssh -L 12345:127.0.0.1:12345 <node>
# then open http://127.0.0.1:12345
```

`prometheus.scrape.engine`, `prometheus.scrape.accel` and
`prometheus.scrape.node` each show their targets as up. Finally, tell
Fractalyze the node is up: they confirm the series arrive with the right
labels.

## Operate

| Task | Command |
|---|---|
| Status | `agent ps` |
| Alloy's log | `agent logs --tail 100 alloy` |
| Follow all logs | `agent logs -f` |
| Stop (keeps buffered samples) | `agent stop` |
| Start again | `agent up -d` |
| Restart Alloy | `agent restart alloy` |

**After changing `node.env` or a file in `targets/`.** Alloy reads them at
start, so recreate it:

```sh
agent up -d --force-recreate alloy
```

**When the engine's address or port changes, or an engine is added.** Edit
`targets/engine.json` and recreate Alloy as above. Restarting the engine on
the same address needs nothing: Alloy keeps scraping and picks it up when it
returns.

**When the push password is rotated.** Fractalyze sends the new one; set
`PUSH_PASSWORD` in `node.env` and recreate Alloy.

**When the push endpoint is unreachable.** Alloy keeps samples on disk (the
`alloy-data` volume) and sends them once the endpoint returns, for outages of
up to 2 hours. Older samples are dropped. An outage never affects the
engine.

**Disk use.** Container logs are capped at 250 MiB per container.

## Configuration reference

### `node.env`

| Key | Required | Meaning |
|---|---|---|
| `NODE_NAME` | yes | Unique name of the node. Label `node` on every series, and the basic-auth user at the push endpoint |
| `CLOUD` | yes | Name of the provider operating the node. Label `cloud` |
| `REGION` | yes | Region or site. Label `region` |
| `POOL` | yes | The model pool the node serves. Label `pool` |
| `ROLE` | yes | The node's role in its pool: `unified`, `prefill` or `decode`. Label `role` |
| `METRICS_PUSH_URL` | yes | Prometheus remote-write URL |
| `PUSH_PASSWORD` | yes | The node's push password, single-quoted |
| `ALLOY_UI_PORT` | no | Loopback port of Alloy's UI. Default `12345` |

Use the values exactly as Fractalyze provided them: dashboards and alerts
select by these labels.

### `targets/`

Scrape targets in Prometheus file_sd format: a JSON list of
`{"targets": ["host:port", ...]}` groups.

| File | Job | Created by |
|---|---|---|
| `targets/engine.json` | `engine` | You, from `engine.json.example` |
| `targets/accel.json` | `accel` | You, from `accel.json.example` |
| `targets/node.json` | `node` | In the repo (the bundled node-exporter) |

`node.env`, `targets/engine.json` and `targets/accel.json` are git-ignored,
so `git pull` never touches them.

### Ports

| Port | Where | Purpose |
|---|---|---|
| `127.0.0.1:12345` | Host loopback | Alloy's UI and own metrics |
| 9100, 9400, 12345 | Compose network only | node-exporter, dcgm-exporter, Alloy |

Nothing listens on a public interface, and the bundled exporters do not
clash with a node-exporter or dcgm-exporter already running on the host.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `agent config` or `up` prints `set NODE_NAME in node.env` (or another key) | The key is empty or missing | Fill it in `node.env` |
| `up` fails with `bind source path does not exist: .../targets/engine.json` | The target file was not created | Step 3 or 4 |
| `samples_failed_total` grows; Alloy's log shows `401` | Wrong `NODE_NAME` or `PUSH_PASSWORD`, or `$` in an unquoted password | Fix `node.env` (single quotes), recreate Alloy |
| Alloy's log shows a timeout or `no such host` for the push URL | Outbound HTTPS is blocked, or `METRICS_PUSH_URL` is wrong | Allow TCP 443 to the endpoint's host; check the URL |
| Engine target is down: `connection refused` | The engine listens on `127.0.0.1` only, or the port is wrong | See the address table in step 4 |
| Engine target is down: timeout | A host firewall drops traffic from the Docker bridge to the engine's port | Allow the bridge subnet to reach that port |
| Engine target is down with HTTP 404, or the `curl` of step 4 counts 0 | SGLang runs without `--enable-metrics` | Restart the engine with the flag |
| `accel-exporter` does not start: `could not select device driver "nvidia"` | The NVIDIA Container Toolkit is missing or not configured for Docker | Install it, or use the node's own dcgm-exporter |
| Port 12345 is already in use | Another service on the host | Set `ALLOY_UI_PORT` in `node.env` |

When asking Fractalyze for help, attach `agent ps` and
`agent logs --tail 200 alloy`. Neither contains the push password.

## Upgrade

Fractalyze announces a new version as a commit or tag of this repo. Image
versions are part of the compose files, so a commit is the whole version.

```sh
git pull          # or: git fetch && git checkout <tag or commit>
agent pull
agent up -d
```

Buffered samples survive the upgrade.

## Remove

```sh
agent down -v     # -v also deletes the buffered samples
cd .. && rm -rf inference-node-agent
```

Tell Fractalyze, so the node's push password is revoked.

## Repository layout

```
alloy/config.alloy       scrape jobs, identity labels, remote-write
compose.yml              Alloy and node-exporter
compose.dcgm.yml         dcgm-exporter (optional overlay)
dcgm/dcgm-counters.csv   DCGM fields the bundled exporter collects
node.env.example         per-node values, copied to node.env
targets/                 scrape targets, and the examples to copy
tests/                   pytest
```

## Tests

```sh
uv run pytest -q
```

`tests/test_files.py` checks the compose files, target files and counter
list. `tests/test_push.py` runs `alloy/config.alloy` against a stand-in
engine and a local Prometheus. Tests that need `alloy`, `prometheus` or
`docker` are skipped when the binary is missing (set `ALLOY`, `PROMETHEUS`
or `DOCKER` to a path), and fail instead under `REQUIRE_TOOLS=1`, as in CI.
