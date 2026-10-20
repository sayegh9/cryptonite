# Host stability (A1a, finding F-C3)

Diagnosis of the HP Z840 that runs the whole trading stack. The host runs **Zorin OS 18.1** on an Ubuntu
24.04 "noble" base. Evidence was collected read-only between 2026-10-05 17:35 and 2026-10-06 01:55 EEST:
no sudo, no system changes, no container restarts. The items that need sudo or physical access are in §4
and belong to the operator. Every §4 step comes with its rollback.

Revision 2 (2026-10-06) applies the Reality Checker review of PR #22:
- GRUB commands rewritten for Zorin OS, with the hidden menu.
- A rollback for every step.
- The zram start command fixed.
- A persistent GPU-cap unit and a check command that actually finds the counter.
- Stale-candidate mechanism corrected.
- 6 crashes on Oct 5, not 5.
- The power-cap counter claim withdrawn.
- Swap sized for the disk.

**Short version**

- The host is **down much more from auto-suspend than from crashes.** GNOME suspends it after 30 minutes
  idle. Between Sep 30 and Oct 5 it was **asleep about 68 of 109 hours (≈62%)**. The 01:00–04:59 journal
  gap listed in F-C3 was a suspend (00:55→05:19), not a crash. Exit stops are not enforced while the host sleeps.
- The **crashes are silent.** No panic, oops, MCE, EDAC, NVIDIA Xid, lockup, OOM or NVMe error was
  logged before any of them. The journal simply stops. That points at the platform (power delivery,
  GPU or PCIe hang, or a kernel regression), not at something the kernel noticed and logged.
- **CPU load did not cause them.** Load 10–14 on 88 hardware threads is about 15% utilisation, and CPU, memory
  and IO pressure are close to zero. The scanner still burns about 11 cores to do work that needs a fraction of
  that, and its event backlog keeps growing (§3). That wastes CPU and adds to power draw.
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
| Crash tooling | none: no kdump, no rasdaemon, no mcelog. `kernel.panic=0`, `hardlockup_panic=0`, `softlockup_panic=0`, `nmi_watchdog=1`. pstore is not readable without root. `memtest86+` 7.00 **is installed** (GRUB entry). |
| GRUB | `GRUB_DEFAULT=0`, `GRUB_TIMEOUT_STYLE=hidden`, `GRUB_TIMEOUT=10`. `GRUB_DISTRIBUTOR` comes from `NAME` in `/etc/os-release`, so the submenu is "Advanced options for **Zorin OS**", not "…for Ubuntu". The menu is hidden: press **Esc** during the 10 s window to see it |
| Disk | `/` (nvme0n1p3, ext4) 236 G, **83% used, 39 G free**, and the lake keeps growing |
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
| 0 | 7.0.0-38 | running | | Up 9 h 14 min at Oct 6 01:52. **No crash so far**, including at load 35 (17:50) |

