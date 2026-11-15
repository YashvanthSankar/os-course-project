# Dataset

| | |
|---|---|
| Name | `tailtag_raw_data.tar.zst` (TailTag raw experiment data) |
| Size | 4.4 GB compressed (4,415,103,004 bytes); about 20 GB unpacked; 336 runs |
| Source | our own experiments, produced by `code/scripts/experiment.py` (no external dataset) |
| Download | Google Drive: https://drive.google.com/drive/folders/1IVK3r3uroNqU_zwaOjeYwiymoN8KJPTw?usp=share_link (stored as 4 parts, `.part1` to `.part4`, for faster transfer; rejoin with `cat tailtag_raw_data.tar.zst.part1 tailtag_raw_data.tar.zst.part2 tailtag_raw_data.tar.zst.part3 tailtag_raw_data.tar.zst.part4 > tailtag_raw_data.tar.zst`, or see `README_how_to_rejoin.txt` in the folder) |
| Checksum | SHA-256 `668d096981e4fab57d59fcc6694d261638905369f4944c9bea45317d415feda5` |
| Use | see section 4 below |

The data in this project is not downloaded from an external source. Every
data point is produced by our own experiment driver (`code/scripts/experiment.py`)
on the test machine described in the reproducibility guide. The full raw
data of all 336 runs is about 20 GB (4.4 GB compressed), too large for the
submission archive. It is on Google Drive (link above); the four ways to
work with the data are described below.

## 1. Sample run (included here)

One complete raw run, `main/cpu-low-shm-r0` (cpu workload, low load, traced
with shared counters, repetition 0), so the data format can be inspected
directly. `bench.csv.gz` and `tlat.agg.csv.gz` are gzip-compressed; the
other files are exactly as written by the experiment driver. Column
meanings are in the data dictionary (reproducibility guide, section 6).

## 2. All derived results (included)

`supplementary/experimental_results/` holds every table the analysis
produced from the raw data: per-run latency, overhead, intervention and
detection results (`*_runs.csv`, `*_summary.csv`, `detection.csv`) and
`results.json`. Every number and figure in the report can be checked
against these files without the raw data.

## 3. Regenerate the raw data

The dataset is fully regenerable from the code. Arrival times use fixed
seeds and all workload parameters are in `code/configs/workloads.json`.
On Ubuntu 24.04 with root (see the reproducibility guide for setup):

```bash
cd code
bash scripts/setup_vm.sh && make
sudo make test                              # 1-minute check first
sudo NOISE_LIMIT=5 bash scripts/run_all.sh  # all 336 runs + analysis, about 3.5 hours, about 20 GB
```

Single experiment sets can also be generated on their own, for example
`sudo python3 scripts/experiment.py main` (90 runs). The available sets
are `calibrate`, `main`, `overhead`, `interventions`, `abtest`, `netrcv`,
`fixval`, `fixval2` and `netfresh`. Runs are written to `code/data/raw/<set>/<run>/`;
finished runs are skipped, so an interrupted campaign continues where it
stopped.

Measured latencies depend on the machine and on host noise, so a rerun
reproduces the results statistically (medians over five runs, confidence
intervals), not byte for byte.

## 4. Full archive (Google Drive)

| | |
|---|---|
| Archive | `tailtag_raw_data.tar.zst` (4.4 GB, zstd, all 336 runs) |
| SHA-256 | `668d096981e4fab57d59fcc6694d261638905369f4944c9bea45317d415feda5` |
| Download | https://drive.google.com/drive/folders/1IVK3r3uroNqU_zwaOjeYwiymoN8KJPTw?usp=share_link (download the 4 parts and rejoin them as described in the table at the top; the archive is also on the pen drive we bring to the presentation) |
| Check | `shasum -a 256 tailtag_raw_data.tar.zst` must print the value above |
| Unpack | `zstd -dc tailtag_raw_data.tar.zst \| tar -x` (creates `raw/<set>/<run>/`) |
| Use | place it as `code/data/raw/` and run only the analysis steps of the reproducibility guide (section 5); no experiments need to be rerun |

No preprocessing is applied: the analysis scripts read the raw files as
written.
