import sqlite3
from plugin.embeddings.venv.embeddings_sqlite import _load_vec_extension
import numpy as np

conn = sqlite3.connect(":memory:")
_load_vec_extension(conn)

conn.execute("CREATE VIRTUAL TABLE vec_test USING vec0(embedding float[3])")
conn.execute("INSERT INTO vec_test(rowid, embedding) VALUES (1, ?)", (np.array([1, 0, 0], dtype=np.float32).tobytes(),))
conn.execute("INSERT INTO vec_test(rowid, embedding) VALUES (2, ?)", (np.array([0, 1, 0], dtype=np.float32).tobytes(),))
conn.execute("INSERT INTO vec_test(rowid, embedding) VALUES (3, ?)", (np.array([0, 0, 1], dtype=np.float32).tobytes(),))
conn.execute("INSERT INTO vec_test(rowid, embedding) VALUES (4, ?)", (np.array([1, 0, 0], dtype=np.float32).tobytes(),))

conn.execute("CREATE TABLE chunks(chunk_id INTEGER PRIMARY KEY, doc_url TEXT)")
conn.execute("INSERT INTO chunks(chunk_id, doc_url) VALUES (1, 'a.txt')")
conn.execute("INSERT INTO chunks(chunk_id, doc_url) VALUES (2, 'b.txt')")
conn.execute("INSERT INTO chunks(chunk_id, doc_url) VALUES (3, 'a.txt')")
conn.execute("INSERT INTO chunks(chunk_id, doc_url) VALUES (4, 'b.txt')")

q = np.array([1, 0, 0], dtype=np.float32).tobytes()

print("Subquery approach:")
rows3 = conn.execute("""
    SELECT v.rowid, v.distance, c.doc_url
    FROM chunks c
    JOIN vec_test v ON v.rowid = c.chunk_id
    WHERE v.embedding MATCH ?
      AND k = ?
      AND c.doc_url = 'a.txt'
    ORDER BY v.distance
""", (q, 10)).fetchall()
print(rows3)


print("Using ROWID IN approach:")
rows4 = conn.execute("""
    SELECT v.rowid, v.distance
    FROM vec_test v
    WHERE v.embedding MATCH ?
      AND k = ?
      AND v.rowid IN (SELECT chunk_id FROM chunks WHERE doc_url = 'a.txt')
    ORDER BY v.distance
""", (q, 10)).fetchall()
print(rows4)