That makes **6 crashes on Oct 5** (boots -6 to -1), all on 7.0.0-38. The earlier crash in `last -x` (May 17,
kernel 6.17) is outside the journal window.

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
- **The "SW Power Capping" counter does not show real 350 W draw.** It read 227 s after 1 h, 8,533 s after 4.5 h
  (Reality Checker), and 12,952 s after 9.3 h (about 39% of uptime). In a 90 s sample on Oct 6 at 01:55, though,
  it grew **17 s** while measured `power.draw` never went above **27 W** (median 27 W; the `sw_power_cap` flag was
  active in 1 of 86 samples). So it also counts time spent near idle. The earlier reading "inference keeps the card
  at 350 W" is withdrawn. Real peak draw is unknown until `power.draw` is logged per second or per minute under load
  (§4.6).
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
| 2 | **Power delivery under combined CPU + GPU load** (PSU over-current trip or GPU transient) | **Medium (circumstantial)** | Silent stops with no MCE or Xid, the typical signature of a power-off or platform reset. Boots -3, -2 and -1 died 15 s to 3 min after boot, exactly when every container starts, the scanner primes all specs at once on 88 threads, and Ollama initialises the GPU and loads models. Ampere cards are known to spike well above TDP for milliseconds. Z840 PSUs are 850 W or 1125 W, and a 3080 Ti on HP's PCIe leads usually goes through adapters. Two Xeon E5-2696 v4 (about 150 W TDP each) on top | Not proven, and **no power draw has been measured under load**: the SW power-cap counter turned out not to reflect real draw (§1.4). The operator reported 07:05 as a *freeze* (display hung), not a power-off. No PSU or BIOS event log has been read yet | First log real `power.draw` (§4.6). Read the PSU label, GPU cabling and BIOS event log. Run `nvidia-smi -pl 250` (§4.4) for a week. Optionally a combined stress test (§4.5): if the box drops under combined load but not under either alone, it is power |
| 3 | **Kernel 7.0.0-38 regression** (or its NVIDIA module build) | **Medium-low** | All **6** Oct 5 crashes (boots -6 to -1) are on -38, which was first booted Oct 4 22:40. Before that, -34 had no crash | -34 was awake only about 37 h (it spent the rest suspended), so "no crash" is weak. -31 also had a silent stop (Sep 15) | Boot 7.0.0-34 for a few days of normal load (§4.5, one-time `grub-reboot`, or Esc at the hidden menu) |
| 4 | Memory (ECC DIMM, memory controller) | Low | none observed | ECC with EDAC loaded, 0 CE / 0 UE. An uncorrectable error would normally leave an MCE that shows at the next boot | `memtest86+` overnight (§4.5). It is already in GRUB |
| 5 | Memory exhaustion (no swap) | Low | Commit is 130–140% of RAM with 0 swap | 43–45 GB available. PSI memory is 0. No OOM kills. An OOM is logged, not silent | Add swap or zram anyway (§4.2), as the plan requires |
| 6 | Thermal | Low | `hp-wmi` cannot set the initial fan profile | CPU at 45–51 °C, GPU HW thermal slowdown at 0 µs, no thermal messages | Log `sensors` and `nvidia-smi` every minute (§4.6) so the next crash has the last readings |
| 7 | CPU load from the scanner or tests | Very low as a cause | Load 14, peaks at 35–39 | 88 threads; utilisation about 15% (40% at peak), CPU PSI about 0. No crash during the 17:50 peak | none needed. Still fix it (§3), because it adds power draw and the scanner cannot keep up |

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
  `scan_runs` shows 919 runs in the last hour (907 in the Reality Checker's re-check) against the 1,920 needed.
  `XINFO GROUPS` lag on `scanner-worker` for `cryptonite:stream:closed_bar_committed`:

  | When | Lag (events) | Last-delivered ID behind newest |
  |---|---|---|
  | Oct 5 ~17:45 | 661 | 7.3 h |
  | Oct 5 ~21:05 | 881 | 548 min |
  | Oct 6 01:52 | **1,189** | 702 min |

  At 2 events per minute, 661 events is about 5.5 h of bars. The "behind" column is wall-clock age, which includes
  the Oct 5 outage gaps, so it overstates the lag.
- **The lag does not make candidates stale. It is wasted work.** `_execute_scan` sets
  `start_ns = time.time_ns() - lookback` and calls `query_bars_df` without `end_ns`, so every run reads the lake **up to
  now**, however old the triggering event is. The debate gate judges staleness by the candidate's own
  `bar_close_inclusive_ns` (`services/debate_worker/gate.py:124-128`), not by event age.
  The stale candidates come from `ScannerEngine.evaluate` turning **every matching row of the lookback window** into
  a candidate. New rows are only inserted when the de-duplication key (spec, instrument, timeframe, bar close) has not
  been seen before, but old bars keep qualifying as the window and its indicators move. Candidates created in the last
  6 h (read-only query, Oct 6 01:55) were old at creation: **15m median 17,116 min (≈11.9 d)**, 1h median 16,711 min
  (≈11.6 d), while 5m was fresh. That matches the 12.5-day lookback, not the hours of consumer lag. The Reality Checker
  measured the same thing independently (15m median 17,670 min ≈ 12.3 d over 3 h). The real driver of F-H2's STALE share
  is the full-lookback evaluation, which M4(d) fixes.

### 3.2 Recommended mitigation (not applied)

Thread caps alone make things worse: they halve CPU but nearly double wall time, so the lag grows faster.
**Cut the work first, then cap.**

| Step | Kind | Change | Expected effect (from the benchmark above) |
|---|---|---|---|
| **M1** | Data, reversible, no restart | Archive the 11 `test-spec-*` specs (`UPDATE scan_specs SET is_archived = true WHERE slug LIKE 'test-spec-%'`, after a backup). The handler reloads active specs on every message, so this takes effect immediately. It also removes ~2,100 duplicate candidates per 3 days (F-H2) | 48 s → **18.6 s** per event. The scanner keeps up (62% duty) and drops to about **7 cores**. Load average falls by about 4 |
| **M2** | Data (new spec version) | Drop `1d` from `scalp-multi-tf`; Q8 says trigger on 15m/1h/4h. This cuts its lookback from 300 days to 50 days | With M1: **15 s** per event, 50% duty, about **6 cores** |
| **M3** | Compose; applies at the next planned recreate of `scanner` | `cpus: "8"` on the scanner, plus `POLARS_MAX_THREADS: "8"` as an explicit backstop. DuckDB 1.5.5 reads cgroup v2 `cpu.max` (verified in the binary), and Polars sizes its pool from Rust's `available_parallelism`, which honours the quota, so both pools shrink without code. Use 8, not 4: at 4 threads, M1+M2 need about 28 s per event (93% duty), which leaves no room to drain the lag | Hard ceiling of 8 cores even if specs are added. Wall time per event lands between the 88-thread and 4-thread figures (the 8-thread point was not measured) |
| **M4** | Code, follow-up item (F-H2 / Phase J) | (a) Compact the lake's day partitions (nightly, or have ingest append to a daily file). (b) Load the 1m frame once per instrument per event and share it across specs. (c) Scan only the instrument whose bar closed. (d) Evaluate only the newly closed bar(s), not every row of the lookback. (d) is what stops old bars becoming candidates (F-H2), both in steady state and after restarts | (a) alone is about 20× less read CPU. With (b)–(d) the scanner should idle at a fraction of one core |

**I would apply M1 now and M2 with it, then M3 at the next planned restart, and schedule M4.**

Before or with M1, the operator must decide what happens to the backlog (1,189 events at 01:52 and growing).
Each backlog event re-runs every spec on **current** lake data, so draining it adds no information. It means about
1,200 redundant runs that are mostly de-duplicated on insert, and it delays real-time scans until it has drained
(at M1 capacity of about 3.2 events/min against 2 arriving, that takes roughly 16 h). **Recommendation: skip the
backlog**, as an operator call:

```bash
# skip: move the scanner group to the newest entry (run inside the scanner container so the Redis password stays in its env)
docker exec cryptonite_scanner python -c "import os,redis; r=redis.Redis.from_url(os.environ['REDIS_URL']); print(r.xgroup_setid('cryptonite:stream:closed_bar_committed','scanner-worker','\$'))"
# rollback: not exact. Note the group's last-delivered-id first (XINFO GROUPS); setting it back with
# xgroup_setid(..., '<that id>') re-queues the skipped events, as long as they have not been trimmed from the stream.
```

M1 rollback: `UPDATE scan_specs SET is_archived = false WHERE slug LIKE 'test-spec-%';`. M2 rollback: re-activate the
previous `scan_spec_versions` row for `scalp-multi-tf`. M3 rollback: remove the two compose lines and recreate the
service.

---

## 4. Operator actions (need sudo or physical access)

Every step below comes with its rollback. Run them one block at a time, and check each block's result before moving on.

### 4.1 Keep the host awake

```bash
# a) Stop idle-suspend in the desktop session (as the desktop user; no sudo)
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
#    rollback:
gsettings reset org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type

# b) Block every sleep path system-wide
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
#    rollback:
sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target

# check: nothing new after this point
journalctl --since today | grep -E "PM: suspend entry|The system will suspend"
```

Step (b) is required, not optional. Step (a) only covers the logged-in desktop user. After a crash reboot the
machine sits at the GDM login screen, and the greeter runs under its own user (`gdm`, uid 121) with its own
gsettings, which still say "suspend after 30 min". Only the mask stops the box from going to sleep at the login
screen while no one is logged in.

### 4.2 Add swap (the plan's "done when")

`/` is **83% full (39 G free)** and the lake keeps growing, so do not use a 16 GB swap file. Option A uses no disk
and is preferred. Use option B only if zram is not wanted.

```bash
# Option A (preferred): zram, compressed swap in RAM, no disk space used
sudo apt install systemd-zram-generator
printf '[zram0]\nzram-size = 16384\ncompression-algorithm = zstd\n' | sudo tee /etc/systemd/zram-generator.conf
sudo systemctl daemon-reload
sudo systemctl start /dev/zram0          # starts dev-zram0.swap; systemd-zram-setup@zram0 alone only formats the device
swapon --show                            # must list /dev/zram0
#    rollback:
sudo systemctl stop dev-zram0.swap && sudo rm /etc/systemd/zram-generator.conf && sudo systemctl daemon-reload
# (optionally: sudo apt purge systemd-zram-generator)

# Option B: 8 GB swap file, only if df shows enough room
df -h /                                  # continue only if "Avail" stays well above 8G (39G at the time of writing)
sudo fallocate -l 8G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
swapon --show                            # must list /swapfile
#    rollback:
sudo swapoff /swapfile && sudo sed -i '\|^/swapfile none swap sw 0 0$|d' /etc/fstab && sudo rm /swapfile

# Either option: prefer RAM, use swap only under pressure
sudo sysctl -w vm.swappiness=10 && echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
#    rollback:
sudo rm /etc/sysctl.d/99-swappiness.conf && sudo sysctl -w vm.swappiness=60
```

### 4.3 Make the next crash leave evidence

```bash
# kdump + rasdaemon (answer yes to enabling kdump; one reboot is needed to reserve crashkernel memory)
sudo apt install kdump-tools rasdaemon
sudo systemctl enable --now rasdaemon
#    rollback (then reboot to free the crashkernel reservation):
sudo systemctl disable --now rasdaemon; sudo apt purge kdump-tools rasdaemon && sudo update-grub

# Panic on lockups so kdump captures them (nmi_watchdog is already 1, so hardlockup_panic works)
printf 'kernel.hardlockup_panic=1\nkernel.softlockup_panic=1\nkernel.panic=30\n' | sudo tee /etc/sysctl.d/99-crash.conf && sudo sysctl --system
# optional, can panic on a spurious NMI:  echo 'kernel.unknown_nmi_panic=1' | sudo tee -a /etc/sysctl.d/99-crash.conf && sudo sysctl --system
#    rollback:
sudo rm /etc/sysctl.d/99-crash.conf && sudo sysctl -w kernel.panic=0 kernel.hardlockup_panic=0 kernel.softlockup_panic=0 kernel.unknown_nmi_panic=0

# Flush the journal to disk more often, so less of the tail before a hang is lost
sudo mkdir -p /etc/systemd/journald.conf.d && printf '[Journal]\nSyncIntervalSec=15s\n' | sudo tee /etc/systemd/journald.conf.d/sync.conf && sudo systemctl restart systemd-journald
#    rollback:
sudo rm /etc/systemd/journald.conf.d/sync.conf && sudo systemctl restart systemd-journald

# after the next crash:
sudo ls /var/crash /sys/fs/pstore; sudo ras-mc-ctl --summary; sudo ras-mc-ctl --errors
```

If the box still dies with nothing in kdump, pstore or rasdaemon, the kernel never got a chance to react. That
points strongly at power or a platform reset (suspect 2) rather than at software.

### 4.4 GPU power cap, persistent across reboots

`nvidia-smi -pl` is lost at every reboot, and so after every crash. A oneshot unit re-applies it at boot.

```bash
sudo tee /etc/systemd/system/nvidia-power-cap.service >/dev/null <<'UNIT'
[Unit]
Description=Cap the RTX 3080 Ti at 250 W while the trading stack shares the host (A1a)
After=systemd-modules-load.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/nvidia-smi -pm 1
ExecStart=/usr/bin/nvidia-smi -pl 250
ExecStop=/usr/bin/nvidia-smi -pl 350

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now nvidia-power-cap.service

# verify ("SW Power Capping" is under PERFORMANCE, not POWER)
nvidia-smi -q -d POWER,PERFORMANCE | grep -E "Current Power Limit|SW Power Capping"   # expect 250.00 W

#    rollback:
sudo systemctl disable --now nvidia-power-cap.service && sudo rm /etc/systemd/system/nvidia-power-cap.service \
  && sudo systemctl daemon-reload && sudo nvidia-smi -pl 350 && sudo nvidia-smi -pm 0
```

A 250 W cap will slow Ollama somewhat, mostly prompt processing, since token generation is mostly limited by memory
bandwidth. Check the debate latency after applying it. If it becomes a problem, 300 W is a middle step.

### 4.5 Hardware checks

The GRUB menu on this host is **hidden** (`GRUB_TIMEOUT_STYLE=hidden`, `GRUB_TIMEOUT=10`). To pick an entry by hand,
press **Esc** during the 10 s window. This applies to memtest86+ and to the older kernel.

- **Read the real entry names first.** The submenu is "Advanced options for Zorin OS", not "…for Ubuntu". Because
  `grub.cfg` is root-only, read it with:
  ```bash
  sudo grep -E "^\s*(submenu|menuentry) " /boot/grub/grub.cfg | cut -c1-160
  ```
  The menu entry IDs do not depend on the distro name. With root UUID `699c00f2-aadb-4233-ace1-f6b6c9235e62` (from
  `/proc/cmdline`), the 7.0.0-34 entry should be
  `gnulinux-advanced-699c00f2-aadb-4233-ace1-f6b6c9235e62>gnulinux-7.0.0-34-generic-advanced-699c00f2-aadb-4233-ace1-f6b6c9235e62`.
  Confirm it against the `$menuentry_id_option` values in the grep output.
- **Kernel A/B with a one-time boot** (safer than changing the default):
  ```bash
  sudo sed -i 's/^GRUB_DEFAULT=0$/GRUB_DEFAULT=saved/' /etc/default/grub && sudo update-grub   # grub-reboot needs "saved"
  sudo grub-reboot 'gnulinux-advanced-699c00f2-aadb-4233-ace1-f6b6c9235e62>gnulinux-7.0.0-34-generic-advanced-699c00f2-aadb-4233-ace1-f6b6c9235e62'
  sudo reboot      # next boot only; check with: uname -r  -> 7.0.0-34-generic
  # to stay on -34 for the multi-day test: sudo grub-set-default '<same entry>'
  #    rollback:
  sudo grub-set-default 0 && sudo sed -i 's/^GRUB_DEFAULT=saved$/GRUB_DEFAULT=0/' /etc/default/grub && sudo update-grub
  ```
- **memtest86+ overnight:** reboot, press Esc, and pick the memtest86+ entry. The host boots EFI and
  `/boot/memtest86+x64.efi` is installed; the exact title is in the grep output above. Let it run at least 4 passes.
  Any error means reseating or replacing that DIMM. Nothing to roll back.
- **PSU:** read the label (Z840: 850 W or 1125 W) and check how the 3080 Ti's two 8-pin inputs are fed (native leads
  or 6-pin adapters, and whether one lead is daisy-chained to both). Consider the 1125 W unit or a lower GPU cap.
