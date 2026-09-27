# Benchmarks

Every number below comes from a run of the stack as shipped, started from empty volumes
(`make destroy && make up`, then `make observe`), and is measured by the load generator's own
summary, by Prometheus (5 s scrapes) and by `docker stats`. None is extrapolated.

## Setup

- Host: MacBook Pro, Apple M5 Pro, 24 GB. Docker Engine 28.4 in a colima VM with 8 vCPUs and
  12 GiB.
- The stack exactly as `docker-compose.yml` defines it: edge (Caddy), 2 api replicas, 2 engine
  replicas, PostgreSQL 18.6 + PostGIS 3.6.1, NATS 2.15, plus Prometheus and Jaeger (traces sampled
  at 1 %).
- Container limits: api 1.5 CPU / 768 MiB each, engine 1.5 CPU / 512 MiB each, nats 2 CPU / 1 GiB,
  edge 1 CPU / 256 MiB, db 2 GiB and no CPU limit. The generator (2 CPU) runs in the same VM.
- Load: `generator.py` inside the compose network, posting through the edge like real devices,
  10,000 devices within 12 km of Amsterdam unless stated. With `--observe` it also signs in,
  creates 20 demo zones, watches the live channel and measures the latency from the device's clock
  to the live socket; those figures include the generator's own batching of up to 100 ms
  (`--linger-ms`).
- CPU figures are the share of one core, averaged over the run after the ramp-up.

## 1. The brief's load: 10,000 devices, a report every 3 s, 5 minutes

```bash
make observe
make load ARGS="--devices 10000 --interval 3 --duration 300 --observe bench --zones 20"
```

| | |
|---|---|
| Reports | 980,063 offered, **980,063 accepted** (3,267/s); 0 rejected, dropped, throttled or failed |
| Ingest acknowledgement (`202` = stored in JetStream) | p50 5.4 ms · p95 11 ms · p99 16 ms |
| Engine | 3,259 reports/s in 366 transactions/s; 8 reports per transaction (p99 25); transaction p50 7.3 ms, p99 25 ms |
| API receipt → engine commit | p50 32 ms · p99 98 ms |
| Position, device → socket | p50 130 ms · p95 190 ms · p99 214 ms (all 980,063 delivered) |
| Alert, device → socket | p50 84 ms · p95 123 ms · p99 153 ms (2,556 alerts: 1,421 enter, 981 exit, 154 dwell) |
| Event-loop lag, p99 | api 2.5 ms · engine 3.1 ms |
| Engine backlog | at most 250 reports; admission control never shed |

| Service | CPU | Memory |
|---|---|---|
| api (each) | 3–7 % | 100 MiB |
| engine (each) | 25–43 % | 95 MiB |
| db | 52 % | 387 MiB |
| nats | 14 % | 173 MiB |
| edge | 1 % | 18 MiB |

## 2. Three times the brief: 10,000 devices, a report every second, 2 minutes

| | |
|---|---|
| Reports | 1,130,267 accepted (9,419/s); 0 lost, 0 throttled |
| Ingest acknowledgement | p50 6.1 ms · p99 22 ms |
| Engine | 9,498 reports/s; 20 reports per transaction (p99 68); API receipt → commit p99 176 ms |
| Position / alert, device → socket | p50 107 ms, p99 214 ms / p50 56 ms, p99 144 ms |
| Event-loop lag, p99 | api 2.5 ms · engine 2.5 ms |
| CPU | db 99 % · engines 45–76 % · nats 34 % · api 4–19 % |

Batches grow with the load (8 → 20 reports per transaction), which keeps the cost per report flat.

## 3. One HTTP request per report: 10,000 devices, every 3 s, 2 minutes

The generator batches like a gateway by default (up to 250 reports per request over 32
connections). Here every report is its own request, over 256 connections:

```bash
make load ARGS="--batch 1 --connections 256 --duration 120 --observe bench --zones 20"
```

| | |
|---|---|
| Requests | 379,873 reports in as many requests (3,165/s), all accepted |
| Acknowledgement | p50 8.8 ms · p95 91 ms · p99 150 ms |
| Position / alert, device → socket | p99 775 ms / p99 688 ms |
| CPU | api 72–76 % each · edge 41 % · db 65 % |

Per-request overhead now dominates: the API replicas spend three quarters of a core each on
3,200 requests/s, and since the same processes serve the live sockets, live delivery's tail grows
with them. A deployment expecting one request per device report would add API replicas, or give
ingest and the live channel replicas of their own; batching gateways, or the WebSocket transport
below, avoid the cost altogether.

## 4. WebSocket ingestion with credit flow control: 10,000 devices over 32 sockets, 2 minutes

| | |
|---|---|
| Reports | 380,229 accepted, 0 errors |
| Acknowledgement | p50 4.5 ms · p99 16 ms |
| Position / alert, device → socket | p99 214 ms / p99 141 ms |

