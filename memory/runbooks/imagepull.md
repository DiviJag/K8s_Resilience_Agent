---
runbook_id: imagepull
title: ImagePullBackOff triage
categories: [image_tag_not_found, registry_auth_missing, registry_unreachable, registry_rate_limited, no_fault]
---

## Read the events, not the logs

A pod in ImagePullBackOff never started its container, so its logs are empty or only say the
container is "waiting to start". The reason the pull failed is in the pod's Warning events
(reason `Failed`, message `Failed to pull image ...`) and in the waiting message on the
container status. Go there first.

## Telling the four causes apart

The same symptom has four different causes, and the error text distinguishes them:

- `not found`, `manifest unknown`, `NotFound desc` - the registry answered and the tag or
  repository does not exist: **image_tag_not_found**. Credentials, if present, worked.
- `pull access denied`, `authorization failed`, `authentication required`, `401 Unauthorized`,
  `403 Forbidden`, `failed to fetch anonymous token` - the registry answered and refused the
  caller: **registry_auth_missing**. Check whether the pod spec has `imagePullSecrets` at all.
- `no such host`, `dial tcp`, `i/o timeout`, `connection refused` against the registry host -
  the node never reached the registry: **registry_unreachable**.
- `toomanyrequests`, `429 Too Many Requests`, `pull rate limit` - the registry answered and
  throttled anonymous pulls: **registry_rate_limited**.

A private registry that hides whether a repository exists may answer "denied" for a missing
tag too. When the spec has no pull secret and the reply is a denial, treat it as auth first.

## Where the fix belongs

Tag, auth and rate-limit problems are fixed in the workload: correct the image reference, or
add `imagePullSecrets` so pulls are authenticated (which also lifts anonymous rate limits).

An unreachable registry is not fixed by editing the deployment. Node DNS, egress or the
registry itself is broken; repointing the image elsewhere only hides it.

## One failed pull is not an incident

Pulls are retried. A single `Failed` event followed by `Pulled` / `Started`, with the pod now
Running and Ready, is a transient blip that has already resolved: **no_fault**. Check the
current pod state before diagnosing from the alert name.
