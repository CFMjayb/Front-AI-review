"""Test configuration. Force the SQLite ledger backend so the suite is fully
local and never touches a real Firestore database, regardless of the ambient
LEDGER_BACKEND (e.g. if it's set to 'firestore' in .env for real use)."""
import os

os.environ["LEDGER_BACKEND"] = "sqlite"
