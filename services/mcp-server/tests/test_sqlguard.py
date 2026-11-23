import pytest

from hammond_mcp.sqlguard import SQLRejected, check

VIEWS = frozenset({"states", "entities"})


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("select * from states", "SELECT * FROM states LIMIT 11"),
        ("select * from states limit 5", "SELECT * FROM states LIMIT 5"),
        (
            "select * from states order by last_updated desc limit 100000",
            "SELECT * FROM states ORDER BY last_updated DESC LIMIT 11",
        ),
        (
            "with a as (select * from states) select count(*) from a",
            "WITH a AS (SELECT * FROM states) SELECT COUNT(*) FROM a LIMIT 11",
        ),
        ("from states select entity_id", "SELECT entity_id FROM states LIMIT 11"),
    ],
)
def test_allowed(sql: str, expected: str) -> None:
    assert check(sql, VIEWS, max_rows=10).sql == expected


def test_views_used() -> None:
    q = check("select * from states s join entities e using (entity_id)", VIEWS, max_rows=10)
    assert q.views == {"states", "entities"}


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("select * from read_csv('/etc/passwd')", "Table functions"),
        ("select * from glob('/*')", "Table functions"),
        ("select * from duckdb_settings()", "Table functions"),
        ("select * from 'data.parquet'", "not permitted"),
        ("select * from lake.silver.states", "without a schema"),
        ("select * from main.states", "without a schema"),
        ("select * from statistics", "not permitted"),
        ("select query('select 1')", "query()"),
        ("select getenv('HOME')", "getenv()"),
        ("select current_setting('s3_secret_access_key')", "current_setting()"),
        ("select * from states; select 1", "one statement"),
        ("insert into states values (1)", "Only SELECT"),
        ("delete from states", "Only SELECT"),
        ("pragma version", "Only SELECT"),
        ("set enable_external_access = true", "Only SELECT"),
        ("attach 'x.db' as x", "Only SELECT"),
        ("copy states to 'x.csv'", "Only SELECT"),
        ("summarize states", "Only SELECT"),
        ("call duckdb_secrets()", "Only SELECT"),
        ("select from where", "parse"),
        ("select 1" + " " * 20_001, "longer"),
    ],
)
def test_rejected(sql: str, message: str) -> None:
    with pytest.raises(SQLRejected, match=message):
        check(sql, VIEWS, max_rows=10)
