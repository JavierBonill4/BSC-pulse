# BSC Pulse

A watchdog for public BNB Smart Chain RPC endpoints. It asks several endpoints the
same questions at the same moment and compares the answers, so it can tell when one
endpoint is behind the others even though it still answers quickly.

Built as a take-home project for a Tech Ops internship interview. It is a learning
project, not a production system.

## Why compare endpoints

A health check that only asks "did you answer, and how fast?" misses the failure
that matters most on a fast chain: a node that answers in 30 ms with old data. BSC
produces a block roughly every 0.45 s, so a node that is two seconds behind is
already several blocks stale. The only way to see that from outside is to compare
it with other endpoints on the same network.

## Status

Work in progress. This list is updated as each step lands.

- [x] Endpoint list from the official docs, in a config file
- [x] JSON-RPC client that sorts failures into named kinds
- [x] `make probe`: one live sweep of all endpoints, printed as a table
- [x] Unit tests, lint and CI
- [x] Comparison logic: lag, stalled endpoint, stalled chain, hash disagreement, reorg
- [x] Long-running watchdog exporting Prometheus metrics
- [x] Docker Compose stack with Prometheus
- [ ] Alert rules, Grafana dashboard
- [ ] Telegram alerts with a plain-English incident note
- [ ] Deployment to AWS with Terraform and Ansible

## Quick start

Needs Python 3.10 or newer and `make`. Docker is only needed for `make up`.

```bash
make setup    # creates .venv and installs dependencies
make check    # lint + tests, the same checks CI runs
make probe    # one live sweep of every endpoint, printed as a table
make run      # the watchdog: sweeps every 5 s, metrics on http://127.0.0.1:9105/metrics
make up       # watchdog + Prometheus in Docker (Prometheus UI: http://127.0.0.1:9090)
```

`make probe` prints one table per network:

```
ENDPOINT   BLOCK   BEHIND   HEAD AGE   LATENCY   SYNCING   CLIENT
```

- **BEHIND**: blocks behind the highest endpoint on the same network
- **HEAD AGE**: this machine's clock minus the timestamp of the endpoint's newest block
- **LATENCY**: time taken by the latest-block request

It exits with 0 if every endpoint returned a usable block on the right chain, and 1
otherwise.

<!-- TODO(Javier): paste the output of your own `make probe` run here. -->

## How it works today

```
                    every 5 s
config/endpoints.yaml ──► main.py ──► probe.py ──► rpc.py ──HTTPS──► public BSC endpoints
                             │            (what did each endpoint say?)
                             ▼
                         compare.py      (what does it mean? lag, stalls, reorgs)
                             │
                             ▼
                         metrics.py ──/metrics──► Prometheus
```

| File | Job |
|---|---|
| `rpc.py` | One JSON-RPC call: send, time, sort failures into named kinds |
| `probe.py` | One sweep across all endpoints, all asked at the same moment |
| `compare.py` | Compares endpoints on the same network and remembers state between sweeps |
| `metrics.py` | Turns the comparison into Prometheus metrics |
| `main.py` | The loop: sweep, compare, export, sleep |

One sweep has three steps. Each step runs on all endpoints at the same time and
finishes everywhere before the next one starts.

1. **Identity**: `eth_chainId` every sweep, `web3_clientVersion` about once a
   minute. This also opens the HTTPS connection, so step 2 does not include
   connection setup in its latency.
2. **Head**: `eth_getBlockByNumber("latest")` on every endpoint at the same moment.
3. **Syncing**: `eth_syncing`.

### What the comparison computes

- **Lag**: blocks behind the highest endpoint on the same network, and the same
  gap in seconds (from block timestamps, so it does not depend on round trips).
- **Stalled endpoint**: its head has not changed for a while, but the network's
  highest head has.
- **Stalled chain**: the network's highest head has not changed for a while.
- **Hash disagreement**: two endpoints report different blocks for the same height
  in the same sweep. Each head gives two hashes: its own, and its parent's.
- **Reorg**: a height we recorded earlier now has a different block.
- **Head regression**: an endpoint reports a lower head than last time. This
  usually means a load balancer switched to a node that is further behind.
- **Block interval**: measured from block timestamps, not assumed.

### Metrics

| Metric | Meaning |
|---|---|
| `bsc_endpoint_up` | 1 if the endpoint returned a valid latest block on the right chain |
| `bsc_endpoint_lag_blocks` / `bsc_endpoint_lag_seconds` | Behind the highest endpoint on the network |
| `bsc_endpoint_head_age_seconds` | This machine's clock minus the block's timestamp |
| `bsc_endpoint_head_unchanged_seconds` | Time since this endpoint's head last moved |
| `bsc_endpoint_latency_seconds` | Latency of the last head request |
| `bsc_rpc_latency_seconds` | Histogram of successful head request latency |
| `bsc_endpoint_syncing`, `bsc_endpoint_wrong_chain` | 1 when true |
| `bsc_endpoint_info` | Labels: `url`, `client_version` |
| `bsc_rpc_requests_total` | Requests by `method` and `outcome` (ok, timeout, rate_limited...) |
| `bsc_network_head_block`, `bsc_network_usable_endpoints` | Per network |
| `bsc_network_head_unchanged_seconds` | Time since the network's highest head moved |
| `bsc_network_block_interval_seconds` | Measured time per block |
| `bsc_reorg_blocks_total`, `bsc_hash_disagreements_total` | Block hash events |
| `bsc_endpoint_head_regressions_total` | Times an endpoint's head went backwards |
| `bsc_last_sweep_timestamp_seconds` | Lets an alert notice that the watchdog itself stopped |

Planned shape once the remaining steps are built:

