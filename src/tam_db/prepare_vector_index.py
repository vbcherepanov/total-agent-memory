from __future__ import annotations

import argparse
import os
from tam_db.contracts import EXTENSIONS_SCHEMA, is_workspace_schema
from tam_db.pg_schema import ensure_hnsw_index

CONNECT_TIMEOUT_SECONDS = 10


def main() -> None:
    parser = argparse.ArgumentParser(description='Prepare a workspace HNSW index without blocking writers')
    parser.add_argument('--dsn-env', required=True)
    parser.add_argument('--schema', required=True)
    parser.add_argument('--dimension', required=True, type=int)
    args = parser.parse_args()
    if not is_workspace_schema(args.schema):
        parser.error('Expected a TAM workspace schema')
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error('The selected DSN environment variable is empty')
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS) as connection:
        connection.execute(sql.SQL('SET search_path TO {}, {}, pg_catalog').format(
            sql.Identifier(args.schema), sql.Identifier(EXTENSIONS_SCHEMA)))
        ensure_hnsw_index(connection, args.dimension)


if __name__ == '__main__':
    main()