- **BIOS event log:** F10 at boot → look for the BIOS / system event log (on Z-series it sits under the Main or
  Advanced menu), and photograph any power, thermal or memory events with timestamps. The Z840 has no BMC/IPMI,
  so `ipmitool sel list` does not apply.
- **BIOS update:** M60 v02.62 is dated 2024-01. Check HP for a newer Z840 release; the ACPI `_PCT`/WMI errors suggest
  firmware gaps. Rollback is HP's BIOS rollback, if the release allows it; read the release notes first.
- **Combined-load test** (only while the trading stack is stopped and with someone present): run
  `stress-ng --cpu 88 --timeout 10m` and a GPU load at the same time, then each alone, with the §4.6 logger running.

### 4.6 Telemetry until resolved (no sudo)

A plain `( … ) &` loop dies when the session ends. Run the loggers as transient user units instead:

```bash
# GPU power once per second (catches sustained draw, not millisecond transients) - about 5 MB/day
systemd-run --user --unit=gpu-power -- nvidia-smi \
  --query-gpu=timestamp,power.draw,power.limit,temperature.gpu,utilization.gpu,clocks_event_reasons.active \
  --format=csv,noheader -l 1 -f "$HOME/gpu-power.csv"

# host summary once per minute
systemd-run --user --unit=host-telemetry -- bash -c 'while true; do date -Is; sensors | grep -E "Package id|Composite"; cat /proc/loadavg; sleep 60; done >> "$HOME/host-telemetry.log" 2>&1'

# status / stop
systemctl --user status gpu-power host-telemetry
systemctl --user stop gpu-power host-telemetry
```

