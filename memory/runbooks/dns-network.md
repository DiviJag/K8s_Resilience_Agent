---
runbook_id: dns-network
title: DNS and network fault triage
categories: [dns_resolution_failure, network_partition, high_latency, registry_unreachable]
---

## Who is doing the lookup?

The same "no such host" text means different things depending on which process failed:

- The **kubelet**, while pulling an image, cannot resolve the registry - the pod is stuck in
  ImagePullBackOff and the container never starts. That is **registry_unreachable**.
- The **application**, once running, cannot resolve a dependency - the container started, and
  its own logs show the lookup failure. That is **dns_resolution_failure**.

Anchor on the symptom: if the container never started, it was not the application's lookup.

## Resolution failure versus partition

**dns_resolution_failure**: names fail to resolve ("no such host", "NXDOMAIN", "server
misbehaving") while connections by IP would work. Check CoreDNS health and the pod's DNS
policy before suspecting the network.

**network_partition**: names resolve, but connections to the resolved address time out
("i/o timeout", "context deadline exceeded") with no reply. Something between the pods drops
traffic.

## Latency, not failure

**high_latency**: requests succeed but slowly, and the application reports timeouts against
deadlines that would normally be met. There is no hard failure to find in events; the evidence
is in timing.
