"""PlanSpan sidecar: tail auto_explain JSON log -> plan spans -> SigNoz OTLP.

Wired up in PR4. This is a placeholder entrypoint so the image builds.
"""
import os
import time


def main():
    log_path = os.environ.get("PG_LOG_PATH", "/var/log/postgresql/postgresql-17-main.log")
    otlp = os.environ.get("OTLP_ENDPOINT", "localhost:4317")
    print(f"planspan sidecar up. log={log_path} otlp={otlp}", flush=True)
    # real tail loop lands in PR4
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
