# inference-node-agent

Metrics collectors for a GPU node that serves a model with SGLang on behalf
of Fractalyze. One [Grafana Alloy](https://grafana.com/docs/alloy/) container
scrapes the node and pushes what it reads to Fractalyze's metrics endpoint by
Prometheus remote-write.

This repo ships configuration, not software: the compose files run upstream
images (Alloy, node-exporter, dcgm-exporter), pinned by digest.

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

## Requirements

- Docker with Compose v2.
- NVIDIA GPUs with the NVIDIA Container Toolkit, for the bundled
  dcgm-exporter (not needed if the node already runs one, see
  [GPU metrics](#gpu-metrics)).
- Outbound HTTPS to the push endpoint Fractalyze provides.
- SGLang started with `--enable-metrics`, on an address the Alloy container
  can reach (see [Engine](#engine)).

## Setup

Fractalyze provides the values for `node.env`: the node's identity labels,
the push URL, and one push password per node.

```sh
git clone https://github.com/fractalyze/inference-node-agent.git
cd inference-node-agent

cp node.env.example node.env                         # fill in
cp targets/engine.json.example targets/engine.json   # the engine's address
cp targets/accel.json.example targets/accel.json     # the GPU exporter's address

docker compose --env-file node.env -f compose.yml -f compose.dcgm.yml up -d
```

The three copied files are git-ignored, so `git pull` never touches them.
Compose names any value `node.env` leaves empty and refuses to start.

### Engine

`targets/engine.json` lists the `host:port` of every SGLang server on the
node, in Prometheus
[file_sd](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#file_sd_config)
format. SGLang serves `/metrics` on its API port when started with
`--enable-metrics`.

```json
[{"targets": ["host.docker.internal:30000", "host.docker.internal:30001"]}]
```

`host.docker.internal` is the host as seen from the Alloy container, so it
reaches an engine that listens on the host (a process, or a container with a
published port). The engine must listen on an address the Docker bridge can
reach: `0.0.0.0` or the bridge gateway, not `127.0.0.1` only. An engine
container can instead join the `inference-node-agent_default` network and be
listed by its container name.

Optional: `--enable-mfu-metrics` adds SGLang's estimated FLOP and memory
traffic counters.

### GPU metrics

`compose.dcgm.yml` runs dcgm-exporter with the field list in
[`dcgm/dcgm-counters.csv`](dcgm/dcgm-counters.csv), collecting every 5 s. It
adds the `SYS_ADMIN` capability, which the four `DCGM_FI_PROF_*` fields
(NVLink and PCIe throughput) need; removing it leaves those four series
empty and affects nothing else.

A node that already runs dcgm-exporter can use that one instead: leave
`compose.dcgm.yml` out of every command and put the existing exporter's
address in `targets/accel.json`, for example
`[{"targets": ["host.docker.internal:9400"]}]`. That exporter must export
the fields in `dcgm/dcgm-counters.csv` with dcgm-exporter's default `gpu`
and `modelName` labels. Extra fields and labels are harmless.

## Verify

```sh
docker compose --env-file node.env -f compose.yml -f compose.dcgm.yml ps
curl -s http://127.0.0.1:12345/metrics | grep -E '^prometheus_remote_storage_samples_(total|failed_total)'
```

`samples_total` grows within a minute of start, and `samples_failed_total`
stays at 0. A growing failure count means the endpoint rejected the push
(`docker compose ... logs alloy` shows the HTTP status: 401 is a wrong
`NODE_NAME` or `PUSH_PASSWORD`).

Alloy's UI at <http://127.0.0.1:12345> shows each scrape job and its targets;
a target that is down points at a wrong address in `targets/`. Set
`ALLOY_UI_PORT` in `node.env` if port 12345 is taken.

Alloy reads `targets/*.json` and `node.env` at start. After editing one:

```sh
docker compose --env-file node.env -f compose.yml -f compose.dcgm.yml up -d --force-recreate alloy
```

## Outages

Alloy keeps samples on disk (the `alloy-data` volume) while the push
endpoint is unreachable and sends them once it returns, for outages of up to
2 hours. Older samples are dropped. Nothing on the node depends on the
endpoint: an outage never affects the engine.

## Upgrade

```sh
git pull
docker compose --env-file node.env -f compose.yml -f compose.dcgm.yml up -d
```

Image versions are part of the compose files, so a commit of this repo is the
whole version.

## Remove

```sh
docker compose --env-file node.env -f compose.yml -f compose.dcgm.yml down -v
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
