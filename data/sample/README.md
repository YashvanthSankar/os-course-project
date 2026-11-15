# Sample run

One complete raw run (`main/cpu-low-shm-r0`: cpu workload, low load, traced with
shared counters, repetition 0) so the data format can be inspected without the
full data set. `bench.csv.gz` and `tlat.agg.csv.gz` are gzip-compressed; the
other files are as written by the experiment driver.

Column meanings: see the data dictionary in the reproducibility guide (section 6).

## Full raw data

The full raw data (336 runs, about 20 GB) is too large to include here.

| | |
|---|---|
| Archive | `tailtag_raw_data.tar.zst` (4.4 GB, zstd, all 336 runs) |
| SHA-256 | `668d096981e4fab57d59fcc6694d261638905369f4944c9bea45317d415feda5` |
| Download | available from the team on request until the public link is added to the GitHub README |
| Check | `shasum -a 256 tailtag_raw_data.tar.zst` must print the value above |
| Unpack | `zstd -dc tailtag_raw_data.tar.zst \| tar -x` (creates `raw/<experiment>/<run>/`) |
| Use | place it as `code/data/raw/` and run the analysis steps in the reproducibility guide (section 5); no re-running of experiments needed |

The data are the direct output of `scripts/experiment.py`; no preprocessing
was applied. The analysis scripts read them as they are.
