# Host stability (A1a, finding F-C3)

Diagnosis of the HP Z840 that runs the whole trading stack. Evidence was collected read-only on
2026-10-05 between 17:35 and 21:00 EEST: no sudo, no system changes, no container restarts. The
items that need sudo or physical access are in §4 and belong to the operator.

**Short version**

- The host is **down much more from auto-suspend than from crashes.** GNOME suspends it after 30 minutes
  idle. Between Sep 30 and Oct 5 it was **asleep about 68 of 109 hours (≈62%)**. The 01:00–04:59 journal
  gap listed in F-C3 was a suspend (00:55→05:19), not a crash. Exit stops are not enforced while the host sleeps.
- The **crashes are silent.** No panic, oops, MCE, EDAC, NVIDIA Xid, lockup, OOM or NVMe error was
  logged before any of them. The journal simply stops. That points at the platform (power delivery,
  GPU or PCIe hang, or a kernel regression), not at something the kernel noticed and logged.
- **CPU load did not cause them.** Load 10–14 on 88 hardware threads is about 15% utilisation, and CPU, memory
  and IO pressure are close to zero. The scanner still burns about 11 cores to do work that needs a fraction of
  that, and it is **7.3 hours behind real time** (§3). That is a correctness problem, and it adds to power draw.
- **Root cause: not proven.** The suspects are ranked in §2. Each has a cheap test that tells them apart.

---

## 1. Evidence

### 1.1 Platform

| Item | Value |
|---|---|
| Machine | HP Z840, BIOS M60 v02.62 (2024-01-04) |
| CPU | 2 × Xeon E5-2696 v4 (22C/44T each): 88 threads, 2 NUMA nodes |
| RAM | 62 GiB ECC; EDAC `sb_edac` loaded on 4 memory controllers; `ce_count` = `ue_count` = 0 |
| Swap | **none** (`swapon --show` empty, `/proc/swaps` empty) |
| GPU | RTX 3080 Ti, driver 580.178.04, power limit 350 W (default = max, min 100 W) |
| Kernel | 7.0.0-38-generic (installed 2026-10-04 01:35, first booted 2026-10-04 22:40). 7.0.0-34 is still installed |
| Crash tooling | none: no kdump, no rasdaemon, no mcelog. `kernel.panic=0`, `hardlockup_panic=0`, `softlockup_panic=0`. pstore is not readable without root. `memtest86+` 7.00 **is installed** (GRUB entry). |
| Logging | journald persistent; sysstat collects every 10 min (`/var/log/sysstat/sa*`) |

### 1.2 Outage timeline (`last -x`, `journalctl --list-boots`, sysstat)

"Journal ends" is the last persisted entry of the boot. "Next boot" is when the machine came back.

| Boot | Kernel | Journal ends | Next boot | How it ended |
|---|---|---|---|---|
| -14 | 7.0.0-31 | Sep 10 18:48:46 | Sep 13 22:28 | **Died entering suspend.** The last line is `PM: suspend entry (deep)` and it never resumed. |
| -13 | 7.0.0-31 | Sep 15 20:45:01 | Sep 15 21:29 | Silent stop; the last line is a sysstat cron entry |
| -7 | 7.0.0-34 | Oct 4 22:39 | Oct 4 22:40 | clean reboot into -38. Spent 63.5 of 101 h **suspended** |
| -6 | 7.0.0-38 | Oct 5 07:05:01 | 07:17 | **Freeze and hard reset** (operator). Silent; the last line is a sysstat cron entry. Suspended 00:55→05:19 |
| -5 | 7.0.0-38 | 14:20:01 | 14:47 | Silent stop, 7 h after boot |
| -4 | 7.0.0-38 | 15:23:28 | 15:55 | Silent stop, 36 min after boot, during container start-up (veth / avahi lines) |
| -3 | 7.0.0-38 | 15:55:41 | 15:57 | Silent stop **15 s after boot**, 1 s after Ollama's "discovering available GPUs", while dockerd was restarting containers |
| -2 | 7.0.0-38 | 15:59:01 | 16:21 | Silent stop about 1.5 min after boot. Ollama had probed the GPU at 15:57:50–57 |
| -1 | 7.0.0-38 | 16:24:14 | 16:37 | Silent stop about 3 min after boot. Two Ollama runners were loaded on the GPU at 16:22:29 (gemma4 12b, 9 GB + 1 GB) |
| 0 | 7.0.0-38 | running | | Up 4 h 22 min at 21:00. **No crash so far**, including at load 35 (17:50) |

