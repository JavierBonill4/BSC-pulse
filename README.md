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
- [x] Alert rules with unit tests, Alertmanager
- [x] Telegram alerts with a plain-English incident note
- [x] Grafana dashboard, generated from a script
- [x] Fake endpoints you can break on demand, for demos and end-to-end tests
- [x] Deployment to AWS with Terraform and Ansible (adapted from my earlier project)

## Quick start

Needs Python 3.10 or newer and `make`. Docker is only needed for `make up`.

```bash
make setup    # creates .venv and installs dependencies
make check    # lint + tests, the same checks CI runs
make probe    # one live sweep of every endpoint, printed as a table
make run      # the watchdog: sweeps every 5 s, metrics on http://127.0.0.1:9105/metrics
make up       # the whole stack in Docker (needs .env, see below)
make demo     # the same, plus fake endpoints you can break
make rules-test   # unit-test the alert rules with promtool (uses Docker)
```

| URL (on the server: through the SSH tunnel) | What |
|---|---|
| http://127.0.0.1:3000 | Grafana, user `admin`, password from `.env` |
| http://127.0.0.1:9090 | Prometheus (queries, alert states) |
| http://127.0.0.1:9093 | Alertmanager (silences, inhibited alerts) |
| http://127.0.0.1:9105/metrics | The watchdog's raw metrics |

`make up` needs a `.env` file: `cp .env.example .env` and set at least
`GRAFANA_ADMIN_PASSWORD`. Compose refuses to start Grafana without it.

### Deploy to AWS

Terraform creates one Ubuntu 24.04 server. Ansible hardens it, installs Docker, and
runs the same `docker-compose.yml` as on a laptop. Only SSH is open, and only from
your IP; everything else is reached through an SSH tunnel.

```
your Mac ── make tf-apply ──► AWS: EC2 t3.small, security group (SSH from your IP only)
         ── make deploy ────► Ansible: hardening → docker → bsc_pulse (compose up, smoke test)
         ── make tunnel ────► http://127.0.0.1:3000 on your Mac = Grafana on the server
```

Needs: an AWS account with credentials set up (`aws sts get-caller-identity`
works), Terraform, Ansible, an SSH key at `~/.ssh/id_ed25519.pub`, and a filled-in `.env`.

```bash
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
# edit it: allowed_ssh_cidrs = ["<your IP>/32"]   (curl https://checkip.amazonaws.com)

make ansible-deps        # once: Ansible collections
make tf-init             # once: Terraform providers
make tf-plan             # read what will be created
make tf-apply            # create the server; also writes the Ansible inventory
make ping                # wait ~1 minute after apply, until this succeeds
make deploy              # harden, install Docker, start the stack, check it works
make tunnel              # leave running; open http://127.0.0.1:3000
```

`make deploy` finishes only if the watchdog reports a network head and Grafana is
healthy, so a green run means the stack really works. It prints how many
endpoints are usable on each network.

For a live demo on the server: `make deploy-demo`, then with `make tunnel` running,
`make demo-break MODE=stall` works from your Mac exactly as it does locally.

> **When you are done: `make tf-destroy`.** The server costs money for every hour
> it runs (a t3.small is a few US cents per hour; check your own account's pricing
> and credits).

### Demo: break an endpoint on purpose

`make demo` adds a network called `demo` with three fake endpoints
(`bsc_pulse/fake_rpc.py`) that serve a simulated chain at 0.45 s per block. They
agree with each other until you break one:

```bash
make demo                      # start everything
make demo-break MODE=stall     # fake-rpc:18547 freezes on its current block
make demo-status               # what each fake endpoint is doing
make demo-heal                 # back to healthy
```

In Grafana, pick network `demo`. With `MODE=stall`:

| After | What you see |
|---|---|
| a few seconds | Its lag and "time since head last moved" start climbing; the others stay flat |
| ~1 minute | `BscEndpointStalled` fires in Prometheus |
| ~1.5 minutes | The incident note arrives in Telegram (Alertmanager waits 30 s to group) |
| after `make demo-heal` | A short RESOLVED message |

Other modes: `lag` (stays 20 blocks behind), `slow` (1.5 s answers), `timeout`,
`error` (HTTP 503), `ratelimit` (every other request gets 429), `wrong_chain`,
`syncing`, `fork` (a different block at the same height, which triggers
`BscHashDisagreement`).

### Telegram setup (5 minutes)

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the number in
   `"chat":{"id": ...}`.
4. `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
5. Test it: `set -a; source .env; set +a; make telegram-test`. A sample note arrives.

Without these settings the notifier runs as a dry run and prints each message to
its log (`docker compose logs notifier`).

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

### Alerting path

```
watchdog ──/metrics──► Prometheus ──alert rules──► Alertmanager ──webhook──► notifier ──► Telegram
                           ▲                    (group, wait, repeat,         │
                           └──── recent numbers for the note ─────────────────┘
                                                 silence duplicates)
