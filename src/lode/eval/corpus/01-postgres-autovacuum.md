# Postgres autovacuum and dead tuples

Postgres uses MVCC, so an UPDATE does not overwrite a row in place: it writes a
new row version and marks the old one as a dead tuple. Autovacuum reclaims the
space those dead tuples occupy and updates the visibility map so index-only
scans stay fast.

Autovacuum triggers when the number of dead tuples exceeds
autovacuum_vacuum_threshold plus autovacuum_vacuum_scale_factor times the table
size. The default scale factor is 0.2, which means a large table is vacuumed
only after 20 percent of its rows are dead. On a big, write-heavy table that is
far too late, so we lower the scale factor per table.

A separate hazard is transaction-ID wraparound. If the oldest unfrozen
transaction approaches two billion, Postgres forces an aggressive
anti-wraparound vacuum and can refuse new writes to protect the data. Monitoring
age(datfrozenxid) is the early warning for that.
