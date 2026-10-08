"""Make the chat suites composable: each test module gets a FRESH database.

Both test_api.py and test_org_authz.py bootstrap an empty actors
collection (the `boot` pseudo-actor only works on an empty tree), so
running them against one database in sequence made the second suite fail.
This hook drops the database after each module finishes, restoring the
empty-actors precondition for the next one. Requires TG_TEST_MONGO_URL
(pointing at the same Mongo the chat service uses).
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
