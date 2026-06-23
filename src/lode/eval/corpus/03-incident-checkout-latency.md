# Postmortem: checkout latency spike on 2026-03-14

On 2026-03-14 the checkout service p99 latency rose from 180ms to 4.2 seconds
for about 40 minutes. The trigger was a deploy that added a synchronous call to
the fraud-scoring service inside the request path, with no timeout configured.

When the fraud service degraded, checkout requests piled up waiting on it.
Because the HTTP client had no timeout, threads were held until the upstream
eventually closed the connection, exhausting the checkout thread pool. The
fallback to "allow the order and score it asynchronously" existed in the code
but was gated behind a feature flag that was off in production.

The fix was three parts: set a 250ms timeout on the fraud call, turn on the
async-scoring fallback flag, and add a circuit breaker that trips after five
consecutive timeouts. The action item is to require an explicit timeout on every
outbound HTTP client at lint time.
