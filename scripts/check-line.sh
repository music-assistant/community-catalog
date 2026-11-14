#!/usr/bin/env bash
# Runs the checks of one server line in its server image: two containers per version of the
# plan, and of each container only the results file it was asked for is kept.
#
#   scripts/check-line.sh <catalog> <line> <image> <results>
#
# <catalog> holds the registry and the work folder with plan.json and the archives; it is
# mounted read-only. A results file lands in <results>/<line>/<domain>-<version>.json; a
# version without one has no result tonight.
set -euo pipefail

catalog=$(cd "$1" && pwd)
line=$2
image=$3
results=$4
# a results file holds a reason of one line and at most 50 kB of output
max_results_size=200000
# the install stage resolves and downloads the requirements; the load stage runs its own
# timeout of 300 seconds on the import
install_timeout=900
load_timeout=420

# run <stage> <seconds> <network> <scratch mode> <domain> <version> <folder>
run() {
  local stage=$1 seconds=$2 network=$3 mode=$4 domain=$5 version=$6 folder=$7
  local name="check-$line-$domain-$version-$stage" status=0
  timeout --kill-after=30 "$seconds" docker run --rm --name "$name" --pull never \
    --platform linux/amd64 --network "$network" --user "$(id -u):$(id -g)" \
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 \
    --memory 4g --memory-swap 4g --env HOME=/tmp \
    --volume "$catalog:/catalog:ro" --volume "$folder/scratch:/scratch:$mode" \
    --volume "$folder/$stage:/results" --workdir /catalog --entrypoint python "$image" \
    -m registry --root /catalog check --line "$line" --domain "$domain" \
    --version "$version" --stage "$stage" --scratch /scratch --output /results/result.json \
    </dev/null || status=$?
  # a container that outlived its time is ended before its folders are read
  docker rm --force "$name" >/dev/null 2>&1 || true
  return "$status"
}

# keep <file> <target>: copies the file when it is a regular file below the size limit
keep() {
  if [ -n "$(find "$1" -maxdepth 0 -type f -size "-${max_results_size}c" 2>/dev/null)" ]; then
    cp "$1" "$2"
  else
    echo "$(basename "$2"): no result, the container left no results file that can be read"
  fi
}

plan=$(jq -r --arg line "$line" '.lines[$line] // [] | .[] | "\(.domain) \(.version)"' \
  "$catalog/work/plan.json")
mkdir -p "$results/$line"
docker pull --quiet --platform linux/amd64 "$image"
while read -r domain version; do
  [ -n "$domain" ] || continue
  folder=$(mktemp -d)
  mkdir "$folder/scratch" "$folder/install" "$folder/load"
  target="$results/$line/$domain-$version.json"
  status=0
  run install "$install_timeout" bridge rw "$domain" "$version" "$folder" || status=$?
  if [ -e "$folder/install/result.json" ] || [ -L "$folder/install/result.json" ]; then
    keep "$folder/install/result.json" "$target"
  elif [ "$status" -eq 0 ]; then
    run load "$load_timeout" none ro "$domain" "$version" "$folder" || true
    keep "$folder/load/result.json" "$target"
  else
    echo "$domain-$version.json: no result, the check could not run"
  fi
  # what the containers left behind may hold folders without permissions
  find "$folder" -type d -exec chmod u+rwx {} + 2>/dev/null || true
  rm -rf "$folder" || true
done <<<"$plan"
