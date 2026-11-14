#!/bin/bash
# Sequential JVM queue (one Joern JVM at a time), started after the opt-full run.
set -u
cd /workspace/replicate/secpat_interaction_recovery
export PROF_SCRATCH=/tmp/claude-0/-workspace-replicate-secpat-interaction-recovery/2b3e719e-e09c-4879-891f-382be0d995cf/scratchpad/prof
until grep -q " done$" profiling/results/opt-full.log; do sleep 10; done
echo "$(date +%T) queue: opt3 on the 19 timeouts"
python3 profiling/prof_walk.py --run opt3-timeouts --joern-dir profiling/opt3 \
  --ids $(python3 -c "import json;print(' '.join(json.load(open('profiling/jobs/timeouts.json'))))") --budget 300 \
  > profiling/results/opt3-timeouts.log 2>&1
echo "$(date +%T) queue: builds"
python3 profiling/prof_build.py --run build --jfr --xmx 32g --repos \
  JeanExtreme002_Dokku-API:python CI-CMG_geosamples-ingest:java \
  smart-cloud_smart-cloud:java smart-cloud_smart-cloud:java:delombok=types-only \
  shangyankeji_super-dev:python cibseven_cibseven:java camunda_camunda:java langflow-ai_langflow:python \
  > profiling/results/build.log 2>&1
echo "$(date +%T) queue: warm worker"
python3 profiling/warm_worker.py --run warm-opt3 --joern-dir profiling/opt3 \
  --ids $(python3 -c "import json;print(' '.join(json.load(open('profiling/jobs/opt_check.json'))))") --budget 300 \
  > profiling/results/warm-opt3.log 2>&1
echo "$(date +%T) queue: done"