User units stop at logout unless lingering is enabled (`loginctl enable-linger $USER`; undo with
`loginctl disable-linger $USER`). Without lingering, start them with `nohup … &` instead, and stop them with
`pkill -f gpu-power.csv` and `pkill -f host-telemetry.log`.

---

## 5. Done-when status (plan item A1a)

- Ranked suspects written down: **yes** (§2).
- Swap active: **no.** It needs sudo (§4.2: zram preferred, or an 8 GB swap file after a `df` check).
- Root cause: **open.** The next decisive data points are real GPU `power.draw` under load (§4.6), the PSU and
  BIOS log, a week at `-pl 250` with suspend disabled, and kdump/rasdaemon output if another crash happens. If crashes continue after §4.1–4.5,
  go ahead with A1b (move the always-on core off this box).

---

## 6. Memory: lake compaction and bounded DuckDB (2026-10-20)

### 6.1 What happened

On a 62 GiB host with no swap, these services reach the following resident memory within minutes of boot:

| Service | RSS |
|---|---|
| scanner | 15 GB |
| api | 13.7 GB |
| agent_worker | 13.6 GB |
| learner | 5.8 GB |

The host hit a global OOM on 2026-10-20 at 14:24–14:27. The kernel killed the scanner, and systemd-oomd killed Chrome,
the Claude app and dbus. The operator then powered the host off. Benchmark containers from the investigation itself
(5 GB caps, with about 7 GB available) helped trigger it. Lesson: **no benchmark or test container on this host unless
`free -g` shows at least 12 GB available, and always use `--memory` ≤ 2g.**

