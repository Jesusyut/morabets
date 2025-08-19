web: gunicorn app:app -k uvicorn.workers.UvicornWorker -w 3 --timeout 60 --keep-alive 15
worker: python worker.py
