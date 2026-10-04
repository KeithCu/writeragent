import sqlite3

conn = sqlite3.connect(":memory:")

conn.execute("CREATE VIRTUAL TABLE passages USING fts5(body, doc_url UNINDEXED)")
conn.execute("INSERT INTO passages(rowid, body, doc_url) VALUES (1, 'hello world', 'a.txt')")
conn.execute("INSERT INTO passages(rowid, body, doc_url) VALUES (2, 'goodbye world', 'b.txt')")
conn.execute("INSERT INTO passages(rowid, body, doc_url) VALUES (3, 'hello again', 'a.txt')")
conn.execute("INSERT INTO passages(rowid, body, doc_url) VALUES (4, 'hello goodbye', 'b.txt')")

conn.execute("CREATE TABLE chunks(chunk_id INTEGER PRIMARY KEY, doc_url TEXT)")
conn.execute("INSERT INTO chunks(chunk_id, doc_url) VALUES (1, 'a.txt')")
conn.execute("INSERT INTO chunks(chunk_id, doc_url) VALUES (2, 'b.txt')")
conn.execute("INSERT INTO chunks(chunk_id, doc_url) VALUES (3, 'a.txt')")
conn.execute("INSERT INTO chunks(chunk_id, doc_url) VALUES (4, 'b.txt')")


print("FTS without filter:")
rows = conn.execute("""
    SELECT p.rowid, p.doc_url, bm25(passages)
    FROM passages p
    WHERE passages MATCH 'hello'
    ORDER BY bm25(passages)
    LIMIT 2
""").fetchall()
print(rows)

print("FTS with chunk doc_url filter:")
rows2 = conn.execute("""
    SELECT p.rowid, c.doc_url, bm25(passages)
    FROM passages p
    JOIN chunks c ON c.chunk_id = p.rowid
    WHERE passages MATCH 'hello'
      AND c.doc_url = 'a.txt'
    ORDER BY bm25(passages)
    LIMIT 1
""").fetchall()
print(rows2)
