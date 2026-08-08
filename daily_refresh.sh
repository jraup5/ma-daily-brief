#!/bin/bash
#
# Daily auto-refresh wrapper for the M&A dashboard pipeline.
# Fetches new deals, regenerates index.html, and pushes to GitHub --
# but only commits/pushes when the pipeline run actually succeeded.

export PATH="/opt/miniconda3/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO_DIR="/Users/jackraup/Downloads/M&AFiles"
PYTHON="/opt/miniconda3/bin/python3.13"
LOG_FILE="$REPO_DIR/refresh.log"

cd "$REPO_DIR" || {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] FATAL: could not cd into $REPO_DIR, aborting" >&2
    exit 1
}

# From this point on, our own log lines AND everything the pipeline/git
# print to stdout+stderr all land in refresh.log.
exec >> "$LOG_FILE" 2>&1

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

log "=== daily refresh starting ==="

log "running pipeline: $PYTHON ma_news_digest.py"
"$PYTHON" ma_news_digest.py
PIPELINE_EXIT=$?

if [ "$PIPELINE_EXIT" -ne 0 ]; then
    log "FAILED: pipeline exited with code $PIPELINE_EXIT -- not committing or pushing"
    exit 1
fi

log "pipeline succeeded"

git add -A

if git diff --cached --quiet; then
    log "no changes to commit -- clean exit"
    exit 0
fi

log "changes detected, committing"
git commit -m "Daily auto-refresh: $(date +%F)"
COMMIT_EXIT=$?

if [ "$COMMIT_EXIT" -ne 0 ]; then
    log "FAILED: git commit exited with code $COMMIT_EXIT -- not pushing"
    exit 1
fi

log "pushing to origin/main"
git push
PUSH_EXIT=$?

if [ "$PUSH_EXIT" -ne 0 ]; then
    log "FAILED: git push exited with code $PUSH_EXIT"
    exit 1
fi

log "=== daily refresh complete: pipeline ran, committed, and pushed successfully ==="
exit 0
