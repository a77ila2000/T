# T Family Barcode

The Oracle VM owns both refresh work and cache reads. Vercel serves only the
static frontend, which calls the Oracle API directly and therefore does not
invoke Vercel Functions.

- Primary frontend: `https://preedgaonprime.vercel.app`
- Oracle fallback: `https://168-138-194-2.sslip.io`
- Read API: `oracle/read_api.py`
- Refresh worker: `oracle/worker_tick.py`
- Shared scheduling and scraping logic: `api/barcode_core.py`

Run tests with `python -m pytest -q`.
