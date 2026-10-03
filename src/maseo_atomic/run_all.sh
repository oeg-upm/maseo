#!/usr/bin/env bash
# Run the MASEO atomic pipeline for several domains, one after another.
#
#   ./run_all.sh                 every domain with a dataset/<domain>_cq2onto_cqs.json
#   ./run_all.sh wine swo        only these domains
#   CONFIG=my.yaml ./run_all.sh  with another config file (default: config.yaml)
#
# A failed domain does not stop the others; the summary at the end lists it.

cd "$(dirname "$0")" || exit 1
PY="${PYTHON:-python}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
CONFIG="${CONFIG:-config.yaml}"

if [ "$#" -gt 0 ]; then
    DOMAINS="$*"
else
    DOMAINS=$(ls dataset/*_cq2onto_cqs.json 2>/dev/null \
              | sed 's#^dataset/##; s#_cq2onto_cqs\.json$##')
fi
[ -n "$DOMAINS" ] || { echo "No domains: dataset/ has no *_cq2onto_cqs.json"; exit 1; }

failed=""
for d in $DOMAINS; do
    echo "=== $d"
    "$PY" -u agent_graph.py "$d" --config "$CONFIG" || failed="$failed $d"
done

echo
if [ -n "$failed" ]; then
    echo "Finished with errors in:$failed"
    exit 1
fi
echo "Finished: $(echo $DOMAINS | wc -w) domain(s)"
