# Kubernetes liveness vs readiness probes

A readiness probe decides whether a pod should receive traffic. When it fails,
the pod is removed from the Service endpoints but is not restarted. Use it for
transient unreadiness, such as a slow cache warm-up or a dependency that is
briefly unavailable.

A liveness probe decides whether a pod should be restarted. When it fails past
its threshold, the kubelet kills and restarts the container. Use it only for
unrecoverable states like a deadlock, because an aggressive liveness probe turns
a slow dependency into a restart loop.

A common mistake is pointing a liveness probe at an endpoint that checks
downstream dependencies. If the database is down, every pod fails liveness and
restarts in a storm, which makes the outage worse rather than better. Liveness
should check only the process itself; readiness can check dependencies.
