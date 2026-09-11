"""gunicorn configuration.  Run:  gunicorn -c deploy/gunicorn.conf.py app:app"""
import multiprocessing, os

bind = os.getenv("GUNICORN_BIND", "127.0.0.1:5000")
# Rate limits and OTP state live in MySQL, so multiple workers are safe.
workers = int(os.getenv("GUNICORN_WORKERS", min(8, multiprocessing.cpu_count() * 2 + 1)))
threads = int(os.getenv("GUNICORN_THREADS", "2"))
timeout = 120          # ZIP exports over a large archive can be slow
graceful_timeout = 30
keepalive = 5
max_requests = 1000    # recycle workers to bound any slow leak
max_requests_jitter = 100
accesslog = os.getenv("GUNICORN_ACCESS_LOG", "logs/access.log")
errorlog = os.getenv("GUNICORN_ERROR_LOG", "logs/error.log")
loglevel = "info"
# Keep DB_POOL_SIZE small: each worker builds its own pool, so the total
# connection count is workers x DB_POOL_SIZE. Check against MySQL's
# max_connections before raising either.