The earlier crash in `last -x` (May 17, kernel 6.17) is outside the journal window.

### 1.3 Kernel log (`journalctl -k -b -1 … -9 -p warning`)

- **Not seen on any boot:** `mce:`/Machine Check/Hardware Error, EDAC CE/UE, `NVRM: Xid`, "fallen off the bus",
  soft/hard lockup, hung_task, OOM kill, NVMe timeout/reset, EXT4 error, `BUG:`/Call Trace, panic, or
  thermal throttling. The next boot did not report MCEs left over from before the reset either.
- Noise present on every boot, unrelated to the crashes: ACPI `_PCT`/`WMIV` firmware errors, "BIOS needs update
  for CPU frequency support", `hp-wmi: Failed to apply initial fan settings: -22`, `mpt2sas` NVDATA override.
- USB Wi-Fi dongle (`rtw88_8822bu`): repeated "firmware failed to leave lps state", "failed to send h2c",
  "write register 0xc4 failed with -71". The dongle is flaky, but nothing links it to the hangs.
- Python segfaults in libc on Sep 30 21:07–21:11 (boot -7), unrelated to the hangs.

### 1.4 Resources now (21:00, boot 0)

- `free -h`: 16 GiB used, **45 GiB available**, 0 swap. sysstat `%commit` is 130–140%: committed memory exceeds RAM with no swap behind it.
- PSI `/proc/pressure/{cpu,memory,io}`: cpu some avg300 = 0.01, memory 0, io 0.
- `sensors`: CPU package 51 °C / 45 °C (high 83, crit 85), NVMe 35 °C.
- `nvidia-smi -q`: 50 °C at idle, P8. HW Thermal Slowdown and HW Power Brake counters are **0 µs**.
  **SW Power Capping: 227 s** in the first hour of this boot, so inference bursts regularly drive the card
  to its 350 W limit.
- sysstat today: CPU user+sys about 10–14% of 88 threads. Load average 8–14. One spike to **ldavg-1 = 35.4 at 17:50**
  (the coordinator saw ~39). That window overlaps a Playwright e2e run from another agent, which pushed the api
  container to 12 cores, and the benchmarks in §3, which run inside the scanner container. The spike is
  therefore partly caused by the diagnostics and is not steady-state. The host did not crash during it.

### 1.5 Auto-suspend

`gsettings get org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type` returns `'suspend'`, with a
timeout of `1800` s. `systemd-logind` logs "The system will suspend now!" before each sleep:

| Suspend → resume | Asleep |
|---|---|
| Sep 30 23:42 → Oct 1 11:17 | 11.6 h |
| Oct 1 12:23 → 14:47 | 2.4 h |
| Oct 1 14:48 → 20:15 | 5.5 h |
| Oct 1 21:20 → 22:19 | 1.0 h |
| **Oct 1 22:54 → Oct 3 14:00** | **39.1 h** |
| Oct 3 15:51 → 16:05 | 0.2 h |
| Oct 4 10:07 → 10:16 | 0.2 h |
| Oct 4 17:53 → 20:43 | 2.8 h |
| Oct 4 21:49 → 22:37 | 0.8 h |
| Oct 5 00:55 → 05:19 | 4.4 h |

That is about 68 h asleep out of about 109 h. On top of that, resume is fragile: `xHC error in resume`, USB
"invalid context state", and Sep 10 never came back from suspend at all.

---

## 2. Ranked suspects

