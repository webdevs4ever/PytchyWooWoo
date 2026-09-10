# Predictions

Drop your Kalshi or Polymarket positions here, one per line:

    Amon-Ra St. Brown over 70.5 receiving_yards
    Josh Allen over 245.5 passing_yards
    Bijan Robinson under 65.5 rushing_yards

`over` / `under` is optional and defaults to `over`. Blank lines and lines
starting with `#` are ignored. A CSV whose columns are player, direction,
threshold, stat works too.

Grade them:

    python -m sources.predictions predictions/week1.txt

Or upload at `http://127.0.0.1:8765/predictions`.

Contents are gitignored — positions are week-specific.
