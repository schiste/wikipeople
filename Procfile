web: uvicorn wikipeople.app:app --host 0.0.0.0 --port ${PORT:-8000}
worker: python -m wikipeople.worker
prewarm: python -m wikipeople.prewarm --days 7
backfill: python -m wikipeople.backfill --batches 1
demand: python -m wikipeople.pageviews
recompute: python -m wikipeople.recompute
cleanup: python -m wikipeople.cleanup
optout: python -m wikipeople.optout
display: python -m wikipeople.displaypolicy
standing: python -m wikipeople.standing
