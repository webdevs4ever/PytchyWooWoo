# Lineups

Drop your own lineups here as plain text — one player name per line. A CSV whose
first column is the name works too; a header row is ignored.

    python -m sources.comp lineups/week1.txt --platform draftkings

Names are matched against the salary export in `salaries/`, so a player who
isn't in that week's contest pool is reported as unmatched rather than silently
dropped.

Contents are gitignored: lineups are week-specific.
