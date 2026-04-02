# Engineering Design: Lightweight Vector Search in LibreOffice

**Author**: Antigravity AI
**Date**: April 2, 2026
**Target Audience**: Python Experts / Vector Novices

---

## 1. Abstract
When building a "Chat with Document" feature for LibreOffice, the core challenge is **Retrieval**: how do we find the *relevant* 200 words in a 200-page document to send to the LLM?

Traditional keyword search (CTRL+F / BM25) fails when the user's vocabulary differs from the document's. This document outlines a cross-platform, dependency-light strategy for **Vector Similarity Search** using the standard Python library and a ~1MB SQLite extension (`sqlite-vec`).

## 2. The Vector Search Primitive (for Pythonistas)

### 2.1 What is an Embedding?
Think of an LLM Embedding as a "Meaning Signature." It is a fixed-length list of floating-point numbers (e.g., 1536 floats) that represents a chunk of text in a multi-dimensional space.

In this space:
- "The dog is barky" and "Canine vocalization" are **close together**.
- "The dog is barky" and "Pythons are interpreted languages" are **very far apart**.

### 2.2 The Math of "Closeness"
To find if two sentences are similar, we don't compare words; we compare **angles**. If the 1536-dimensional vectors are pointing in roughly the same direction, the sentences have similar meanings.

- **Dot Product**: Multiply the values at each index and sum them up. 
- **Cosine Similarity**: The dot product of two **normalized** vectors (vectors with a length of 1.0).

> [!NOTE]
> **Optimization Trick**: If we normalize our vectors *once* when we receive them from the LLM, the expensive "Cosine Similarity" formula simplifies to a lightning-fast "Dot Product."

## 3. The Deployment Dilemma: The "NumPy Tax"

Usually, Python developers reach for `numpy` for vector math. However, for a LibreOffice extension (`.oxt`), NumPy carries a heavy "tax":
- **Binary Size**: ~50–100MB per platform.
- **Complexity**: Packaging NumPy for Windows, macOS (Intel + Silicon), and Linux (x86 + ARM) inside a single extension is a maintenance nightmare.

**The Solution**: We don't need a general-purpose linear algebra library. We need a specialized **Vector Database Engine**.

## 4. The `sqlite-vec` Breakthrough

`sqlite-vec` is a modern, lightweight C-extension for SQLite. It is the spiritual successor to the older `sqlite-vss`. 

### 4.1 Key Features:
- **Tiny Footprint**: ~1MB per OS binary.
- **Native SQL Syntax**: You search vectors using `SELECT` statements.
- **Specialized Storage**: It stores vectors in a compact, bit-packed format in `BLOB` columns.
- **Support for Hybrid Search**: It is compatible with SQLite's FTS5 (Full Text Search) for combining keyword and vector results.

### 4.2 Why this is a "Porsche" for extensions:
Unlike NumPy, which runs in the Python interpreter's loop, `sqlite-vec` performs the vector math in **optimized C loops with SIMD (Single Instruction, Multiple Data) acceleration** directly inside the database engine. It can scan 10,000 vectors in a fraction of a millisecond.

### 4.3 The Generation Gap: Search vs. Inference
It is critical to note that **`sqlite-vec` does not create vectors**. 

In the AI pipeline, there are two distinct steps:
1.  **Inference (Generation)**: An LLM or Embedding Model takes a sentence (String) and converts it into a list of floats (Vector).
2.  **Indexing (Search)**: A Vector Database (like `sqlite-vec`) takes those floats and performs high-speed comparisons.

The `sqlite-vec` extension assumes you are providing the floats. For a lightweight LibreOffice extension, the most efficient path is calling a remote LLM API (Gemini, OpenAI) for the inference step, then using `sqlite-vec` for the local indexing step.

## 5. How it works in Python

You don't need a new library. You use the built-in `sqlite3` module:

```python
import sqlite3

# 1. Connect to the database
conn = sqlite3.connect("document_vectors.db")

# 2. Load the lightweight extension
conn.enable_load_extension(True)
conn.load_extension("./vec0") # The ~1MB binary

# 3. Create a Vector Virtual Table
conn.execute("CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[1536])")

# 4. Search by meaning
# The 'vec_distance_cosine' function is provided by the C extension
results = conn.execute("""
    SELECT doc_id, text_content 
    FROM vec_chunks 
    WHERE embedding MATCH ? 
    ORDER BY distance 
    LIMIT 5
""", [query_embedding])
```

## 6. Hybrid Search: The Secret Sauce

In a real-world document, users often search for specific terms (acronyms, names, product codes) that an embedding model might not "understand" deeply. This is where **Hybrid Search** wins.

### 6.1 The Two Pillars
1.  **BM25 (Keyword Search)**: Measures how often a specific word (e.g., "TX-900") appears in a chunk. It is highly precise for exact matches.
2.  **Semantic (Vector Search)**: Measures how similar the *concept* is (e.g., "The latest hardware" vs. "TX-900"). It is broad and covers paraphrasing.