All four services read the lake through `AnalyticsQueryEngine.query_bars_df`, and every read opens every file it
globs. There are two root causes, measured in 2 GB-capped throwaway containers on a read-only lake (BTCUSDT 1m):

1. **Small files.** 23,424 files hold only 310,767 rows (79.5 MB uncompressed), and about 700 new files arrive
   every day (one per ingest flush).
2. **`SELECT DISTINCT ON (ts_init_ns) * … ORDER BY ts_init_ns, <5 tie-break keys>` cannot spill to disk.** The
   full-history read fails even single-threaded at 1.6 GB.

The fix and its evidence:

- **QUALIFY gives the same pick.** `SELECT * … QUALIFY row_number() OVER (PARTITION BY ts_init_ns ORDER BY <same
  tie-break keys>) = 1 ORDER BY ts_init_ns` picked exactly the same row for all 302,867 bars. `EXCEPT ALL` returned 0
  rows in both directions. 3,921 timestamps have more than one candidate row, and there are 0 ties on the full key.
  On a 12.5-day window the content hash is identical.
- **Compaction is lossless.** Compacting each date partition to one zstd file kept the row count, the distinct
  count and the row hash identical. The schema stayed identical too, including the hive `date` column. File counts
  went from 23,424 to 217 for 1m, and from 465 to 40 for 1h. Compaction peaked at 1.7 GB RSS with DuckDB
  `threads=4` and `memory_limit=1GB`.