## 5. Fan-out: 200 dashboards watching the whole fleet

`scripts/viewers.py` opens 200 live sessions (4 processes × 50, inside the network) with a
viewport over the whole city while 10,000 devices report every 3 s:

```bash
docker compose --profile load run --rm --no-deps --entrypoint python generator \
    /app/scripts/viewers.py --url http://edge:8080 --viewers 50 --duration 110
```

| | |
|---|---|
| Delivered | every viewer received all 3,330 positions/s: **666,000 positions/s** in total, 21 MB/s |
| Device → viewer latency | p50 140 ms · p95 173 ms · p99 195 ms, 0 viewers failed |
| CPU | api 20–22 % each · edge 6 % |

Each position frame is encoded once by the engine and forwarded as the same bytes to all 200
sockets.

## 6. The ceiling: 30,000 devices, a report every second, 90 s

| | |
|---|---|
| Reports | 2,489,149 offered and **2,489,149 accepted** (27,657/s); none dropped or lost, and admission control never shed (one batch was throttled once and resent) |
| Engine | 28,007 reports/s applied, 80 reports per transaction (p99 882) |
| Backlog | peaked at 37,041, below the shedding watermark (150,000) |
| Ingest acknowledgement | p50 11 ms · p99 299 ms |
| CPU | db 200 % · engines 56–96 % · nats 94 % · api 40 % each |

About 28,000 reports per second are sustained on this laptop without shedding — eight times the
brief's load.

## 7. Overload: 60,000 devices, a report every second

About 55,000 reports/s offered, to watch backpressure rather than infer it:

| | |
|---|---|
| Applied | 31,847 reports/s on average over the run, up to about 49,000/s in bursts; **0 accepted reports lost** |
| Admission control | shed with `503` + `Retry-After` whenever the backlog crossed 150,000 and resumed below 50,000 (218 throttle answers); at the end the backlog went from its peak of 151,730 to 0 in about 15 s |
| Client side | the generator dropped 1,862,750 reports it could not send (open loop: its buffer filled while the server throttled) |
| Everything else stays up | `GET /v1/me` through the edge, every 0.5 s during the run: 220 of 220 answered `200` |
| CPU | db 172 % · nats 148 % of its 2 CPUs · engines 46–80 % |

Backlog over time (Prometheus, every 4 s; `admitting` is 0 while shedding; the gauges are scraped
separately, so a shed that starts and ends between two samples shows only in the next one):

```
21:42:23  backlog  57,257  admitting 1   applied/s 46,116
21:42:27  backlog 127,398  admitting 1   applied/s 38,687
21:42:31  backlog 105,251  admitting 0   applied/s 31,197
21:42:35  backlog  86,144  admitting 0   applied/s 22,068
21:42:39  backlog  55,349  admitting 1   applied/s 35,853
21:42:43  backlog 137,394  admitting 1   applied/s 38,192
21:42:47  backlog  52,634  admitting 0   applied/s 39,592
21:42:51  backlog 135,849  admitting 1   applied/s 35,753
21:42:55  backlog 135,849  admitting 1   applied/s 49,673
21:42:59  backlog  41,852  admitting 1   applied/s 39,661
21:43:03  backlog 114,738  admitting 1   applied/s 48,638
21:43:07  backlog 114,192  admitting 0   applied/s 40,034
…
21:43:35  backlog 151,730  admitting 0   applied/s 26,465
21:43:43  backlog  41,305  admitting 1   applied/s 22,262
21:43:51  backlog       0  admitting 1   applied/s  9,798
```

At the top of the range PostgreSQL (about two cores) and NATS (close to its two-CPU limit) are the
busiest components, with the generator using much of the rest of the VM.

## 8. Failure drills

`make drill` runs the generator (10,000 devices, a report every 3 s, 90 s, an observer with 20
zones), SIGKILLs one replica 35 s in, starts it again 15 s later, lets the pipeline drain and audits
the result. Both pass every check (the engine drill ran on a fresh clone, after `make up`,
`make smoke` and a one-minute load):

| Check | engine killed | api killed |
|---|---|---|
| Accepted reports stored exactly once | 280,050 accepted = stream grew by 280,050 | 280,335 = 280,335 |
| Every stored report applied | 0 pending, 0 unacknowledged | 0 / 0 |
| Enter/exit strictly alternate per device and zone | 0 violations | 0 |
| Exits without an enter · duplicate alerts | 0 · 0 | 0 · 0 |
| Alerts received live by the observer vs stored | 1,290 / 1,290 | 1,348 / 1,348 |
| Recovery | orphaned partitions owned again **7.0 s** after the kill (lease TTL 6 s + one round) | sockets on the killed replica reconnect to the other one and resume by sequence; the observer missed nothing |

During the engine drill, the reports of devices on the orphaned partitions waited in the stream
for those seven seconds: ingest went on, their positions and alerts came late, and nothing was
lost.