```

| Piece | Job |
|---|---|
| `deploy/prometheus/rules/bsc.yml` | Decides what is a problem and for how long it must last |
| `deploy/prometheus/tests/bsc_test.yml` | Unit tests that replay made-up metric history through the rules |
| `deploy/alertmanager/alertmanager.yml` | Groups alerts, limits repeats, and silences alerts caused by another one |
| `notifier.py` | Receives each alert and makes sure a message goes out, whatever fails |
| `prom.py` | Asks Prometheus for the numbers behind the alert |
| `notes.py` | Writes the incident note: what we see, likely causes, next checks |
| `telegram.py` | Sends the note |

### Alerts

| Alert | Fires when | Severity |
|---|---|---|
| `BscEndpointLagging` | Head more than 5 s older than the newest head, for 2 min | warning |
| `BscEndpointStalled` | Head stuck for 30 s while the network moves | critical |
| `BscChainStalled` | Network's highest head stuck for 30 s | critical |
| `BscEndpointDown` | No valid latest block for 2 min | warning |
| `BscEndpointWrongChain` | `eth_chainId` does not match the network | critical |
| `BscEndpointSyncing` | `eth_syncing` reports progress for 5 min | warning |
| `BscEndpointErrorRate` | Over 10% of requests fail, for 5 min | warning |
| `BscEndpointRateLimited` | More than 5 rate-limit answers in 5 min | warning |
| `BscEndpointSlow` | p95 latency over 1 s for 10 min | warning |
| `BscEndpointHeadFlapping` | Head went backwards more than 3 times in 10 min | warning |
| `BscHashDisagreement` | Endpoints disagreed on a block hash more than 3 times in 10 min | warning |
| `BscReorgObserved` | A block we had seen was replaced | warning |
| `BscTooFewEndpoints` | Fewer than 2 usable endpoints, so no comparison | critical |
| `BscPulseDown`, `BscPulseSweepsStale` | The watchdog itself stopped | critical |

Alertmanager silences duplicates: an endpoint that is down does not also send
error-rate, latency and lag alerts, and a stalled endpoint does not also send a
lag alert.

### Incident notes

Each firing alert becomes a message like this one, produced in the end-to-end test
against a fake endpoint (connection refused):

```
FIRING · warning · BscEndpointDown
127.0.0.1:18104 · mainnet

127.0.0.1:18104 (mainnet) has not returned a valid latest block for 2 minutes

Now
• status: down
• failed requests (5m): 100%
• errors (5m): connection 128
• network head: 211,421,410, 3/4 endpoints usable, block interval 450 ms

Likely causes
1. Connections fail before any HTTP answer (DNS, TCP or TLS): the host may be
   down, or its DNS or certificate changed.