- **Read cost before and after:**

  | Read | Today (DISTINCT ON, uncompacted) | QUALIFY, uncompacted | QUALIFY, compacted |
  |---|---|---|---|
  | 12.5-day scanner window | 5.5 s / 4.1 GB (8.2 s / 2.7 GB in the run that measured QUALIFY alone) | 7.6 s / 1.7 GB | **0.14 s / 289 MB** |
  | Full history | fails | not measured | **0.7 s**, fits a 300 MB DuckDB limit |

**Not measured:** the uncompacted lake under `DUCKDB_MEMORY_LIMIT=1GB`. This matters most for the 300-day 1d
HTF read (specs with 4h pull 1d through `HTF_MAP`). QUALIFY can spill, so it should complete, but slowly. If it
fails, the scanner now logs `Skipping <inst> in the scan: the analytics lake read failed (OutOfMemoryException …)`
and carries on. **Run compaction soon after the deploy** (§6.3), so that window stays short.

### 6.2 What changed (PR "lake compaction + bounded DuckDB")

- `query_bars_df` now uses the QUALIFY form above, with the same tie-break and the same output order. When a
  file vanishes between DuckDB's glob and its open (compaction or the repair script moved it), it reads once more
  before returning an empty frame.
- `scripts/compact_lake.py` compacts each partition up to yesterday, counted from one hour ago. Before 01:00 UTC
  that means the day before yesterday, because yesterday can still receive a straggler file just after midnight.
  For each partition with more than one file it:
  - writes a temp copy outside every `date=*` glob;
  - checks the copy's column types, row count, distinct `ts_init_ns` and order-independent row hash against the
    originals;
  - moves the copy in with `os.replace`;
  - **only then moves** the originals to `data/analytics_lake_quarantine/<same path>`, with a `compaction-<stamp>.json`
    manifest. It never deletes anything.

  The script is idempotent: a partition that gains files later (a straggler or a backfill) is recompacted together
  with its compacted file. It holds `<lake>/.lake-maintenance.lock`. `backfill_history.py`, `backfill_gaps.py` and
  `repair_lake_open_stamped_rows.py --apply` take the same lock and exit 75 while another job holds it.
  `backfill_regime_tags.py` only reads the lake, so it does not take the lock.
- The scanner catches `duckdb.Error` per instrument (log and skip), as it already did for `ValueError`. Before
  this, the error escaped before `XACK` and every later spec was skipped.
- `AnalyticsQueryEngine` reads `DUCKDB_THREADS`, `DUCKDB_MEMORY_LIMIT` and `DUCKDB_TEMP_DIR`. When they are unset,
  DuckDB keeps its defaults: all cores, 80% of RAM, and `.tmp` in the working directory, which is not writable for
  api/scanner (uid 1000). Compose sets `8` / `1GB` / `/tmp` and `POLARS_MAX_THREADS=8` for scanner, api, learner and
  agent_worker. There is **no `mem_limit` and no `MALLOC_ARENA_MAX` yet**: §6.4 measures first.
  `SCAN_LOOKBACK_BARS`, `HTF_MAP` and `/api/v1/bars` are unchanged.
- The model-callable DuckDB tool reads only the Nautilus catalog and has its own caps, so it is unaffected.
- `ingest_batches.analytics_paths` is an audit column that no code reads back. After compaction it names files
  that now sit in quarantine.

### 6.3 Nightly maintenance (automatic)

**Correction (2026-10-20).** An earlier version of this section said to run compaction from `repo/` on
`data/analytics_lake`, assuming `deploy-main/data` was a symlink into `repo/`. It is not: the stack has written
`deploy-main/data` since 2026-10-19, and `repo/data` is a stale copy. §7 makes `deploy-main/data` the only data
folder. Never compact `repo/data`.

The `lake_maintenance` compose service (`cryptonite_storage.lake_maintenance`) does the routine work. It starts with
the stack, runs on the data folder next to its compose file, and is capped at 2 GB. Every night at **01:30 UTC** it:

