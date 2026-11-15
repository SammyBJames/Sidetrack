import gc

bind = "localhost:23847"
preload_app = True
# More than one thread makes gunicorn use the gthread worker, so a slow request
# (a search waiting on a provider) doesn't block the other requests.
threads = 4
timeout = 200
# A worker restarts only after its in-flight requests finish, which stalls every
# request behind the slowest one. An All search is up to ~10 requests, so this
# keeps restarts about as rare per search as 500 was for one request.
max_requests = 5000
max_requests_jitter = 10

accesslog = "-"
errorlog = "-"


def pre_fork(server, worker):  # noqa: ARG001
    """Freeze preloaded objects so the forked worker keeps sharing their memory."""
    gc.freeze()
