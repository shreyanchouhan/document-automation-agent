#!/usr/bin/env sh
set -eu

# The demo corpus stands in for the landing zone a real deployment mounts.
if [ ! -f "${DOCAGENT_CORPUS_DIR}/manifest.jsonl" ]; then
  echo "generating corpus into ${DOCAGENT_CORPUS_DIR}"
  python data/generate_corpus.py --corpus "${DOCAGENT_CORPUS_DIR}" --count "${DOCAGENT_CORPUS_SIZE:-200}"
fi

case "${1:-api}" in
  api)
    exec uvicorn docagent.api:app --host 0.0.0.0 --port 8080 --workers "${DOCAGENT_WORKERS:-2}"
    ;;
  console)
    exec streamlit run console/app.py --server.port 8501 --server.address 0.0.0.0
    ;;
  ingest)
    shift
    exec python jobs/databricks_ingest.py "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