```
public RPCs ──► watchdog ──/metrics──► Prometheus ──► Grafana
                                          │ alert rules
                                          ▼
                                    Alertmanager ──► notifier ──► Telegram
```

## Endpoints

All ten URLs in `config/endpoints.yaml` are copied from the
[official JSON-RPC endpoint page](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/).
I used every endpoint that page lists: seven for mainnet (chain ID 56) and three for
the Chapel testnet (chain ID 97). All ten answered when probed on 2026-11-15.

The page gives a rate limit of 10K requests per 5 minutes. One sweep is four
requests per endpoint.

## Design decisions so far

Written so far in short form. `docs/probe-notes.md` has the evidence behind them.

1. **Compare endpoints with each other, not with a fixed expectation.** I do not
   know what the "right" block height is at any moment. The highest height that any
   endpoint on the same network reports is the best reference available from outside.
2. **Look at real responses before writing logic.** I probed the endpoints by hand
   first. That is how I found the millisecond timestamp field and the rate-limit
   error hidden inside an HTTP 200.
3. **Ask every endpoint at the same moment.** Asking one after another made later
   endpoints look ahead, because the chain moved on in between.
4. **Use `eth_getBlockByNumber("latest")`, not `eth_blockNumber`.** One call
   returns height, hash and timestamp together. The hash is needed later for reorg
   detection, and the timestamp gives the head age.
5. **Use `milliTimestamp` for head age.** The standard `timestamp` is in whole
   seconds, which is too coarse when several blocks share one second.
6. **Sort failures into six kinds**: timeout, connection, rate limited, HTTP error,
   RPC error and bad response. Each one points to a different next step when debugging.
7. **A failed request is a result, not a crash.** One dead endpoint must not stop
   the others from being probed.
8. **Tests never touch the network.** They use fake HTTP sessions, so CI cannot
   fail because a public endpoint had a bad minute.
9. **Keep "what was said" separate from "what it means".** `probe.py` only
   collects answers. `compare.py` interprets them, and is given the time as an
   argument instead of reading the clock, so every comparison can be tested with
   made-up sweeps.
10. **The exporter decides nothing.** It reports measurements. Deciding what is bad
    (and for how long) belongs in the alert rules, where thresholds can change
    without touching the code.
11. **Rebuild the gauges on every scrape.** When an endpoint goes down, its lag
    disappears instead of staying frozen at its last value, which would look healthy.
12. **Create every counter at zero at startup.** A counter that first appears at 1
    looks like "no change" to Prometheus, so the first error would be missed.
13. **One histogram for latency, not one gauge.** A single last value hides spikes.
    The histogram lets the dashboard and alerts use percentiles.
14. **Compose with a private network and ports on 127.0.0.1.** The same file runs on
    a Mac and on the server, and nothing listens on a public interface.

### Alternatives I rejected

- **Extending my Bash health check.** It works for one endpoint. Comparing ten
  endpoints in parallel and unit-testing the logic is awkward in Bash.
- **A Web3 library (web3.py).** The tool needs four JSON-RPC methods. Calling them
  directly keeps the dependency list short and keeps the raw responses visible,
  which is the part I want to understand.
- **asyncio.** Threads do the same job here (ten endpoints, one thread each) and
  are simpler to read and explain.
- **Host networking in Compose**, as in eth-node-iac. It does not work the same
  way on Docker Desktop for Mac, and service names (`bsc-pulse:9105`) are clearer
  than ports on localhost.
- **Writing textfile metrics for node-exporter**, as my Bash script did. That suits
  a cron job. A long-running service can serve `/metrics` itself.
- **An extra request per sweep to check a fixed recent block on every endpoint.**
  It would catch more disagreements, but adds load. Comparing the heads and parent
  hashes we already fetch costs nothing extra.

## What is reused and what is new

This project builds on my earlier learning project, `eth-node-iac` (Terraform +
Ansible + Prometheus/Grafana watching one Ethereum Sepolia endpoint).

| | From eth-node-iac | New here |
|---|---|---|
| Signals | Up, latency, block number, syncing | Lag against other endpoints, head age, error kinds |
| Health check | One endpoint, Bash + cron | Many endpoints in parallel, Python, unit tested |
| Metrics | Textfile written for node-exporter | Service serves `/metrics` itself |
| Compose | Host networking, `:latest` images | Private network, ports on 127.0.0.1, pinned image versions |
| Conventions | Makefile targets, exit codes, `.gitignore` rules | Lint, tests and CI |
| Infrastructure | Reused at the deployment step (not done yet) | |

## Known limitations

- A public endpoint is probably a load balancer in front of several nodes. Two
  requests to the same URL may be answered by different nodes.
- All three Chapel endpoints are on `bnbchain.org`, so they may share failures.
- Everything is measured from one machine. A slow network path from here looks the
  same as a slow endpoint.
- Head age depends on this machine's clock being correct.
- The request timeout applies to connecting and to each wait for data. It is not a
  hard limit on the whole request.
- `web3_clientVersion` is shown for information only. At least one endpoint reports
  a version string that does not match its behaviour.
- Dependencies are given as version ranges, not locked.
- Reorg and disagreement detection only sees the heights that endpoints happen to
  report as their head (and the parent of each). It samples the chain; it does not
  check every block.
- State lives in memory. After a restart, stall timers and hash history start
  again from empty.

## What I'd do next

To be written when the build is finished.

## How AI was used

I built this with Claude (Anthropic) as a pair programmer. Claude checked my
starting facts against the BNB Chain docs and the `bnb-chain/bsc` release notes,
probed the endpoints, proposed the structure, and wrote the first version of the
code and tests. I reviewed each step, ran it myself, and made the commits.

<!-- TODO(Javier): edit this section into your own words before submitting. -->
