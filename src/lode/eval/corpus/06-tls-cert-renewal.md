# How our TLS certificates get renewed

Our public certificates come from Let's Encrypt and are issued for 90 days. A
cert-manager controller in the cluster requests renewal automatically once a
certificate is within 30 days of expiry, using the ACME DNS-01 challenge against
our Route 53 zone.

DNS-01 proves control of the domain by creating a TXT record, which is why it
works for wildcard certificates where HTTP-01 does not. The trade-off is that
cert-manager needs an IAM role allowed to write the _acme-challenge records in
the hosted zone.

If renewal fails, the most common cause is that the IAM permissions drifted or
the hosted-zone ID in the Issuer is stale. The pager alert fires at 14 days
remaining, which leaves two full renewal attempts before anything user-facing
breaks.
