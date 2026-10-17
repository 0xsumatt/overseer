# Gunicorn serves market analytics and bounded, transient read-only Positions
# requests. Persistent market-data collection remains a separate process.

bind = "127.0.0.1:8000"
workers = 2
# Reserve request capacity for analytics while Positions waits on venue reads.
worker_class = "gthread"
threads = 4

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
