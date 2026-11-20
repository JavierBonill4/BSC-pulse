# Walkthrough

A guided tour for reviewers: what to open, in what order, and what each piece shows.
About 15 minutes to read, plus a 5-minute live demo at the end.

## 1. The problem (1 minute)

Open the [README](../README.md), section "The idea: fast is not the same as fresh".
On a chain with 0.45 s blocks, an endpoint can answer in 100 ms and still be several
blocks behind. A check that only measures "up and fast" calls that endpoint healthy.
The table there comes from the first real probe of the Chapel endpoints.

## 2. Look before you build (2 minutes)

Open [docs/probe-notes.md](probe-notes.md). Before writing any comparison logic, I
probed every official endpoint by hand. Each finding there changed the code:

- `timestamp` is whole seconds, but BSC blocks also carry `milliTimestamp` (finding 1)
- asking endpoints one after another makes later ones look ahead (finding 4)
- a rate limit can arrive as HTTP 200 with error `-32005` (finding 6)

## 3. One request, one sweep (3 minutes)

| File | Look at |
|---|---|
| `bsc_pulse/rpc.py` | `interpret()`: how an HTTP answer becomes one of six error kinds. Note the `-32005` case |
| `bsc_pulse/rpc.py` | `parse_block()`: prefers `milliTimestamp` |
| `bsc_pulse/probe.py` | `sweep()`: three steps, each on all endpoints at once. The docstring explains the order |

Try it: `make probe` prints one live sweep as a table.

## 4. What the answers mean (4 minutes)

Open `bsc_pulse/compare.py`, the core of the project.

- `Tracker.update()` groups results by network and picks the highest head as the
  reference. Lag is measured against it.
- `_endpoint_view()` computes lag in blocks and in seconds (from block timestamps),
  and how long the head has been unchanged.
- `_network_view()` measures the block interval instead of assuming it.
- `_check_hashes()` finds disagreements (two endpoints, two blocks, one height) and
  reorgs (a height that changed its block between sweeps), using each head's hash
  and its parent's hash.

Nothing in this file reads the clock or the network, so `tests/test_compare.py` can
describe each situation in a few lines. Read `test_stalled_endpoint_versus_stalled_chain`
and `test_a_fast_endpoint_can_still_be_the_one_behind`.

## 5. From measurements to alerts (3 minutes)

| File | Look at |
|---|---|
| `bsc_pulse/metrics.py` | Gauges rebuilt on every scrape, counters created at zero (module docstring) |
| `deploy/prometheus/rules/bsc.yml` | `BscEndpointLagging` (seconds, not blocks) and `BscEndpointStalled` (stuck while the network moves) |
| `deploy/prometheus/tests/bsc_test.yml` | The first test: lag must last 2 minutes; a short spike must not fire |
| `deploy/alertmanager/alertmanager.yml` | The inhibit rules: one problem, one message |

## 6. The incident note (2 minutes)

| File | Look at |
|---|---|
| `bsc_pulse/notifier.py` | Module docstring: three steps, each with a fallback, so an alert always goes out |
| `bsc_pulse/notes.py` | `_lagging_causes()`: the same alert gets different causes depending on latency and head regressions |
| `bsc_pulse/telegram.py` | Why the error message never includes the URL (it contains the bot token) |

## 7. Running it (2 minutes)

| File | Look at |
|---|---|
| `docker-compose.yml` | Every port on 127.0.0.1; the `demo` profile |
| `infra/terraform/variables.tf` | `allowed_ssh_cidrs`: no default, rejects `0.0.0.0/0` |
| `infra/ansible/roles/bsc_pulse/tasks/main.yml` | Secrets from `.env`, then the smoke test at the end |
| `.github/workflows/ci.yml` | Three jobs: Python, containers and rules, infrastructure |

## Live demo (5 minutes)

Start it a few minutes early so the graphs have some history:

```bash
make demo            # or, on the server: make deploy-demo, then make tunnel
```

1. **Grafana, network `mainnet`** (http://127.0.0.1:3000). The "Endpoints right now"
   table, then the "Fast is not the same as fresh" row: lag and latency side by side.
   The measured block interval reads about 0.450 s.
2. **Switch to network `demo`.** Three fake endpoints, all in agreement.
3. **Break one:** `make demo-break MODE=stall`. Within seconds its lag and "time
   since head last moved" climb while the others stay flat.
4. **Prometheus** (http://127.0.0.1:9090/alerts): `BscEndpointStalled` goes from
   pending to firing after about a minute. A minute later `BscEndpointLagging` fires
   too, but Alertmanager (http://127.0.0.1:9093) holds it back as inhibited: same
   problem, one message.
5. **Telegram:** the note arrives about 30 seconds later. Point at the "Now"
   numbers, the causes and the ready-to-run `curl`.
6. **Heal:** `make demo-heal`, and a RESOLVED message follows.

Other modes worth showing if there is time: `MODE=fork` (hash disagreement) and
`MODE=ratelimit` (errors by kind on the Errors row).
