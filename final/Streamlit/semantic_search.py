"""Optional local multilingual embeddings. Never downloads models during a query."""
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def model():
    path = os.getenv('SEMANTIC_MODEL_PATH')
    if not path:
        return None
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(path,local_files_only=True)


def encode(texts):
    encoder = model()
    if encoder is None:
        return None
    return encoder.encode(texts,normalize_embeddings=True,show_progress_bar=False)
