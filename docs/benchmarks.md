# Benchmarks

Every number below comes from a run of the stack as shipped (`make up`), measured by the
generator's own summary, by Prometheus (`make observe` profile, 5 s scrapes) and by `docker stats`.
None is extrapolated.

## Setup

- Host: MacBook Pro, Apple M5 Pro, 24 GB. Docker Engine 28.4 in a colima VM with 8 vCPUs and
  12 GiB.
- The stack exactly as `docker-compose.yml` defines it: edge (Caddy), 2 api replicas, 2 engine
  replicas, PostgreSQL 18.6 + PostGIS 3.6.1, NATS 2.15, plus Prometheus.
- Container limits: api 1.5 CPU / 768 MiB each, engine 1.5 CPU / 512 MiB each, db 3 CPU / 2 GiB,
  nats 1 CPU / 1 GiB, edge 1 CPU / 256 MiB.
- Load: `generator.py` inside the compose network, posting HTTP batches **through the edge** like
  real devices (`docker compose --profile load run --rm generator ...`), 10,000 devices within
  12 km of Amsterdam unless stated. With `--observe`, the generator also signs in, creates 20
  demo zones, watches the live channel and measures device-clock-to-socket latency.
- CPU is the share of one core; the latency figures include the generator's own batching of up
  to 100 ms before a report is sent (`--linger-ms`).

## 1. Steady state: 10,000 devices, a report every 3 s, 5 minutes

```bash
docker compose --profile load run --rm generator \
    --devices 10000 --interval 3 --duration 300 --observe bench --zones 20
```

| | |
|---|---|
| Reports | 980,397 offered, **980,397 accepted** (3,268/s); 0 rejected, dropped, throttled or failed |
| Ingest acknowledgement (`202` = stored in JetStream) | p50 5.3 ms · p95 9.9 ms · p99 13.1 ms |
| Engine | 3,258 reports/s in 392 transactions/s; 7 reports per transaction (p99 25); transaction p50 6.3 ms, p99 22.6 ms |
| API receipt → engine commit | p50 30 ms · p99 93 ms |
| Position, device → browser | p50 128 ms · p95 183 ms · p99 202 ms (all 980,397 delivered) |
| Alert, device → browser | p50 79 ms · p95 111 ms · p99 133 ms (3,001 alerts: 1,714 enter, 1,129 exit, 158 dwell) |
| Event-loop lag, p99 | api 4.9 ms · engine 5.5 ms |
| Engine backlog | at most 322 reports; admission control never shed |

| Service | CPU | Memory |
|---|---|---|
| api (each) | 2–8 % | ~100 MiB |
| engine (each) | 26 % | 97 MiB |
| db | 51 % | 409 MiB |
| nats | 11 % | 148 MiB |
| edge | 1 % | 18 MiB |

## 2. Three times the brief: 10,000 devices, a report every second

| | |
|---|---|
| Reports | 1,129,411 accepted (9,412/s) in 2 minutes; 0 lost, 0 throttled |
| Ingest acknowledgement | p50 5.2 ms · p99 19 ms |
| Engine | 9,375 reports/s; 18 reports per transaction (p99 49); API receipt → commit p99 94 ms |
| Position / alert, device → browser | p50 97 ms, p99 183 ms / p50 48 ms, p99 123 ms |
| Event-loop lag, p99 | api 3.3 ms · engine 3.7 ms |
| CPU | db 84 % · engines 42 % each · nats 23 % · api 3–18 % |

Batches grow with the load (7 → 18 reports per transaction), which is what keeps the cost per report
flat.

## 3. The ceiling: 30,000 devices, a report every second

| | |
|---|---|
| Reports | 2,490,877 accepted at **27,675/s**; engines applied 27,090/s |
| Backlog | peaked at 47,165 reports, below the shedding watermark (150,000) |
| Ingest acknowledgement | p50 12 ms · p99 760 ms |
| Bottleneck | PostgreSQL at 1.9 of its 3 cores; engines ~70 % each, nats 80 % |

