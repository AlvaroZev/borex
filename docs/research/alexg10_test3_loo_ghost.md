# AlexG10 Test 3

See `C:/Users/azeva/OneDrive/Documentos/work/trading/borex-main/data/runs/research/alexg10_test3_20261004_141920/TEST3_EXECUTIVE_SUMMARY.md` and `TEST3_REPORT.md`.

Root cause of LOO fill inflation: shared AOI/HTF cache cross-symbol pollution (fixed in strategy5_revised.py). Under PRE_FIX, ghost_sl_mult did not change fill counts on the diagnostic grid; the LOO anomaly is cache-driven, not ghost-distance-driven.
