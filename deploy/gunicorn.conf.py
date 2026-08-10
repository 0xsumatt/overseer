# Gunicorn owns only the read-only Flask dashboard. The market-data scheduler
# remains a separate process and must never be started inside a web worker.

bind = "127.0.0.1:8000"
workers = 2
worker_class = "sync"

# Fail a genuinely wedged request, but leave enough room for a cold database
# query. SIGTERM/SIGQUIT then gives in-flight requests the same grace period.
timeout = 30
graceful_timeout = 30

# Nginx is the only expected peer. Trust its scheme header, not headers sent
# directly by arbitrary network clients.
forwarded_allow_ips = "127.0.0.1"

# stdout/stderr are captured by systemd and available through journalctl.
accesslog = "-"
errorlog = "-"
capture_output = True
proc_name = "overseer-web"