About 28,000 reports per second is what this laptop sustains without shedding — eight times the
brief's load.

## 4. Overload: 60,000 devices, a report every second

~55,000 reports/s offered, twice the ceiling, to watch backpressure rather than infer it:

| | |
|---|---|
| Accepted | 2,642,012 reports at 29,355/s; **0 lost, 0 errors** |
| Admission control | shed when the backlog crossed the watermark: `503` + `Retry-After` (63 throttle responses); the backlog peaked at 131,888 and drained to 0 within 8 s, then ingest resumed |
| Client side | the generator dropped 2,337,000 reports it could not send (open loop: its buffer filled while the server throttled) |
| After the run | backlog back to 0 in ~5 s |

Backlog over time (Prometheus, every ~4 s, `admitting` = 0 while shedding):

```
16:09:47  backlog  93,850  admitting 1   applied/s 35,559
16:09:51  backlog 125,056  admitting 1   applied/s 35,442
16:09:55  backlog 131,888  admitting 1   applied/s 33,800
16:09:59  backlog 128,057  admitting 0   applied/s 33,550
16:10:03  backlog  52,012  admitting 0   applied/s 28,897
16:10:07  backlog       0  admitting 1   applied/s 12,462
```

## 5. WebSocket ingestion with credit flow control

10,000 devices over 32 sockets (`--transport ws`), a report every 3 s, 2 minutes:

| | |
|---|---|
| Reports | 380,129 accepted, 0 errors |
| Acknowledgement | p50 5.7 ms · p99 61 ms |
| Position / alert, device → browser | p99 256 ms / p99 175 ms |

## 6. Fan-out: 200 dashboards watching the whole fleet

`scripts/viewers.py` opens 200 live sessions (4 processes × 50, inside the network) with a
viewport over the whole city, while 10,000 devices report every 3 s:

```bash
docker compose --profile load run --rm --no-deps -v "$PWD/scripts:/scripts:ro" \
    --entrypoint python generator /scripts/viewers.py --url http://edge:8080 --viewers 50
```

| | |
|---|---|
| Delivered | every viewer received all ~3,325 positions/s: **665,000 positions/s** in total, 27 MB/s |
| Device → viewer latency | p50 150 ms · p95 218–246 ms · p99 290–490 ms (two runs) |
| Server side | socket send p99 0.1 ms, 0 frames dropped, event-loop lag p99 18 ms (api) |
| CPU | api 26–37 % each · edge 9 % |

Each position frame is encoded once by the engine and forwarded as the same bytes to all 200
sockets. (Run from the host through the VM's port forwarding, the same test shows a p99 above a
second: that tail belongs to the port forwarder, not the stack.)

## 7. Failure drills

`make drill` runs the generator (10,000 devices, a report every 3 s, 90 s, observer with 20
zones), SIGKILLs one replica 35 s in, starts it again later, lets the pipeline drain and audits the
result. Both drills pass every check:

| Check | engine killed | api killed |
|---|---|---|
| Accepted reports stored exactly once | 280,601 accepted = stream grew by 280,601 | 280,358 = 280,358 |
| Every stored report applied | 0 pending, 0 unacknowledged | 0 / 0 |
| Enter/exit strictly alternate per device and zone | 0 violations | 0 |
| Exits without an enter · duplicate alerts | 0 · 0 | 0 · 0 |
| Alerts received live by the observer vs stored | 1,212 / 1,212 | 1,249 / 1,249 |
| Recovery | orphaned partitions owned again **7.8 s** after the kill (lease TTL 6 s + one round) | the observer's socket was on the killed replica: it reconnected, resumed by sequence and missed nothing |

During the engine drill, devices on the orphaned partitions paused for those ~8 s (positions p99
4 s over the whole run); nothing they reported was lost.
