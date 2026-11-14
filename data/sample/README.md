# Sample run

One complete raw run (`main/cpu-low-shm-r0`: cpu workload, low load, traced with
shared counters, repetition 0) so the data format can be inspected without the
full data set. `bench.csv.gz` and `tlat.agg.csv.gz` are gzip-compressed; the
other files are as written by the experiment driver.

Column meanings: see the data dictionary in [docs/REPRODUCE.md](../../docs/REPRODUCE.md) §6.

The full raw data (336 runs, ~20 GB) is too large for GitHub. Download link:
**TODO: add the Drive/Zenodo link here.** Archive `tailtag_raw_data.tar.zst` (4.4 GB, zstd; 336 runs), SHA-256 `668d096981e4fab57d59fcc6694d261638905369f4944c9bea45317d415feda5`. Unpack with `zstd -dc tailtag_raw_data.tar.zst | tar -x`.