1. **compacts** every closed date partition (`cryptonite_storage.lake_compaction`, each one checked before it is
   swapped in; the originals go to `data/analytics_lake_quarantine/`);
2. **prunes** the quarantine of every compaction older than **14 days**, but only once its rows are proven to be in
   the lake: the compacted file named in the manifest must still match the row count, distinct bars and row hash
   recorded when it was checked against the originals (or, if a later night recompacted the partition, the later
   manifest must pass the same test). Anything unproven is kept;
3. **alerts only on problems**, as a CRITICAL message on `channel:system_alerts`, which the notifier sends to
   Telegram and ntfy: a partition that failed its check, a quarantine entry it could not prove, an error, or the lake
   lock held by a backfill all night (it retries hourly, 6 times).

Its state is in `data/analytics_lake_quarantine/.lake-maintenance-state.json`. The healthcheck turns the container
unhealthy when the last night failed or nothing has succeeded for 36 hours. Settings (compose environment):
`LAKE_MAINTENANCE_AT` (01:30), `LAKE_QUARANTINE_DAYS` (14), `LAKE_LOCK_RETRY_S` (3600), `LAKE_LOCK_RETRIES` (6).

Nothing here needs a person. To look anyway:

```bash
docker logs --since 2d cryptonite_lake_maintenance 2>&1 | grep -E "lake maintenance (ok|problems|failed)|ALERT"
docker inspect -f '{{.State.Health.Status}}' cryptonite_lake_maintenance
docker exec cryptonite_lake_maintenance cat /app/data/analytics_lake_quarantine/.lake-maintenance-state.json
```

**By hand** (a dry run, one symbol, or a run right now), from a checkout on `main`, against the live lake:

```bash
cd ~/Documents/projects/cryptonite/repo
uv run python scripts/compact_lake.py --lake ../deploy-main/data/analytics_lake --dry-run | tail -3
docker exec cryptonite_lake_maintenance python -m cryptonite_storage.lake_maintenance --once
```

Both take the lake maintenance lock (exit 75 while the nightly run, a backfill or the repair holds it).

**Backups.** Quarantine lives under `data/`, so the weekly `cryptonite-backup data` archive carries it until it is
pruned: every compacted original is in at least one weekly archive before it is deleted.

**Rollback.**
- Stop the automation: `docker compose -f infra/docker-compose.yml stop lake_maintenance` (from `deploy-main`).
- One partition: move the files listed in its manifest's `quarantined_as` back into the partition, then move its
  `part-compacted-<stamp>.parquet` into quarantine. Readers de-duplicate per bar, so the order of the two moves does
  not matter.

### 6.4 24 h measurement checklist, then memory limits

Start after the first nightly compaction (§6.3). VmHWM and `memory.peak` count from container
start, so recreate a container first if it predates the change.

```bash
# every 10 min for 24 h: memory and CPU of the lake readers (stop with: kill %1)
nohup bash -c 'while true; do date -Is; docker stats --no-stream --format "{{.Name}} {{.MemUsage}} {{.CPUPerc}}" \
  cryptonite_scanner cryptonite_api cryptonite_learner cryptonite_agent_worker; sleep 600; done' \
  >> ~/lake-readers-24h.log 2>&1 &

# at the end: peak RSS of every process (VmHWM) and the container's own peak (cgroup v2)
for c in cryptonite_scanner cryptonite_api cryptonite_learner cryptonite_agent_worker; do
  echo "== $c"; docker exec $c sh -c 'grep -H VmHWM /proc/[0-9]*/status 2>/dev/null; cat /sys/fs/cgroup/memory.peak'
done

# scanner keeps up: the scanner-worker group's lag stays near 0 (it was 661-1,189 on Oct 5-6)
docker exec cryptonite_scanner python -c "import os,redis; r=redis.Redis.from_url(os.environ['REDIS_URL']); print([(g['name'], g.get('lag'), g['pending']) for g in r.xinfo_groups('cryptonite:stream:closed_bar_committed')])"

# lake read failures and OOM kills in the window
docker logs --since 24h cryptonite_scanner 2>&1 | grep -c "analytics lake read failed"
journalctl -k --since -24h | grep -iE "out of memory|oom-kill|killed process"
free -g
```

Record the results:

| Service | `docker stats` max | `memory.peak` | max VmHWM | Lake read failures | `mem_limit` = 1.5–2× peak |
|---|---|---|---|---|---|
| scanner | | | | | |
| api | | | | — | |
| learner | | | | — | |
| agent_worker | | | | — | |

After 24 h, set `mem_limit` per service at 1.5–2× the measured peak (the larger of `memory.peak` and the `docker
stats` maximum), in a separate change. `MALLOC_ARENA_MAX` stays out until it has been measured the same way.

### 6.5 Follow-ups

