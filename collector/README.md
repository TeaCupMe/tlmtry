# Tlmtry PC collector

See repository root README for install, systemd, and CLI usage.

```bash
pip install -e ".[dev]"          # includes psycopg for Postgres tests
# or: pip install -e ".[postgres]" for runtime Postgres only

tlmtry-collector run --sim --database /tmp/primary.db
tlmtry-collector run --sim --database 'postgresql://user:pass@127.0.0.1:5432/tlmtry'
pytest -q
```
