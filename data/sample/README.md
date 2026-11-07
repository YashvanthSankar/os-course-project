# Sample run

One complete raw run (`main/cpu-low-shm-r0`: cpu workload, low load, traced with
shared counters, repetition 0) so the data format can be inspected without the
full data set. `bench.csv.gz` and `tlat.agg.csv.gz` are gzip-compressed; the
other files are as written by the experiment driver.

Column meanings: see the data dictionary in [docs/REPRODUCE.md](../../docs/REPRODUCE.md) §6.

The full raw data (291 + 27 runs, ~16 GB) is too large for GitHub. Download link:
**TODO: add the Drive/Zenodo link here.** Archive `tailtag_raw_data.tar.zst` (4.2 GB, zstd; 318 runs), SHA-256 `a9bc7508e738b23361dcad6a8695022478e2869d33a5e7195ae597f6a18c0044`. Unpack with `zstd -dc tailtag_raw_data.tar.zst | tar -x`.
