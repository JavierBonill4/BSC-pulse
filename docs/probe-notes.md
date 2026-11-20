# What the real endpoints return

Notes from probing the public endpoints by hand on 2026-11-15, before writing any
comparison logic. Each finding below changed something in the code.

These are observations from a handful of requests sent from my laptop (browser
`fetch` calls, made during the AI-assisted build session). They show what the
responses look like. They are not a measurement of endpoint quality.

## How to reproduce

```bash
RPC=https://bsc-dataseed.bnbchain.org
call() { curl -s -H 'Content-Type: application/json' "$RPC" \
  --data "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":${2:-[]}}"; echo; }

call eth_chainId
call eth_syncing
call web3_clientVersion
call eth_getBlockByNumber '["latest",false]'
call eth_doesNotExist
call eth_getLogs '[{"fromBlock":"latest"}]'
```

Or run `make probe` for all endpoints at once.

## Findings

### 1. Block time is in whole seconds, but there is a millisecond field

`timestamp` is whole seconds (`0x6af9fd9f` = 1794768287). Several blocks share the
same second, so it cannot tell me how old the newest block is.

Every endpoint also returned `milliTimestamp` (`0x1a1e086b6da` = 1794768287450). The
last bytes of `mixHash` held the same millisecond part (`0x...0352` = 850 ms on a
block whose `milliTimestamp` ended in 850).

**In the code:** `parse_block` uses `milliTimestamp` and falls back to `timestamp`.

### 2. Measured block interval: 450 ms on both networks

| Network | First block seen | Second block seen | Blocks | Time between | Per block |
|---|---|---|---|---|---|
| Mainnet | 133785872 | 133785879 | 7 | 3150 ms | 450 ms |
| Chapel | 142923283 | 142923290 | 7 | 3150 ms | 450 ms |

This matches the 0.45 s interval in the bsc v1.6.4 release notes (Fermi fork).

**In the code:** nothing is hard-coded. The watchdog will measure the interval itself.

### 3. An endpoint can answer quickly and still be behind

Chapel, both sweeps, all three endpoints asked at the same moment:

| Endpoint | Sweep 1 block | Sweep 2 block | Latency | Head age when the answer arrived |
|---|---|---|---|---|
| bsc-prebsc-dataseed.bnbchain.org | 142923286 | 142923292 | ~250 ms | 0.3 s, 0.9 s |
| bsc-testnet-dataseed.bnbchain.org | 142923283 (3 behind) | 142923290 (2 behind) | ~100 ms | 1.5 s, 1.6 s |
| bsc-testnet.bnbchain.org | 142923283 (3 behind) | 142923290 (2 behind) | ~100 ms | 1.5 s, 1.6 s |

The two faster endpoints were the ones further behind. On mainnet, all seven
endpoints returned the same block in both sweeps.

**In the code:** latency and lag are separate measurements. A lag of one block can
come from timing alone (a 450 ms block is shorter than some round trips), so alert
thresholds will need a tolerance of a few blocks.

### 4. Asking endpoints one after another gives wrong lag numbers

The first probe asked the mainnet endpoints one after another. Heights came back
as 133785820, ...820, ...820, ...820, ...821, ...821, ...822: the later an endpoint
was asked, the "further ahead" it looked. The chain had moved on during the probe.

**In the code:** `sweep` asks every endpoint for its latest block at the same
moment, one thread per endpoint.

### 5. The first request on a new connection is slow

On mainnet, the first request to each endpoint took about 100 to 130 ms (about
200 ms for one of them). Later requests mostly took about 30 ms. The likely cause is
connection setup (DNS, TCP and TLS), which only the first request pays for.

**In the code:** each endpoint has its own HTTP session that stays open, and the
identity calls run first, so the latency of the latest-block call does not include
connection setup.

### 6. A rate limit is not always HTTP 429

`eth_getLogs` on bsc-dataseed.bnbchain.org returned **HTTP 200** with:

```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32005,"message":"limit exceeded"}}
```

The docs say `eth_getLogs` is disabled on the public mainnet endpoints, and this is
how that shows up. An unknown method returned HTTP 200 with code `-32601`.

**In the code:** `interpret` checks the JSON-RPC error code as well as the HTTP
status. Code `-32005` counts as `rate_limited`. Other error codes count as `rpc_error`.

### 7. `web3_clientVersion` cannot be fully trusted

| Endpoint | Reported version |
|---|---|
| bsc-dataseed.bnbchain.org, bsc-dataseed.ninicoin.io | `Geth/v1.8.2/linux-amd64/go1.26.10` |
| bsc-dataseed.defibit.io, bsc.nodereal.io, bsc-dataseed.nariox.org | `Geth/v1.8.2-4e1c8247-20261104/linux-amd64/go1.26.10` |
| bsc-dataseed-public.bnbchain.org | `Geth/v1.1.16-cb131fab/linux-amd64/go1.17.5` |
| rpc-bnb.blockmachine.io | `blockmachine-gateway/v0.1.0` |
| bsc-testnet-dataseed.bnbchain.org, bsc-testnet.bnbchain.org | `Geth/v1.8.1-56d84e62-20261019/linux-amd64/go1.26.9` |
| bsc-prebsc-dataseed.bnbchain.org | `Geth/v1.8.1/linux-amd64/go1.26.10` |

The `v1.1.16` endpoint returned blocks containing `milliTimestamp`, a field that
did not exist in a release that old. So that string probably comes from a proxy in
front of the nodes, not from the node that answered. I have not confirmed this.

**In the code:** the version is shown for information only. Nothing depends on it.

### 8. Other things that work

- `eth_syncing` returned `false` on all ten endpoints.
- The `safe` and `finalized` block tags work. One sample on mainnet: latest
  133785880, safe 133785879, finalized 133785878. If finality usually sits about
  two blocks behind the head, reorgs can only be very shallow. To check again in
  the reorg detection step.
- Batch requests (several calls in one HTTP request) work on bsc-dataseed.bnbchain.org.
  Not tested on the others, so the tool does not use them.
