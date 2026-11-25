#!/usr/bin/env bash
# Download the AgamiAI Indian-Bank-Statements dataset (Apache-2.0, ~1.4 GB, 400 PDFs + JSON)
# into data/agami/. Re-running resumes / skips files already present.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
uvx --from huggingface_hub hf download AgamiAI/Indian-Bank-Statements \
  --repo-type dataset --local-dir "$ROOT/data/agami" "$@"
