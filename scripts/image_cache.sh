#!/usr/bin/env bash
# Helpers for caching heal.py's Docker images between workflow runs (each run starts on an empty runner).
#
#   image_cache.sh key            print a cache key from everything that decides what the images contain
#   image_cache.sh save <file>    docker save every self-heal-setup:* image into <file> (gzip); prints saved=true|false
#                                 (HEAL_IMAGE_FILTER narrows which images, mainly for tests)
#   image_cache.sh load <file>    docker load <file>
#
# heal.py names its images self-heal-setup:<hash of base image, platform, setup command, requirements files>.
# The key covers the same inputs (plus the image-building code), so a stale cache is at worst a rebuild.
set -euo pipefail

ACTION_PATH="${ACTION_PATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WORKSPACE="${WORKSPACE:-.}"

case "${1:-}" in
  key)
    {
      printf 'image=%s\0setup=%s\0' "${HEAL_IMAGE:-}" "${HEAL_SETUP_CMD:-}"
      find "$WORKSPACE" -maxdepth 2 -name 'requirements*.txt' -not -path '*/.git/*' | sort | while read -r f; do
        printf 'file=%s\0' "$f"; cat "$f"
      done
      cat "$ACTION_PATH/core/docker_runner.py"   # changes to how images are built must not reuse old ones
    } | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-20
    ;;
  save)
    out="${2:?usage: image_cache.sh save <file>}"
    images="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E "${HEAL_IMAGE_FILTER:-^self-heal-setup:}" || true)"
    if [ -z "$images" ]; then
      echo "No self-heal-setup images to cache."
      echo "saved=false"
    else
      mkdir -p "$(dirname "$out")"
      # shellcheck disable=SC2086
      docker save $images | gzip > "$out"
      echo "Cached $(echo "$images" | wc -l | tr -d ' ') image(s), $(du -h "$out" | cut -f1)."
      echo "saved=true"
    fi
    ;;
  load)
    in="${2:?usage: image_cache.sh load <file>}"
    gunzip -c "$in" | docker load
    ;;
  *)
    echo "usage: image_cache.sh key | save <file> | load <file>" >&2
    exit 2
    ;;
esac