| # | Suspect | Confidence | For | Against | Test that settles it |
|---|---|---|---|---|---|
| 1 | **Idle auto-suspend** (an outage, not a crash) | **Verified** | §1.5. It produced the 01:00–04:59 gap, a 39 h outage, and the Sep 10 crash (died entering S3) | none | Disable suspend (§4.1). After that, `journalctl | grep "PM: suspend entry"` must stay empty |
| 2 | **Power delivery under combined CPU + GPU load** (PSU over-current trip or GPU transient) | **Medium** | Silent stops with no MCE or Xid, the typical signature of a power-off or platform reset. Boots -3, -2 and -1 died 15 s to 3 min after boot, exactly when every container starts, the scanner primes all specs at once on 88 threads, and Ollama initialises the GPU and loads models. The 3080 Ti spends minutes at its 350 W cap, and Ampere cards spike well above TDP for milliseconds. Z840 PSUs are 850 W or 1125 W, and a 3080 Ti on HP's PCIe leads usually goes through adapters. Two 145 W Xeons on top. | Not proven. The operator reported 07:05 as a *freeze* (display hung), not a power-off. No PSU or BIOS event log has been read yet. | Read the PSU label and GPU cabling. Read the BIOS event log. Run `nvidia-smi -pl 250` (§4.3) for a week. Optionally stress test: CPU `stress-ng --cpu 88` plus a GPU burn at the same time; if the box drops under combined load but not under either alone, it is power |
| 3 | **Kernel 7.0.0-38 regression** (or its NVIDIA module build) | **Medium-low** | All 5 Oct 5 crashes are on -38, which was first booted Oct 4 22:40. Before that, -34 had no crash | -34 was awake only about 37 h (it spent the rest suspended), so "no crash" is weak. -31 also had a silent stop (Sep 15) | Boot 7.0.0-34 from GRUB → Advanced options for a few days of normal load. No sudo needed to pick it once at boot |
| 4 | Memory (ECC DIMM, memory controller) | Low | none observed | ECC with EDAC loaded, 0 CE / 0 UE. An uncorrectable error would normally leave an MCE that shows at the next boot | `memtest86+` overnight (§4.2). It is already in GRUB |
| 5 | Memory exhaustion (no swap) | Low | Commit is 130–140% of RAM with 0 swap | 43–45 GB available. PSI memory is 0. No OOM kills. An OOM is logged, not silent | Add swap or zram anyway (§4.1), as the plan requires |
| 6 | Thermal | Low | `hp-wmi` cannot set the initial fan profile | CPU at 45–51 °C, GPU HW thermal slowdown at 0 µs, no thermal messages | Log `sensors` and `nvidia-smi` every minute (§4.4) so the next crash has the last readings |
| 7 | CPU load from the scanner or tests | Very low as a cause | Load 14, peaks at 35–39 | 88 threads; utilisation about 15% (40% at peak), CPU PSI about 0. No crash during the 17:50 peak | none needed. Still fix it (§3), because it adds power draw and the scanner is 7 h behind |

---

## 3. Scanner CPU: why 1,080%, and what to do

### 3.1 Measurements

- `ps`: `python -m cryptonite_scanner_service.server` uses **10.8–11.0 cores on average over its lifetime** (64 min and
  4 h 22 min samples) with **365 threads**. A 60 s cgroup `cpu.stat` window gave 8.9 cores (31% of host busy).
  It accounts for about 11 of the 12–14 runnable tasks in the load average.
- `docker stats --no-stream`: scanner 705%. The api showed 1,696%, but that was an e2e run; its lifetime average is 0.16 cores.
- DuckDB 1.5.5 and Polars 1.44.1 both default to **88 threads**. No `POLARS_MAX_THREADS`, no DuckDB `threads`
  setting, and no `cpus:` limit on the scanner in `infra/docker-compose.yml`.
- **Work per closed bar:** `_handle_stream_message` runs **every active spec**, and `_execute_scan` scans **every
  instrument in the spec's universe**, not only the one whose bar closed (`worker.py:280-320` loops over specs; `worker.py:455-488` loops over instruments and evaluates).
  There are **16 active specs, 11 of them `test-spec-*` copies**, each over BTC and ETH. Two 1m bars close
  per minute.
- **Lookback:** `SCAN_LOOKBACK_BARS = 300` bars of the spec's *largest* timeframe. For 15m/1h specs that is 12.5 days
  (18k 1m rows per instrument). `scalp-multi-tf` includes `1d`, so it loads **300 days (≈284k rows per instrument)**.
