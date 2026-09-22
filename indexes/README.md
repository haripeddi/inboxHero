# Vector index artifacts

Rebuild after regenerating `inbox.json`:

```bash
python3 scripts/build_index.py
```

Produces (gitignored):

- `inbox_vectors/embeddings.npy` — dense matrix, one row per message
- `inbox_vectors/meta.json` — message metadata + TF-IDF vocab (or ST backend tag)