- **DuckDB 1.5.5's glob matches dot-files.** `date=*/*.parquet` also returns the writers' in-progress `.tmp.*.parquet`.
  A read that lands on a half-written one fails with `InvalidInputException`. The scanner now skips that
  instrument for that run, but `/api/v1/bars` would return an error. Fix it by globbing `part-*.parquet`, or by
  writing temp files outside the partition, as compaction does.
- **Prune partitions by date.** `query_bars_df` filters on `ts_event_ns` only, so DuckDB still opens every
  partition's footer. A `date >= <start date>` predicate on the hive column would skip old partitions entirely.

---

## 7. One data folder (2026-10-20)

### 7.1 What was wrong

HANDOFF.md said `deploy-main/data` was a symlink into `repo/data`. Since 2026-10-18/19 it has been a real folder:

| | `deploy-main/data` | `repo/data` |
|---|---|---|
| Used by the stack | **yes**: every container that mounts `data/` was created from `deploy-main/infra` | no; last write 2026-10-19 14:26 local |
| Analytics lake | full history, growing | same history to 2026-10-19 (hard links) plus 16 bars written in the last minutes before the switch |
| Nautilus catalog | **only from 2026-10-19 11:27 UTC** | **the only full history**, 2026-03-18 to 2026-10-19 11:26 UTC |

So the live catalog had lost its history, the weekly `data` backup archived the stale `repo/data`, and the launchers
(which ran `repo/infra/docker-compose.yml`) would have mounted `repo/data` into any container they had to create.
Also, `.env.notifier` and `infra/security/*.pem` exist only in `repo/`.

### 7.2 What changed

- **`deploy-main/data` is the one data folder.** The launchers start and stop the stack with the deploy worktree's
  compose file (falling back to `repo/` only when there is no deploy worktree), so every container mounts it.
- **The backup archives it** (`backup.sh data`: `CRYPTONITE_DATA_DIR`, else `deploy-main/data`, else `repo/data`),
  tolerates files that change while it reads, and logs a WARNING when the lake has not changed in two days.
- **`scripts/merge_data_dirs.py`** brings into it everything only `repo/data` holds (catalog history, the 16 lake
  files, a research report): hard links, never overwriting, refusing any catalog bar file that overlaps one already
  there. Its dry run on 2026-10-20: 70,716 files to link, 0 conflicts.

### 7.3 Cut-over (once, about 10 minutes, the stack keeps running)

Each step is safe to repeat. From the host, as `samx`:

```bash
cd ~/Documents/projects/cryptonite
git -C repo pull --ff-only && git -C deploy-main checkout --detach origin/main     # after the PR is merged
cryptonite-backup data          # still the old script: archives repo/data, the only full catalog history
```

1. **Secrets for deploy-main** (links, like its `.env`):
   ```bash
   ln -s ../repo/.env.notifier deploy-main/.env.notifier
   ln -s ../../../repo/infra/security/binance_demo_private_key.pem deploy-main/infra/security/binance_demo_private_key.pem
   ```
2. **Merge the history into the live folder** (dry run, then apply; expect 0 conflicts):
   ```bash
   cd repo && uv run python scripts/merge_data_dirs.py --src ../repo/data --dst ../deploy-main/data && \
     uv run python scripts/merge_data_dirs.py --src ../repo/data --dst ../deploy-main/data --apply; cd ..
   ```
3. **Retire the old folder** (moved, not deleted; the files stay reachable through their hard links in
   `deploy-main/data`), and put back the placeholders git tracks:
   ```bash
   mv repo/data ~/backups/cryptonite/repo-data-retired-$(date +%F) && git -C repo checkout -- data
   ```
4. **New backup script and the maintenance service:**
   ```bash
   install -m 755 repo/scripts/backup.sh ~/.local/bin/cryptonite-backup
   cd deploy-main && set -a && . ./.env && set +a && \
     docker compose -f infra/docker-compose.yml build lake_maintenance && \
     docker compose -f infra/docker-compose.yml up -d --no-deps lake_maintenance
   ```
5. **Check:**
   - `cryptonite-backup data` logs `of …/deploy-main/data` and no WARNING.
   - `ls deploy-main/data/nautilus_catalog/data/bars/BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL | head -1` starts in March.
   - `docker inspect -f '{{.State.Health.Status}}' cryptonite_lake_maintenance` is `healthy` (after about a minute).
   - The launchers print `compose file: …/deploy-main/infra/docker-compose.yml`.

After about a month without surprises, `~/backups/cryptonite/repo-data-retired-*` can be deleted.

**Rollback:** `mv ~/backups/cryptonite/repo-data-retired-<date> repo/data` (after moving the placeholders aside),
reinstall the previous `backup.sh`, `docker compose stop lake_maintenance`. The merged files in `deploy-main/data`
are harmless to keep.