- **Cost of one spec run** (BTC + ETH; read-only benchmark run inside the scanner container):

  | Run | Threads | Wall | CPU | of which DuckDB lake read |
  |---|---|---|---|---|
  | 15m/1h spec | 88 | 2.7 s | 31.8 s | 2.5 s wall (92%) |
  | 15m/1h spec | 4 | 4.7 s | 15.2 s | 4.6 s |
  | scalp-multi-tf to 1d (as configured) | 88 | 7.8 s | 81.4 s | 3.6 s |
  | scalp-multi-tf to 4h | 88 | 4.2 s | 56.9 s | 3.1 s |
  | scalp-multi-tf to 4h | 4 | 9.1 s | 27.5 s | 8.2 s |

- **Why the read is so expensive:** the lake has **4,896 parquet files for BTCUSDT 1m**. Live days hold 600–875 files
  each (one per ingest flush), and the 12.5-day window alone has 2,830. DuckDB opens all of them on every run.
  Pruning by date does not help, because the window itself is the small-file problem. In a scratchpad copy
  compacted to one file per day, the same query took **81 ms wall and 0.67 s CPU instead of 1,065 ms and 13.1 s**,
  about **20× less CPU**.
- **It cannot keep up.** One event costs about 15 × 2.7 s + 7.8 s ≈ **48 s** of wall time, and two events arrive per minute.
  `scan_runs` shows 919 runs in the last hour against the 1,920 needed. The `scanner-worker` group on
  `cryptonite:stream:closed_bar_committed` has **lag 661, and its last delivered entry is 7.3 h older than the newest one**.
  Every candidate it produces is therefore hours stale. This is a second driver of F-H2's 82% STALE share, separate
  from the restart replays.

### 3.2 Recommended mitigation (not applied)

Thread caps alone make things worse: they halve CPU but nearly double wall time, so the lag grows faster.
**Cut the work first, then cap.**

| Step | Kind | Change | Expected effect (from the benchmark above) |
|---|---|---|---|
| **M1** | Data, reversible, no restart | Archive the 11 `test-spec-*` specs (`UPDATE scan_specs SET is_archived = true WHERE slug LIKE 'test-spec-%'`, after a backup). The handler reloads active specs on every message, so this takes effect immediately. It also removes ~2,100 duplicate candidates per 3 days (F-H2) | 48 s → **18.6 s** per event. The scanner keeps up (62% duty) and drops to about **7 cores**. Load average falls by about 4 |
| **M2** | Data (new spec version) | Drop `1d` from `scalp-multi-tf`; Q8 says trigger on 15m/1h/4h. This cuts its lookback from 300 days to 50 days | With M1: **15 s** per event, 50% duty, about **6 cores** |
| **M3** | Compose; applies at the next planned recreate of `scanner` | `cpus: "8"` on the scanner, plus `POLARS_MAX_THREADS: "8"` as an explicit backstop. DuckDB 1.5.5 reads cgroup v2 `cpu.max` (verified in the binary), and Polars sizes its pool from Rust's `available_parallelism`, which honours the quota, so both pools shrink without code. Use 8, not 4: at 4 threads, M1+M2 need about 28 s per event (93% duty), which leaves no room to drain the lag | Hard ceiling of 8 cores even if specs are added. Wall time per event lands between the 88-thread and 4-thread figures (the 8-thread point was not measured) |
| **M4** | Code, follow-up item (F-H2 / Phase J) | (a) Compact the lake's day partitions (nightly, or have ingest append to a daily file). (b) Load the 1m frame once per instrument per event and share it across specs. (c) Scan only the instrument whose bar closed. (d) Evaluate only the newly closed bar(s), not every row of the lookback. (d) also stops historical setups being enqueued after restarts | (a) alone is about 20× less read CPU. With (b)–(d) the scanner should idle at a fraction of one core |

**I would apply M1 now and M2 with it, then M3 at the next planned restart, and schedule M4.**
Before or with M1, the operator must decide what happens to the 661-event backlog. Draining it produces another
~7 h of stale candidates. Skipping it (`XGROUP SETID cryptonite:stream:closed_bar_committed scanner-worker $`)
drops them. Given F-H2, skipping is the better choice, but it is an operator call.

---

## 4. Operator actions (need sudo or physical access)

### 4.1 Keep the host awake, and add swap (the plan's "done when")

