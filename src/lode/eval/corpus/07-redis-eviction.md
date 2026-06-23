# Redis eviction policies for our cache

Redis enforces a memory ceiling with the maxmemory setting, and maxmemory-policy
decides what happens when that ceiling is hit. For a pure cache we use
allkeys-lru, which evicts the least-recently-used key regardless of whether it
has a TTL.

The volatile-lru policy only evicts keys that have an expiry set, which is the
right choice when the same Redis instance also holds data that must never be
dropped. Mixing durable and cache data in one instance is the usual reason to
pick a volatile policy, but we avoid that by running separate instances.

The default policy is noeviction, which makes writes fail with an error once
memory is full rather than dropping anything. That default is a trap for a
cache: a full cache that refuses writes looks like an outage. We always set
allkeys-lru explicitly on cache instances.
