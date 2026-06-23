# Idempotency keys for payment requests

A POST that charges a card is not naturally idempotent: a client retry after a
network blip can charge the customer twice. We solve this with an
Idempotency-Key header, a client-generated unique value that the client reuses
on every retry of the same logical request.

The server stores the key with the result of the first successful request. When
a request arrives with a key it has seen, the server returns the stored result
instead of charging again. Keys are retained for 24 hours, which comfortably
covers any client retry window.

A subtle requirement is that the key must be bound to the request body. If a
client reuses a key with a different amount, that is a client bug, and the server
returns a 422 rather than silently charging or replaying the wrong amount.
