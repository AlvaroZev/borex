import pandas as pd
from pathlib import Path

p = Path("data/cache/mt5/utc_v2/EURUSDX/1h.parquet")
df = pd.read_parquet(p)
print("cols", list(df.columns))
print("rows", len(df))
print(df.head(2).to_string())
print("---")
print(df.tail(2).to_string())
ts = df["timestamp"] if "timestamp" in df.columns else df.index
print("min", ts.min(), "max", ts.max())
