"""Make the suites composable: each test module gets a FRESH database.

The issue #20 suite seeds its own actors/wipes at setup, but leaves them
behind — the next suite's first-actor bootstrap (empty actors collection)
then fails with "bootstrap closed". Drop the database after each module,
mirroring services/chat/tests/conftest.py. Requires TG_TEST_MONGO_URL
(pointing at the same Mongo the mail service uses).
"""
import os

import pytest

@pytest.fixture(scope="module", autouse=True)
def _fresh_db_per_module(request):
    yield
    url = os.environ.get("TG_TEST_MONGO_URL")
    if not url:
        return  # no drop configured; suites must run on their own DB
    import pymongo
    client = pymongo.MongoClient(url, directConnection=True)
    client.drop_database("treegent")
    client.close()