### 6.2 The "Reciprocal Rank Fusion" (RRF) Strategy
To combine these, we don't just "add" the scores (since they are in different units). Instead, we use RRF:
- We run both searches.
- We look at the top results for both.
- A result that appears in the top 3 of **both** searches gets a massive "boost" in the final ranking.

**The Benefit**: If a user searches for "How do I fix the TX-900?", the keyword search finds the manual page for "TX-900", and the vector search finds the section about "fixing hardware." The hybrid result brings the exact correct page to the top.

## 7. The "Everything Else" (WriterAgent Roadmap)

Integrating this into `WriterAgent` involves three "non-vector" primitives that we must implement:

1.  **Semantic Chunking**: A text-parsing strategy that splits LibreOffice paragraphs into ~500-character windows, ensuring we don't split a sentence in the middle.
2.  **Versioning**: Embedding models change. We need a schema that allows us to re-index a document if the model (e.g., from OpenAI to Gemini) is swapped.
3.  **The Fallback**: If the user is on a niche architecture (e.g., an old PowerPC or a new RISC-V), we implement a **Pure Python** `dot_product` loop. It will be 10x slower but still works.

## 8. Local Inference (The "Offline" Option)

If the 4MB footprint constraint is ever relaxed to **~100MB**, we can implement fully local embedding generation. This removes API latency and costs.

### The "Tiny-Runtime" Stack:
- **Engine**: `onnxruntime-cpu` (~15-20MB). This is the fastest, leanest way to run local models without `torch` or `tensorflow`.
- **Model**: `all-MiniLM-L6-v2` (ONNX format). This is the gold standard for "fast and small" local embeddings (~45MB–90MB depending on precision).
- **The Process**: Python pulls the text from Writer -> Passes it to `onnxruntime` -> Receives the 384-dimensional vector -> Saves it to `sqlite-vec`.

## 9. Conclusion
Vector search in LibreOffice doesn't require a 100MB dependency bundle. By leveraging the built-in SQLite engine and a tiny, specialized C extension, we can provide industry-standard "Meaning Search" with a negligible impact on the extension's size and performance.

This turns `WriterAgent` from a simple wrapper UI into a powerful **Local Knowledge Base** for the user's documents.

## 10. Multi-Document Intelligence: Beyond "Similar Paragraphs"

The true "killer app" for vector search in LibreOffice is not just finding a similar sentence in the *current* file; it's understanding a **global corpus of documents**.

### 10.1 The Universal Semantic Index
By indexing every document the user opens or saves into a single `sqlite-vec` database, we enable:
- **Global Q&A**: "Across all my documents, what is our policy on remote work?"
- **Cross-File Discovery**: While writing "Project_X_Proposal.odt", the sidebar can automatically suggest: *"You wrote a similar section in '2025_Budget_Plan.ods' last year."*

### 10.2 Thematic Clustering
Since vectors are coordinates in a "Meaning Space," we can use standard clustering algorithms (like K-Means) to automatically group documents by topic.
- A user with 1,000 files can suddenly see them categorized into "Invoices," "Design Specs," and "Personal Notes" without ever creating a folder.

### 10.3 Synthesis & Gap Analysis
By comparing the vectors of two different documents, we can perform **Synthesis**:
- "What information is in Document A that is missing from my draft (Document B)?"
- Vector math can identify the "Semantic Delta" between files, helping the user ensure consistency across a large project.

## 11. The Recursive Splitter: Vetted Implementation

The most critical part of the "Everything Else" roadmap is the text splitter. A naive split by character index will cut words in half, destroying the embedding's meaning. 

To ensure stability and handle complex edge cases (e.g., massive paragraphs without punctuation), we recommend adapting the **RecursiveCharacterTextSplitter** from the FOSS **LangChain** ecosystem. This implementation has been battle-tested on millions of documents and is MIT licensed.

### Where to Grab the Code:
- **Repository**: [langchain-text-splitters (GitHub)](https://github.com/langchain-ai/langchain/tree/master/libs/text-splitters/langchain_text_splitters)
- **Key File**: `recursive_character.py` (Look for the `RecursiveCharacterTextSplitter` class).
- **The Core Logic**: It recursively attempts to split text using a prioritized set of separators: `["\n\n", "\n", " ", ""]`. If a chunk exceeds the `chunk_size`, it tries the next separator in the list.

### Why use the "Standard" version:
1.  **Paragraph Integrity**: It prioritizes keeping double-newlines (`\n\n`) together to preserve atomic ideas.
2.  **Smart Recombination**: After splitting, it smartly recombines pieces into the largest possible chunks that still fit within your size limit.
3.  **Proven Overlap Support**: Its `chunk_overlap` logic ensures that context from one chunk is correctly "bridged" into the next, which is vital for search accuracy.

## 12. Conclusion
Vector search in LibreOffice doesn't require a 100MB dependency bundle. By leveraging the built-in SQLite engine and a tiny, specialized C extension, we can provide industry-standard "Meaning Search" with a negligible impact on the extension's size and performance.

This turns `WriterAgent` from a simple wrapper UI into a powerful **Local Knowledge Base** for the user's documents.
