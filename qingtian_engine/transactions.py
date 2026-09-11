"""Transaction scopes that leave commit ownership with the connection provider."""
from contextlib import contextmanager
import uuid


@contextmanager
def transaction_scope(connection, *, write=False):
    """Start a snapshot, or join the caller's existing transaction.

    The surrounding ``Database.connect`` owns commit/rollback. A borrowed
    connection (for example the worker's _TransactionDatabase) is never committed
    here. Nested write operations use a savepoint so their partial writes are
    undone even when the caller handles their exception and keeps working.
    Read scopes simply reuse the caller's snapshot and uncommitted writes.
    """
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield connection
        return
    if not write:
        yield connection
        return

    savepoint = "qingtian_scope_" + uuid.uuid4().hex
    connection.execute("SAVEPOINT " + savepoint)
    try:
        yield connection
    except BaseException:
        # RAISE(ROLLBACK) may already have aborted the entire transaction.
        # Do not mask that original failure with a nonexistent-savepoint error.
        if connection.in_transaction:
            connection.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            connection.execute("RELEASE SAVEPOINT " + savepoint)
        raise
    else:
        connection.execute("RELEASE SAVEPOINT " + savepoint)
