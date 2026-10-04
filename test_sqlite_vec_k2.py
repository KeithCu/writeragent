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

# with k limit and filter applied AFTER retrieval
q = np.array([1, 0, 0], dtype=np.float32).tobytes()
print("With k limit:")
rows = conn.execute("""
    SELECT v.rowid, v.distance, c.doc_url
    FROM vec_test v
    JOIN chunks c ON c.chunk_id = v.rowid
    WHERE v.embedding MATCH ?
      AND k = ?
    ORDER BY v.distance
""", (q, 1)).fetchall()

filtered_rows = [r for r in rows if r[2] == 'a.txt']

print("Filter applied after limit:", filtered_rows)


# with filter applied BEFORE/DURING retrieval
print("Filter applied during limit:")
try:
    rows2 = conn.execute("""
        SELECT v.rowid, v.distance, c.doc_url
        FROM vec_test v
        JOIN chunks c ON c.chunk_id = v.rowid
        WHERE v.embedding MATCH ?
          AND k = ?
          AND c.doc_url = 'a.txt'
        ORDER BY v.distance
    """, (q, 1)).fetchall()
    print("Filter applied during limit:", rows2)
except Exception as e:
    print("Error:", e)
