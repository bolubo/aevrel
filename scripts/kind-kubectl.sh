#!/bin/sh
set -eu

exec docker exec -i "${KIND_CLUSTER:-sf-handoff-cpu}-control-plane" kubectl "$@"
