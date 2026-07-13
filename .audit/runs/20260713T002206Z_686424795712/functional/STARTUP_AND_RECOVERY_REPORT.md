# Startup and recovery report

- Standard turbo cold start reached readiness in about 376 seconds.
- Mono-perf became ready after roughly 3.5 minutes but failed model-quality probes.
- Mono required extended two-stage autotuning and crossed the bounded 20-minute deadline before later becoming healthy.
- Three warm turbo starts returned nonzero due to the API executive-state lease.
- Unknown profile failed quickly with the valid profile list.
- Owned occupied-port and interrupted-start fixtures preserved the owned listener/process boundary and standard cleanup closed ports 3000/8000/8001/8765.
- Final post-fault turbo start, smoke, stop, and inventory passed.
- Docker Desktop was started only for the campaign, then stopped; the Docker engine became unavailable again and the `docker-desktop` WSL distribution returned to `Stopped`, matching the pre-campaign offline baseline.