Next checks
1. See where it fails (DNS, TLS, HTTP status): curl -sv http://127.0.0.1:18104
2. Check that the name still resolves: dig +short 127.0.0.1
3. Try from another machine to rule out this server's network.
```

The causes depend on the numbers. A lagging endpoint that answers in 40 ms gets
"it serves old data: the node imports blocks too slowly". One that is also slow
gets "overloaded node or congested path". One whose head keeps going backwards
gets "load balancer mixing nodes".

<!-- TODO(Javier): replace with a screenshot of a real Telegram message. -->

### Dashboard

Sections, top to bottom:

1. **Network right now**: highest block, usable endpoints, measured block interval,
   freshest head age, firing alerts, replaced blocks in 24 h.
2. **Endpoints right now**: one row per endpoint with up/down, lag, head age, p95
   latency, failure rate and syncing, coloured at the alert thresholds. Plus firing
   alerts and client versions.
3. **Fast is not the same as fresh**: lag and latency side by side, the core idea of
   the project. Red dashed lines are the alert thresholds.
4. **Errors**: failed requests by kind, and per endpoint.
5. **Chain**: measured block interval, time since the network head moved, and block
   hash events.

Every panel has a tooltip explaining what to look for. The dashboard is generated by
`deploy/grafana/build_dashboard.py` (`make dashboard`). A test checks that the JSON
is up to date and that every query uses a metric the watchdog really exports.

<!-- TODO(Javier): add a Grafana screenshot (mainnet, and demo with a stalled endpoint). -->

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
15. **Alert thresholds in seconds, not blocks.** The block interval changed from
    0.75 s to 0.45 s with the Fermi hard fork. "5 seconds behind" keeps its meaning
    when it changes again; "10 blocks behind" does not.
16. **Every alert waits before firing (`for:`).** I measured endpoints 2 to 3 blocks
    apart on a normal day. A short spike should not wake anyone up.
17. **Test the alert rules, not only the Python.** promtool replays made-up metric
    history and checks exactly when each alert fires. I broke two rules on purpose
    to check that the tests catch it.
18. **Incident notes are rule-based, not written by an LLM.** Every sentence can be
    traced to a line in `notes.py`, the same alert always gives the same note, and
    it cannot invent a cause. The rules use the live numbers to pick causes, so the
    note still adapts to the situation.
19. **An alert always goes out.** If Prometheus does not answer, the note says so
    and keeps the general causes. If building the note fails, the plain alert is
    sent. If Telegram fails, the notifier answers 502 so Alertmanager retries.
20. **The bot token never reaches a log.** `requests` puts the URL, which contains
    the token, in its error messages, so `telegram.py` reports only the error type.
21. **Fake endpoints for demos, as a separate network.** Waiting for a public endpoint
    to fail is not a demo. The `demo` network is compared only with itself, so
    breaking a fake endpoint never affects the mainnet or Chapel numbers.
22. **The dashboard is code.** Each panel is one function call with its query and an
    explanation, which is reviewable in a pull request. Editing in the Grafana UI is
    switched off, so the dashboard in git is the dashboard that runs.
23. **Grafana has no default password.** Compose refuses to start until `.env` sets
    one. (My earlier project had `change-me` in a variables file.)
24. **The laptop and the server run the same Compose file.** Ansible copies the
    repository and runs `docker compose up`, so what I test locally is what runs.
    Secrets go to a `.env` file on the server that only root can read.
25. **SSH from the whole internet is refused by Terraform.** `allowed_ssh_cidrs`
    has no default and rejects `0.0.0.0/0`, so forgetting to set it fails loudly.
26. **A deploy checks that it worked.** The playbook waits for real metrics and a
    healthy Grafana instead of stopping once the containers have started.

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
- **Alerting from the Python code directly.** Prometheus and Alertmanager already
  handle "for how long", grouping, repeats and silences, and the rules can be
  tested and changed without touching the code.
- **Alertmanager's built-in Telegram receiver.** It can send the alert text, but it
  cannot look up live numbers or choose causes. A small webhook service can.
- **An LLM writing the notes.** See decision 18. It is listed under what I would
  try next, behind the same interface and with the rule-based note as fallback.
- **Building the image in CI and pushing it to a registry.** The right move for a
  team. For one server, building on the server avoids running a registry and
  managing its credentials.
- **Kubernetes or ECS.** Five small containers on one host do not need an
  orchestrator. Compose keeps the deployment readable.
- **Opening Grafana to the internet behind a password.** An SSH tunnel keeps the
  attack surface at one port.
- **Hand-editing the dashboard JSON, or building it in the UI and exporting it.**
  Both work, but produce large diffs nobody reviews.
- **Grafana's own alerting.** Prometheus rules can be unit-tested with promtool and
  live next to the metrics they use.
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
| Grafana | Default password in a variables file | Required from `.env`; dashboard generated from a script |
| Conventions | Makefile targets, exit codes, `.gitignore` rules | Lint, tests and CI |
| Terraform | EC2 + security group + key pair, Ubuntu AMI via SSM, IMDSv2, encrypted disk, generated inventory | SSH CIDR required and validated; P2P ports removed |
| Ansible | `hardening` and `docker` roles, nearly unchanged | `bsc_pulse` role: copy the app, write secrets from `.env`, compose up, smoke test |
| Access | Everything on 127.0.0.1, SSH tunnel, SSH restricted to my IP | Same; a test checks no compose port is public |

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
- Alert thresholds come from a short look at normal behaviour, not from a long
  baseline. The hash-disagreement threshold in particular is a guess.
- The incident notes list causes I wrote myself. They are a starting point for
  investigation, not a diagnosis.
- One Telegram chat receives everything; there is no routing by severity yet.
- `BscHashDisagreement` and `BscReorgObserved` use a 10 minute window, so they stay
  firing for up to 10 minutes after the cause is gone.
- The dashboard's queries are tested against a real Prometheus, but the dashboard
  itself was not rendered in CI.
- One server, no redundancy. If it goes down, so does the monitoring, and nobody
  is told (a second, external check would be needed).
- Terraform state is a local file on my Mac, not in a shared backend.
- The deployed images are built on the server, so two deploys can produce slightly
  different images if a dependency releases a new version in between.

## What I'd do next

To be written when the build is finished.

## How AI was used

I built this with Claude (Anthropic) as a pair programmer. Claude checked my
starting facts against the BNB Chain docs and the `bnb-chain/bsc` release notes,
probed the endpoints, proposed the structure, and wrote the first version of the
code and tests. I reviewed each step, ran it myself, and made the commits.

<!-- TODO(Javier): edit this section into your own words before submitting. -->