```bash
# Stop GNOME idle-suspend (as the desktop user; no sudo)
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
# Belt and braces: block every sleep path system-wide
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target

# 16 GB swap file (or zram below)
sudo fallocate -l 16G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo sysctl -w vm.swappiness=10 && echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
# Alternative: zram
# sudo apt install systemd-zram-generator && printf '[zram0]\nzram-size = 16384\ncompression-algorithm = zstd\n' | sudo tee /etc/systemd/zram-generator.conf && sudo systemctl daemon-reload && sudo systemctl start systemd-zram-setup@zram0
swapon --show   # must list the device
```

### 4.2 Make the next crash leave evidence

```bash
sudo apt install kdump-tools rasdaemon       # say yes to enabling kdump; reboot once to reserve crashkernel memory
sudo systemctl enable --now rasdaemon
printf 'kernel.hardlockup_panic=1\nkernel.softlockup_panic=1\nkernel.panic=30\nkernel.unknown_nmi_panic=1\n' | sudo tee /etc/sysctl.d/99-crash.conf && sudo sysctl --system
sudo mkdir -p /etc/systemd/journald.conf.d && printf '[Journal]\nSyncIntervalSec=15s\n' | sudo tee /etc/systemd/journald.conf.d/sync.conf && sudo systemctl restart systemd-journald
# after the next crash:
sudo ls /var/crash /sys/fs/pstore; sudo ras-mc-ctl --summary; sudo ras-mc-ctl --errors
```

If the box still dies with nothing in kdump, pstore or rasdaemon, the kernel never got a chance to react. That
points strongly at power or a platform reset (suspect 2) rather than at software.

### 4.3 Hardware checks

```bash
# GPU power cap while the trading stack runs (resets at reboot unless re-applied by a unit)
sudo nvidia-smi -pm 1 && sudo nvidia-smi -pl 250
nvidia-smi -q -d POWER | grep -E "Current Power Limit|Power Capping"
```

- **PSU:** read the label (Z840: 850 W or 1125 W) and check how the 3080 Ti's two 8-pin inputs are fed (native leads
  or 6-pin adapters, and whether one lead is daisy-chained to both). Consider the 1125 W unit or a lower GPU cap.
- **BIOS event log:** F10 at boot → look for the BIOS / system event log (on Z-series it sits under the Main or
  Advanced menu), and photograph any power, thermal or memory events with timestamps. The Z840 has no BMC/IPMI,
  so `ipmitool sel list` does not apply.
- **BIOS update:** M60 v02.62 is dated 2024-01. Check HP for a newer Z840 release; the ACPI `_PCT`/WMI errors suggest
  firmware gaps.
- **memtest86+ overnight:** reboot, choose "Memory test (memtest86+)" in GRUB (installed, v7.00), and let it run at least
  4 passes. Any error means reseating or replacing that DIMM.
- **Kernel A/B:** pick 7.0.0-34 under GRUB → Advanced options for a few days. To pin it:
  `sudo grub-set-default "Advanced options for Ubuntu>Ubuntu, with Linux 7.0.0-34-generic"` (requires
  `GRUB_DEFAULT=saved` in `/etc/default/grub` and `sudo update-grub`).
- **Combined-load test** (only while the trading stack is stopped and with someone present): run
  `stress-ng --cpu 88 --timeout 10m` and a GPU load at the same time, then each alone.

### 4.4 Lightweight telemetry until resolved (no sudo)

```bash
# one line per minute; survives as long as the disk write does
( while true; do date -Is; sensors | grep -E "Package id|Composite"; \
  nvidia-smi --query-gpu=power.draw,temperature.gpu,clocks_event_reasons.active --format=csv,noheader; \
  cat /proc/loadavg; sleep 60; done ) >> ~/host-telemetry.log 2>&1 &
```

---

## 5. Done-when status (plan item A1a)

- Ranked suspects written down: **yes** (§2).
- Swap active: **no.** It needs sudo (§4.1).
- Root cause: **open.** The next decisive data points are the PSU and BIOS log, a week at `-pl 250` with
  suspend disabled, and kdump/rasdaemon output if another crash happens. If crashes continue after §4.1–4.3,
  go ahead with A1b (move the always-on core off this box).
